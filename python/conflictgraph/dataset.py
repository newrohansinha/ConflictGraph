from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np
from numpy.typing import NDArray

from .errors import ArtifactError
from .graph import PairFeatures, TestResourceGraph
from .types import ConflictCause, TraceQuality, dump_json, stable_pair

SCHEMA_VERSION = "pair-features-v1"


@dataclass(frozen=True)
class ConflictLabel:
    test_a: str
    test_b: str
    conflict: bool
    cause: ConflictCause = ConflictCause.UNKNOWN
    confidence: float = 1.0
    source: str = "ground_truth"

    @property
    def key(self) -> tuple[str, str]:
        return stable_pair(self.test_a, self.test_b)


@dataclass
class PairExample:
    test_a: str
    test_b: str
    features: list[float]
    label: int
    confidence: float
    cause: str
    split_group: str
    resources: list[str]


@dataclass
class DatasetMetadata:
    schema_version: str
    created_at: str
    seed: int
    source_runs: list[str]
    test_count: int
    resource_count: int
    example_count: int
    positive_examples: int
    negative_examples: int
    feature_names: list[str]
    trace_quality: float
    content_hash: str = ""


@dataclass
class Dataset:
    examples: list[PairExample]
    metadata: DatasetMetadata

    def arrays(
        self, indices: Optional[Sequence[int]] = None
    ) -> tuple[NDArray[np.float32], NDArray[np.float32], NDArray[np.float32]]:
        selected = self.examples if indices is None else [self.examples[index] for index in indices]
        if selected:
            x = np.asarray([item.features for item in selected], dtype=np.float32)
        else:
            x = np.empty((0, len(self.metadata.feature_names)), dtype=np.float32)
        y = np.asarray([item.label for item in selected], dtype=np.float32)
        weights = np.asarray([item.confidence for item in selected], dtype=np.float32)
        return x, y, weights

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        rows = [asdict(example) for example in self.examples]
        payload = "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n"
        (directory / "examples.jsonl").write_text(payload)
        self.metadata.content_hash = hashlib.sha256(payload.encode()).hexdigest()
        dump_json(self.metadata, directory / "metadata.json")
        try:
            import polars as pl

            flattened = []
            for item in self.examples:
                row: dict[str, Any] = {
                    "test_a": item.test_a,
                    "test_b": item.test_b,
                    "label": item.label,
                    "confidence": item.confidence,
                    "cause": item.cause,
                    "split_group": item.split_group,
                    "resources": item.resources,
                }
                row.update(dict(zip(PairFeatures.FEATURE_NAMES, item.features, strict=True)))
                flattened.append(row)
            pl.DataFrame(flattened).write_parquet(
                directory / "examples.parquet", compression="zstd"
            )
        except ImportError:
            pass

    @classmethod
    def load(cls, directory: Path) -> "Dataset":
        try:
            metadata_raw = json.loads((directory / "metadata.json").read_text())
            examples_raw = [
                json.loads(line)
                for line in (directory / "examples.jsonl").read_text().splitlines()
                if line
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError(f"Invalid dataset at {directory}: {exc}") from exc
        if metadata_raw.get("schema_version") != SCHEMA_VERSION:
            raise ArtifactError(
                f"Unsupported dataset schema {metadata_raw.get('schema_version')!r}"
            )
        metadata = DatasetMetadata(**metadata_raw)
        payload = "\n".join(json.dumps(value, sort_keys=True) for value in examples_raw) + "\n"
        if (
            metadata.content_hash
            and hashlib.sha256(payload.encode()).hexdigest() != metadata.content_hash
        ):
            raise ArtifactError("Dataset content hash does not match metadata")
        try:
            examples = [PairExample(**value) for value in examples_raw]
        except TypeError as exc:
            raise ArtifactError(f"Dataset row does not match {SCHEMA_VERSION}: {exc}") from exc
        return cls(examples, metadata)


class DatasetBuilder:
    def __init__(self, seed: int = 42, negative_ratio: Optional[float] = 4.0) -> None:
        if negative_ratio is not None and negative_ratio < 0:
            raise ValueError("negative_ratio must be non-negative or None")
        self.seed, self.negative_ratio = seed, negative_ratio

    def build(
        self,
        graph: TestResourceGraph,
        labels: Iterable[ConflictLabel],
        source_runs: Sequence[str] = (),
        trace_quality: Optional[TraceQuality] = None,
    ) -> Dataset:
        label_index = {label.key: label for label in labels}
        candidates = set(graph.candidate_pairs(include_readonly=True)) | set(label_index)
        positives: list[PairExample] = []
        negatives: list[PairExample] = []
        for pair in sorted(candidates):
            features = graph.pair_features(*pair)
            label = label_index.get(pair)
            conflict = bool(label and label.conflict)
            example = PairExample(
                pair[0],
                pair[1],
                features.vector(),
                int(conflict),
                label.confidence if label else 0.65,
                label.cause.value if label else ConflictCause.UNKNOWN.value,
                hashlib.sha256((pair[0] + "\0" + pair[1]).encode()).hexdigest()[:16],
                [item["resource"] for item in features.evidence],
            )
            (positives if conflict else negatives).append(example)
        if self.negative_ratio is not None:
            limit = int(max(len(positives) * self.negative_ratio, min(len(negatives), 32)))
            if len(negatives) > limit:
                negatives = random.Random(self.seed).sample(negatives, limit)
        examples = sorted(positives + negatives, key=lambda item: (item.test_a, item.test_b))
        quality_score = trace_quality.score if trace_quality else 1.0
        metadata = DatasetMetadata(
            SCHEMA_VERSION,
            datetime.now(timezone.utc).isoformat(),
            self.seed,
            list(source_runs),
            len(graph.tests),
            len(graph.resources),
            len(examples),
            len(positives),
            len(negatives),
            list(PairFeatures.FEATURE_NAMES),
            quality_score,
        )
        return Dataset(examples, metadata)


@dataclass
class DatasetSplit:
    train: list[int] = field(default_factory=list)
    validation: list[int] = field(default_factory=list)
    test: list[int] = field(default_factory=list)
    strategy: str = "pair-disjoint"

    def validate(self, dataset: Dataset) -> None:
        sets = [set(self.train), set(self.validation), set(self.test)]
        if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
            raise ValueError("dataset split indices overlap")
        if any(index < 0 or index >= len(dataset.examples) for group in sets for index in group):
            raise ValueError("dataset split index is out of range")
        pair_groups = [
            {dataset.examples[index].split_group for index in indices}
            for indices in (self.train, self.validation, self.test)
        ]
        if (
            pair_groups[0] & pair_groups[1]
            or pair_groups[0] & pair_groups[2]
            or pair_groups[1] & pair_groups[2]
        ):
            raise ValueError("pair leakage detected across splits")


def pair_disjoint_split(
    dataset: Dataset, seed: int = 42, ratios: tuple[float, float, float] = (0.7, 0.15, 0.15)
) -> DatasetSplit:
    if abs(sum(ratios) - 1.0) > 1e-9 or any(value <= 0 for value in ratios):
        raise ValueError("split ratios must be positive and sum to one")
    by_group: dict[str, list[int]] = defaultdict(list)
    for index, example in enumerate(dataset.examples):
        by_group[example.split_group].append(index)
    groups = sorted(by_group)
    random.Random(seed).shuffle(groups)
    target = [len(dataset.examples) * value for value in ratios]
    buckets: list[list[int]] = [[], [], []]
    positives = [0, 0, 0]
    for group in groups:
        indices = by_group[group]
        group_positives = sum(dataset.examples[index].label for index in indices)
        # Balance size and positive count without ever breaking pair groups.
        scores = []
        total_positive_target = max(1, dataset.metadata.positive_examples)
        for bucket in range(3):
            size_pressure = len(buckets[bucket]) / max(1, target[bucket])
            positive_pressure = positives[bucket] / max(1, total_positive_target * ratios[bucket])
            scores.append((size_pressure + positive_pressure, bucket))
        bucket = min(scores)[1]
        buckets[bucket].extend(indices)
        positives[bucket] += group_positives
    split = DatasetSplit(
        train=sorted(buckets[0]),
        validation=sorted(buckets[1]),
        test=sorted(buckets[2]),
    )
    split.validate(dataset)
    return split


def family_holdout_split(
    dataset: Dataset, held_out_causes: set[str], seed: int = 42
) -> DatasetSplit:
    test = [
        index for index, example in enumerate(dataset.examples) if example.cause in held_out_causes
    ]
    remaining = [index for index in range(len(dataset.examples)) if index not in set(test)]
    random.Random(seed).shuffle(remaining)
    validation_size = max(1, int(len(remaining) * 0.2)) if remaining else 0
    return DatasetSplit(
        sorted(remaining[validation_size:]),
        sorted(remaining[:validation_size]),
        sorted(test),
        "conflict-family-holdout",
    )
