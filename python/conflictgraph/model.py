from __future__ import annotations

import hashlib
import json
import math
import platform
import random
import subprocess
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, cast

import numpy as np
from numpy.typing import NDArray

from .dataset import Dataset, DatasetSplit
from .errors import ArtifactError
from .graph import PairFeatures, TestResourceGraph
from .metrics import ClassificationMetrics, classification_metrics
from .types import ConflictCause, PairPrediction, dump_json

MODEL_SCHEMA_VERSION = "conflict-model-v1"


@dataclass
class TrainingConfig:
    seed: int = 42
    hidden_dim: int = 64
    embedding_dim: int = 48
    gnn_layers: int = 2
    learning_rate: float = 0.01
    weight_decay: float = 1e-4
    dropout: float = 0.2
    epochs: int = 100
    patience: int = 15
    batch_size: int = 256
    class_weight: Optional[float] = None
    calibration: str = "temperature"


@dataclass
class ArtifactMetadata:
    schema_version: str
    model_type: str
    version: str
    created_at: str
    feature_schema: list[str]
    dataset_hash: str
    seed: int
    code_revision: str
    training_config: dict[str, Any]
    validation_metrics: dict[str, Any]
    test_metrics: dict[str, Any]
    calibration: dict[str, Any]
    feature_normalization: dict[str, list[float]]
    environment: dict[str, str]
    checksum: str = ""


class TemperatureScaler:
    def __init__(self, temperature: float = 1.0) -> None:
        self.temperature = max(0.05, float(temperature))

    def fit(
        self,
        probabilities: Sequence[float] | NDArray[Any],
        labels: Sequence[int] | NDArray[Any],
    ) -> "TemperatureScaler":
        raw = np.asarray(probabilities, dtype=np.float64)
        y = np.asarray(labels, dtype=np.float64)
        if raw.ndim != 1 or y.ndim != 1:
            raise ValueError("calibration probabilities and labels must be one-dimensional")
        if raw.size == 0 or raw.size != y.size:
            raise ValueError("calibration probabilities and labels must have equal nonzero length")
        if not np.all(np.isfinite(raw)) or not np.all(np.isfinite(y)):
            raise ValueError("calibration inputs must be finite")
        p = np.clip(raw, 1e-6, 1 - 1e-6)
        logits = np.log(p / (1 - p))
        best_temperature, best_loss = 1.0, float("inf")
        for temperature in np.geomspace(0.1, 10.0, 200):
            scaled = 1 / (1 + np.exp(-logits / temperature))
            loss = -float(
                np.mean(y * np.log(scaled + 1e-12) + (1 - y) * np.log(1 - scaled + 1e-12))
            )
            if loss < best_loss:
                best_temperature, best_loss = float(temperature), loss
        self.temperature = best_temperature
        return self

    def transform(self, probabilities: Sequence[float] | NDArray[Any]) -> NDArray[np.float64]:
        raw = np.asarray(probabilities, dtype=np.float64)
        if raw.ndim != 1:
            raise ValueError("probabilities must be one-dimensional")
        if not np.all(np.isfinite(raw)):
            raise ValueError("probabilities must be finite")
        p = np.clip(raw, 1e-6, 1 - 1e-6)
        logits = np.log(p / (1 - p)) / self.temperature
        return np.asarray(np.clip(1 / (1 + np.exp(-logits)), 1e-9, 1 - 1e-9), dtype=np.float64)


