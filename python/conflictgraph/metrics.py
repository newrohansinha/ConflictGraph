from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
from numpy.typing import NDArray


@dataclass
class ClassificationMetrics:
    pr_auc: float
    roc_auc: float
    precision: float
    recall: float
    f1: float
    brier_score: float
    expected_calibration_error: float
    threshold: float
    confusion_matrix: list[list[int]]


def expected_calibration_error(
    labels: NDArray[Any], probabilities: NDArray[Any], bins: int = 10
) -> float:
    if bins <= 0:
        raise ValueError("calibration bin count must be positive")
    if labels.size != probabilities.size:
        raise ValueError("labels and probabilities must have equal length")
    if labels.size == 0:
        return 0.0
    boundaries = np.linspace(0.0, 1.0, bins + 1)
    error = 0.0
    for index in range(bins):
        lower, upper = boundaries[index], boundaries[index + 1]
        mask = (probabilities >= lower) & (
            probabilities < upper if index < bins - 1 else probabilities <= upper
        )
        if np.any(mask):
            error += np.mean(mask) * abs(
                float(np.mean(labels[mask])) - float(np.mean(probabilities[mask]))
            )
    return float(error)


def classification_metrics(
    labels: Sequence[int] | NDArray[Any],
    probabilities: Sequence[float] | NDArray[Any],
    threshold: float = 0.5,
) -> ClassificationMetrics:
    y = np.asarray(labels, dtype=np.int64)
    p = np.clip(np.asarray(probabilities, dtype=np.float64), 0, 1)
    if y.size != p.size:
        raise ValueError("labels and probabilities must have equal length")
    predictions = p >= threshold
    tp = int(np.sum((y == 1) & predictions))
    fp = int(np.sum((y == 0) & predictions))
    tn = int(np.sum((y == 0) & ~predictions))
    fn = int(np.sum((y == 1) & ~predictions))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    try:
        from sklearn.metrics import average_precision_score, roc_auc_score

        pr_auc = float(average_precision_score(y, p)) if y.size else 0.0
        roc_auc = float(roc_auc_score(y, p)) if len(set(y.tolist())) > 1 else 0.5
    except ImportError:
        pr_auc = _average_precision(y, p)
        roc_auc = _roc_auc(y, p)
    return ClassificationMetrics(
        pr_auc,
        roc_auc,
        precision,
        recall,
        f1,
        float(np.mean((p - y) ** 2)) if y.size else 0.0,
        expected_calibration_error(y, p),
        threshold,
        [[tn, fp], [fn, tp]],
    )


def _average_precision(labels: NDArray[Any], probabilities: NDArray[Any]) -> float:
    positive = int(np.sum(labels))
    if positive == 0:
        return 0.0
    order = np.argsort(-probabilities)
    hits, total = 0, 0.0
    for rank, index in enumerate(order, 1):
        if labels[index]:
            hits += 1
            total += hits / rank
    return total / positive


def _roc_auc(labels: NDArray[Any], probabilities: NDArray[Any]) -> float:
    positive = probabilities[labels == 1]
    negative = probabilities[labels == 0]
    if not len(positive) or not len(negative):
        return 0.5
    wins = sum(
        float(left > right) + 0.5 * float(left == right) for left in positive for right in negative
    )
    return wins / (len(positive) * len(negative))


def bootstrap_interval(
    values: Sequence[float], seed: int = 42, confidence: float = 0.95, samples: int = 2000
) -> tuple[float, float]:
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    if samples <= 0:
        raise ValueError("bootstrap sample count must be positive")
    data = np.asarray(values, dtype=np.float64)
    if data.size == 0:
        return 0.0, 0.0
    randomizer = np.random.default_rng(seed)
    means = np.mean(randomizer.choice(data, size=(samples, data.size), replace=True), axis=1)
    tail = (1 - confidence) / 2
    return float(np.quantile(means, tail)), float(np.quantile(means, 1 - tail))
