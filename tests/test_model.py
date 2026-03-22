import numpy as np
import pytest
from conflictgraph.metrics import ClassificationMetrics
from conflictgraph.model import TemperatureScaler, select_non_regressing_model


def metric(pr=0.8, brier=0.1, f1=0.7):
    return ClassificationMetrics(pr, 0.9, 0.7, 0.7, f1, brier, 0.03, 0.5, [[8, 2], [2, 8]])


def test_temperature_scaler_bounds_probabilities():
    scaler = TemperatureScaler().fit([0.05, 0.1, 0.8, 0.9], [0, 0, 1, 1])
    output = scaler.transform([0, 0.5, 1])
    assert np.all(output > 0) and np.all(output < 1)
    assert output[0] < output[1] < output[2]


def test_temperature_scaling_improves_overconfident_data():
    probabilities = np.array([0.001, 0.999, 0.001, 0.999])
    labels = np.array([0, 0, 1, 1])
    scaler = TemperatureScaler().fit(probabilities, labels)
    assert scaler.temperature > 1


def test_model_regression_guard_accepts_improvement():
    assert select_non_regressing_model(metric(0.85, 0.08, 0.73), metric())


def test_model_regression_guard_rejects_pr_drop():
    assert not select_non_regressing_model(metric(0.7, 0.08, 0.73), metric())


def test_model_regression_guard_rejects_calibration_drop():
    assert not select_non_regressing_model(metric(0.81, 0.2, 0.73), metric())


@pytest.mark.parametrize(
    ("probabilities", "labels", "message"),
    [
        ([], [], "nonzero"),
        ([0.1], [0, 1], "equal"),
        ([[0.1]], [[0]], "one-dimensional"),
        ([float("nan")], [0], "finite"),
        ([0.1], [float("inf")], "finite"),
    ],
)
def test_temperature_fit_rejects_invalid_training_data(probabilities, labels, message):
    with pytest.raises(ValueError, match=message):
        TemperatureScaler().fit(probabilities, labels)


@pytest.mark.parametrize("probabilities", [[[0.1]], [float("nan")], [float("inf")]])
def test_temperature_transform_rejects_invalid_probabilities(probabilities):
    with pytest.raises(ValueError):
        TemperatureScaler().transform(probabilities)