class TabularBaseline:
    def __init__(self, kind: str = "gradient_boosting", seed: int = 42) -> None:
        self.kind, self.seed = kind, seed
        self.estimator: Any = None
        self.scaler: Any = None
        self.calibrator = TemperatureScaler()

    def fit(self, dataset: Dataset, split: DatasetSplit) -> dict[str, ClassificationMetrics]:
        try:
            from sklearn.ensemble import HistGradientBoostingClassifier
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
        except ImportError as exc:
            raise ArtifactError(
                "scikit-learn is required to train tabular baselines; install conflictgraph[ml]"
            ) from exc
        x_train, y_train, weights = dataset.arrays(split.train)
        x_validation, y_validation, _ = dataset.arrays(split.validation)
        x_test, y_test, _ = dataset.arrays(split.test)
        if self.kind == "logistic_regression":
            self.scaler = StandardScaler().fit(x_train)
            x_train = self.scaler.transform(x_train)
            x_validation = self.scaler.transform(x_validation)
            x_test = self.scaler.transform(x_test)
            self.estimator = LogisticRegression(
                max_iter=2000, class_weight="balanced", random_state=self.seed
            )
        elif self.kind == "gradient_boosting":
            self.estimator = HistGradientBoostingClassifier(
                max_iter=250,
                learning_rate=0.05,
                max_leaf_nodes=31,
                l2_regularization=0.1,
                random_state=self.seed,
            )
        else:
            raise ValueError(f"unknown baseline kind {self.kind}")
        self.estimator.fit(x_train, y_train, sample_weight=weights)
        validation_probabilities = self.estimator.predict_proba(x_validation)[:, 1]
        if len(set(y_validation.tolist())) > 1:
            self.calibrator.fit(validation_probabilities, y_validation)
        validation_calibrated = self.calibrator.transform(validation_probabilities)
        test_probabilities = self.calibrator.transform(self.estimator.predict_proba(x_test)[:, 1])
        return {
            "validation": classification_metrics(y_validation, validation_calibrated),
            "test": classification_metrics(y_test, test_probabilities),
        }

    def predict(self, features: NDArray[Any]) -> NDArray[np.float64]:
        if self.estimator is None:
            raise ArtifactError("baseline model has not been trained")
        transformed = self.scaler.transform(features) if self.scaler is not None else features
        return self.calibrator.transform(self.estimator.predict_proba(transformed)[:, 1])


def _torch_modules() -> tuple[Any, Any, Any]:
    try:
        import torch
        import torch.nn as nn
        import torch.nn.functional as functional

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"`torch\.jit\.script` is deprecated\. Please switch to `torch\.compile` or `torch\.export`\.",
                category=DeprecationWarning,
            )
            from torch_geometric.nn import SAGEConv
    except ImportError as exc:
        raise ArtifactError(
            "PyTorch and PyTorch Geometric are required for GNN training; install conflictgraph[ml]"
        ) from exc
    return torch, nn, (functional, SAGEConv)


class HybridConflictGNN:
    """Factory-backed heterogeneous GraphSAGE encoder plus explicit pair semantics."""

    def __new__(
        cls,
        test_feature_dim: int,
        resource_feature_dim: int,
        pair_feature_dim: int,
        config: TrainingConfig,
    ) -> Any:
        torch, nn, modules = _torch_modules()
        functional, SAGEConv = modules

        class Network(nn.Module):  # type: ignore[name-defined,misc]
            def __init__(self) -> None:
                super().__init__()
                hidden = config.hidden_dim
                self.test_projection = nn.Linear(test_feature_dim, hidden)
                self.resource_projection = nn.Linear(resource_feature_dim, hidden)
                self.convolutions = nn.ModuleList(
                    [SAGEConv(hidden, hidden) for _ in range(config.gnn_layers)]
                )
                pair_input = hidden * 4 + pair_feature_dim
                self.pair_head = nn.Sequential(
                    nn.Linear(pair_input, hidden * 2),
                    nn.ReLU(),
                    nn.Dropout(config.dropout),
                    nn.Linear(hidden * 2, hidden),
                    nn.ReLU(),
                    nn.Dropout(config.dropout),
                    nn.Linear(hidden, 1),
                )
                # A direct semantic residual prevents pair-disjoint generalization from
                # being drowned out by test-specific embeddings early in training.
                self.explicit_head = nn.Linear(pair_feature_dim, 1)

            def encode(self, test_x: Any, resource_x: Any, edge_index: Any) -> Any:
                features = torch.cat(
                    [self.test_projection(test_x), self.resource_projection(resource_x)], dim=0
                )
                for convolution in self.convolutions:
                    residual = features
                    features = convolution(features, edge_index)
                    features = functional.relu(features)
                    features = functional.dropout(
                        features, p=config.dropout, training=self.training
                    )
                    features = features + residual
                return features[: test_x.shape[0]]

            def forward(
                self, test_x: Any, resource_x: Any, edge_index: Any, pairs: Any, explicit: Any
            ) -> Any:
                embeddings = self.encode(test_x, resource_x, edge_index)
                left, right = embeddings[pairs[:, 0]], embeddings[pairs[:, 1]]
                combined = torch.cat(
                    [left, right, torch.abs(left - right), left * right, explicit], dim=1
                )
                graph_logit = self.pair_head(combined).squeeze(1)
                explicit_logit = self.explicit_head(explicit).squeeze(1)
                return explicit_logit + 0.1 * torch.tanh(graph_logit)

        return Network()


