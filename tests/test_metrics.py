import numpy as np
import pytest
from conflictgraph.metrics import (
    bootstrap_interval,
    classification_metrics,
    expected_calibration_error,
)


def test_perfect_classifier_metrics():
    metrics = classification_metrics([0, 0, 1, 1], [0.01, 0.1, 0.9, 0.99])
    assert metrics.pr_auc == pytest.approx(1)
    assert metrics.roc_auc == pytest.approx(1)
    assert metrics.f1 == 1
    assert metrics.confusion_matrix == [[2, 0], [0, 2]]


def test_threshold_changes_recall():
    low = classification_metrics([1, 1, 0], [0.6, 0.4, 0.2], 0.3)
    high = classification_metrics([1, 1, 0], [0.6, 0.4, 0.2], 0.5)
    assert low.recall > high.recall


def test_ece_well_calibrated_bins():
    labels = np.array([0, 0, 1, 1])
    assert expected_calibration_error(labels, np.array([0, 0, 1, 1])) == 0


def test_bootstrap_interval_is_deterministic():
    left = bootstrap_interval([1, 2, 3, 4], seed=9, samples=200)
    right = bootstrap_interval([1, 2, 3, 4], seed=9, samples=200)
    assert left == right
    assert left[0] <= 2.5 <= left[1]


def test_empty_metrics_are_safe():
    metrics = classification_metrics([], [])
    assert metrics.pr_auc == 0 and metrics.brier_score == 0


@pytest.mark.parametrize("bins", [0, -1, -100])
def test_ece_rejects_nonpositive_bins(bins):
    with pytest.raises(ValueError, match="positive"):
        expected_calibration_error(np.array([0]), np.array([0.1]), bins)


def test_ece_rejects_mismatched_inputs():
    with pytest.raises(ValueError, match="equal length"):
        expected_calibration_error(np.array([0, 1]), np.array([0.1]))


@pytest.mark.parametrize("confidence", [-1.0, 0.0, 1.0, 2.0])
def test_bootstrap_rejects_invalid_confidence(confidence):
    with pytest.raises(ValueError, match="confidence"):
        bootstrap_interval([1.0, 2.0], confidence=confidence)


@pytest.mark.parametrize("samples", [0, -1, -100])
def test_bootstrap_rejects_nonpositive_samples(samples):
    with pytest.raises(ValueError, match="sample count"):
        bootstrap_interval([1.0, 2.0], samples=samples)