@dataclass
class GraphTensors:
    test_x: Any
    resource_x: Any
    edge_index: Any
    test_index: dict[str, int]


def graph_tensors(graph: TestResourceGraph, device: str = "cpu") -> GraphTensors:
    torch, _, _ = _torch_modules()
    tests = sorted(graph.tests)
    resources = sorted(graph.resources)
    test_index = {value: index for index, value in enumerate(tests)}
    resource_index = {value: index for index, value in enumerate(resources)}
    test_features = []
    for test_id in tests:
        stats = graph.test_stats[test_id]
        test_features.append(
            [
                math.log1p(stats.duration_ema),
                stats.failure_rate,
                math.log1p(stats.resource_count),
                math.log1p(stats.process_count),
                stats.write_ratio,
            ]
        )
    resource_features = []
    resource_types = list(__import__("conflictgraph.types", fromlist=["ResourceType"]).ResourceType)
    for resource_id in resources:
        resource = graph.resources[resource_id]
        type_one_hot = [float(resource.type == candidate) for candidate in resource_types]
        degree = len(graph.resource_tests[resource_id])
        mutating = sum(
            graph.edges[(test_id, resource_id)].mutating
            for test_id in graph.resource_tests[resource_id]
        )
        resource_features.append(
            [*type_one_hot, math.log1p(degree), mutating / max(1, degree), 1.0 / max(1, degree)]
        )
    edges: list[list[int]] = [[], []]
    offset = len(tests)
    for test_id, resource_id in graph.edges:
        left, right = test_index[test_id], offset + resource_index[resource_id]
        edges[0].extend([left, right])
        edges[1].extend([right, left])
    return GraphTensors(
        torch.tensor(test_features, dtype=torch.float32, device=device),
        torch.tensor(resource_features, dtype=torch.float32, device=device),
        torch.tensor(edges, dtype=torch.long, device=device),
        test_index,
    )


class GNNTrainer:
    def __init__(self, config: Optional[TrainingConfig] = None) -> None:
        self.config = config or TrainingConfig()
        self.model: Any = None
        self.calibrator = TemperatureScaler()
        self.training_history: list[dict[str, float]] = []
        self.feature_mean = np.zeros(len(PairFeatures.FEATURE_NAMES), dtype=np.float32)
        self.feature_scale = np.ones(len(PairFeatures.FEATURE_NAMES), dtype=np.float32)

    def fit(
        self, graph: TestResourceGraph, dataset: Dataset, split: DatasetSplit, device: str = "cpu"
    ) -> dict[str, ClassificationMetrics]:
        torch, nn, _ = _torch_modules()
        random.seed(self.config.seed)
        np.random.seed(self.config.seed)
        torch.manual_seed(self.config.seed)
        tensors = graph_tensors(graph, device)
        if tensors.resource_x.shape[0] == 0:
            raise ArtifactError("cannot train a graph model without resource nodes")
        self.model = cast(
            Any,
            HybridConflictGNN(
                tensors.test_x.shape[1],
                tensors.resource_x.shape[1],
                len(PairFeatures.FEATURE_NAMES),
                self.config,
            ),
        ).to(device)
        optimizer = torch.optim.AdamW(
            self.model.parameters(),
            lr=self.config.learning_rate,
            weight_decay=self.config.weight_decay,
        )
        positives = max(1, dataset.metadata.positive_examples)
        negatives = max(1, dataset.metadata.negative_examples)
        positive_weight = self.config.class_weight or negatives / positives
        criterion = nn.BCEWithLogitsLoss(
            pos_weight=torch.tensor(positive_weight, device=device), reduction="none"
        )
        train_features, _, _ = dataset.arrays(split.train)
        self.feature_mean = np.mean(train_features, axis=0)
        self.feature_scale = np.std(train_features, axis=0)
        self.feature_scale[self.feature_scale < 1e-6] = 1.0
        # Warm up the explicit semantic residual before graph-message parameters can
        # memorize test identities. This gives pair-disjoint validation a stable base,
        # after which bounded graph evidence may improve rather than replace it.
        train_examples = [dataset.examples[index] for index in split.train]
        warm_features = torch.tensor(
            (train_features - self.feature_mean) / self.feature_scale,
            dtype=torch.float32,
            device=device,
        )
        warm_labels = torch.tensor(
            [example.label for example in train_examples], dtype=torch.float32, device=device
        )
        warm_weights = torch.tensor(
            [example.confidence for example in train_examples],
            dtype=torch.float32,
            device=device,
        )
        warm_optimizer = torch.optim.Adam(
            self.model.explicit_head.parameters(), lr=max(0.01, self.config.learning_rate)
        )
        for _ in range(200):
            warm_optimizer.zero_grad(set_to_none=True)
            warm_logits = self.model.explicit_head(warm_features).squeeze(1)
            warm_loss = (criterion(warm_logits, warm_labels) * warm_weights).mean()
            warm_loss.backward()
            warm_optimizer.step()
        for parameter in self.model.explicit_head.parameters():
            parameter.requires_grad_(False)
        best_state, best_score, stale = None, -1.0, 0
        train_batches = self._batches(split.train, self.config.batch_size, self.config.seed)
        for epoch in range(self.config.epochs):
            self.model.train()
            epoch_losses: list[float] = []
            for indices in train_batches:
                pairs, explicit, labels, weights = self._batch(
                    dataset, indices, tensors.test_index, torch, device
                )
                optimizer.zero_grad(set_to_none=True)
                logits = self.model(
                    tensors.test_x, tensors.resource_x, tensors.edge_index, pairs, explicit
                )
                loss = (criterion(logits, labels) * weights).mean()
                loss.backward()
                nn.utils.clip_grad_norm_(self.model.parameters(), 5.0)
                optimizer.step()
                epoch_losses.append(float(loss.detach().cpu()))
            validation_probabilities = self._predict_indices(
                dataset, split.validation, tensors, torch, device
            )
            validation_labels = [dataset.examples[index].label for index in split.validation]
            metrics = classification_metrics(validation_labels, validation_probabilities)
            self.training_history.append(
                {
                    "epoch": float(epoch),
                    "loss": float(np.mean(epoch_losses)),
                    "pr_auc": metrics.pr_auc,
                    "brier": metrics.brier_score,
                }
            )
            score = metrics.pr_auc - 0.15 * metrics.brier_score
            if score > best_score + 1e-5:
                best_score, stale = score, 0
                best_state = {
                    key: value.detach().cpu().clone()
                    for key, value in self.model.state_dict().items()
                }
            else:
                stale += 1
                if epoch >= 80 and stale >= self.config.patience:
                    break
        if best_state is None:
            raise ArtifactError("training did not produce a valid model state")
        self.model.load_state_dict(best_state)
        validation_raw = self._predict_indices(dataset, split.validation, tensors, torch, device)
        validation_labels = [dataset.examples[index].label for index in split.validation]
        if self.config.calibration == "temperature" and len(set(validation_labels)) > 1:
            self.calibrator.fit(validation_raw, validation_labels)
        validation = self.calibrator.transform(validation_raw)
        test_raw = self._predict_indices(dataset, split.test, tensors, torch, device)
        test = self.calibrator.transform(test_raw)
        return {
            "validation": classification_metrics(validation_labels, validation),
            "test": classification_metrics(
                [dataset.examples[index].label for index in split.test], test
            ),
        }

    @staticmethod
    def _batches(indices: Sequence[int], size: int, seed: int) -> list[list[int]]:
        values = list(indices)
        random.Random(seed).shuffle(values)
        return [values[index : index + size] for index in range(0, len(values), size)]

    def _batch(
        self,
        dataset: Dataset,
        indices: Sequence[int],
        test_index: Mapping[str, int],
        torch: Any,
        device: str,
    ) -> tuple[Any, Any, Any, Any]:
        examples = [dataset.examples[index] for index in indices]
        pairs = torch.tensor(
            [[test_index[item.test_a], test_index[item.test_b]] for item in examples],
            dtype=torch.long,
            device=device,
        )
        explicit_values = np.asarray([item.features for item in examples], dtype=np.float32)
        explicit_values = (explicit_values - self.feature_mean) / self.feature_scale
        explicit = torch.tensor(explicit_values, dtype=torch.float32, device=device)
        labels = torch.tensor([item.label for item in examples], dtype=torch.float32, device=device)
        weights = torch.tensor(
            [item.confidence for item in examples], dtype=torch.float32, device=device
        )
        return pairs, explicit, labels, weights

    def _predict_indices(
        self,
        dataset: Dataset,
        indices: Sequence[int],
        tensors: GraphTensors,
        torch: Any,
        device: str,
    ) -> NDArray[Any]:
        self.model.eval()
        output: list[NDArray[Any]] = []
        with torch.no_grad():
            ordered = list(indices)
            batches = [
                ordered[index : index + self.config.batch_size]
                for index in range(0, len(ordered), self.config.batch_size)
            ]
            for batch in batches:
                pairs, explicit, _, _ = self._batch(
                    dataset, batch, tensors.test_index, torch, device
                )
                logits = self.model(
                    tensors.test_x, tensors.resource_x, tensors.edge_index, pairs, explicit
                )
                output.append(torch.sigmoid(logits).cpu().numpy())
        return np.concatenate(output) if output else np.asarray([], dtype=np.float32)

    def save(
        self, directory: Path, dataset: Dataset, metrics: Mapping[str, ClassificationMetrics]
    ) -> ArtifactMetadata:
        if self.model is None:
            raise ArtifactError("cannot save an untrained model")
        torch, _, _ = _torch_modules()
        directory.mkdir(parents=True, exist_ok=True)
        weights_path = directory / "model.pt"
        torch.save(self.model.state_dict(), weights_path)
        digest = hashlib.sha256(weights_path.read_bytes()).hexdigest()
        revision = "unknown"
        try:
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            pass
        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + digest[:8]
        metadata = ArtifactMetadata(
            MODEL_SCHEMA_VERSION,
            "hybrid-graphsage",
            version,
            datetime.now(timezone.utc).isoformat(),
            list(PairFeatures.FEATURE_NAMES),
            dataset.metadata.content_hash,
            self.config.seed,
            revision,
            asdict(self.config),
            asdict(metrics["validation"]),
            asdict(metrics["test"]),
            {"method": "temperature", "temperature": self.calibrator.temperature},
            {
                "mean": self.feature_mean.tolist(),
                "scale": self.feature_scale.tolist(),
            },
            {"python": platform.python_version(), "platform": platform.platform()},
            digest,
        )
        dump_json(metadata, directory / "metadata.json")
        dump_json(self.training_history, directory / "history.json")
        return metadata


def select_non_regressing_model(
    candidate: ClassificationMetrics,
    active: ClassificationMetrics,
    pr_tolerance: float = 0.005,
    brier_tolerance: float = 0.01,
) -> bool:
    return (
        candidate.pr_auc >= active.pr_auc - pr_tolerance
        and candidate.brier_score <= active.brier_score + brier_tolerance
        and candidate.f1 >= active.f1 - 0.02
    )


def pair_predictions(
    dataset: Dataset,
    probabilities: Sequence[float] | NDArray[Any],
    heuristic_predictions: Sequence[PairPrediction],
    model_version: str,
) -> list[PairPrediction]:
    by_pair = {prediction.key: prediction for prediction in heuristic_predictions}
    if len(dataset.examples) != len(probabilities):
        raise ArtifactError("prediction count does not match the inference dataset")
    for index, example in enumerate(dataset.examples):
        probability = probabilities[index]
        base = by_pair.get((example.test_a, example.test_b))
        by_pair[(example.test_a, example.test_b)] = PairPrediction(
            example.test_a,
            example.test_b,
            float(probability),
            base.cause if base else ConflictCause.UNKNOWN,
            model_version,
            base.shared_resources if base else example.resources,
            base.explanation if base else "model prediction from pair features",
        )
    return list(by_pair.values())


def predict_artifact(
    directory: Path,
    graph: TestResourceGraph,
    dataset: Dataset,
    device: str = "cpu",
) -> tuple[NDArray[np.float64], ArtifactMetadata]:
    """Load a checksummed GNN artifact and predict dataset rows in stable order."""
    try:
        metadata = ArtifactMetadata(**json.loads((directory / "metadata.json").read_text()))
        weights = directory / "model.pt"
        checksum = hashlib.sha256(weights.read_bytes()).hexdigest()
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise ArtifactError(f"invalid model artifact at {directory}: {exc}") from exc
    if metadata.schema_version != MODEL_SCHEMA_VERSION:
        raise ArtifactError(f"unsupported model schema {metadata.schema_version}")
    if checksum != metadata.checksum:
        raise ArtifactError("model weight checksum does not match metadata")
    if metadata.feature_schema != list(PairFeatures.FEATURE_NAMES):
        raise ArtifactError("model feature schema is incompatible with this code")
    torch, _, _ = _torch_modules()
    config = TrainingConfig(**metadata.training_config)
    tensors = graph_tensors(graph, device)
    model = cast(
        Any,
        HybridConflictGNN(
            tensors.test_x.shape[1],
            tensors.resource_x.shape[1],
            len(PairFeatures.FEATURE_NAMES),
            config,
        ),
    ).to(device)
    try:
        state = torch.load(weights, map_location=device, weights_only=True)
        model.load_state_dict(state)
    except Exception as exc:
        raise ArtifactError(f"could not load model weights: {exc}") from exc
    mean = np.asarray(metadata.feature_normalization["mean"], dtype=np.float32)
    scale = np.asarray(metadata.feature_normalization["scale"], dtype=np.float32)
    test_index = tensors.test_index
    examples = dataset.examples
    probabilities: list[NDArray[Any]] = []
    model.eval()
    with torch.no_grad():
        for offset in range(0, len(examples), config.batch_size):
            batch = examples[offset : offset + config.batch_size]
            pairs = torch.tensor(
                [[test_index[item.test_a], test_index[item.test_b]] for item in batch],
                dtype=torch.long,
                device=device,
            )
            explicit = np.asarray([item.features for item in batch], dtype=np.float32)
            explicit = torch.tensor((explicit - mean) / scale, dtype=torch.float32, device=device)
            logits = model(tensors.test_x, tensors.resource_x, tensors.edge_index, pairs, explicit)
            probabilities.append(torch.sigmoid(logits).cpu().numpy())
    raw = np.concatenate(probabilities) if probabilities else np.asarray([], dtype=np.float32)
    calibrated = TemperatureScaler(float(metadata.calibration["temperature"])).transform(raw)
    return calibrated, metadata
