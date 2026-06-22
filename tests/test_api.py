import json
from pathlib import Path

import conflictgraph.api as api_module
import pytest
from conflictgraph.api import create_app
from conflictgraph.config import Config, ModelConfig, TracingConfig
from conflictgraph.storage import ArtifactStore, StorageError
from fastapi.testclient import TestClient


class OfflinePostgres:
    def __init__(self, *_args, **_kwargs):
        pass

    def open(self):
        raise StorageError("database intentionally offline in API tests")


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api_module, "PostgresStore", OfflinePostgres)
    settings = Config(
        artifact_dir=str(tmp_path),
        model=ModelConfig(artifact=str(tmp_path / "model")),
        tracing=TracingConfig(mode="replay"),
    )
    with TestClient(create_app(settings)) as client:
        yield client, tmp_path


def test_health_reports_offline_database_and_heuristic(api_client):
    client, _ = api_client
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["database"].startswith("unavailable:")
    assert response.json()["model"] == "heuristic-fallback"


def test_health_detects_model_artifact_created_after_start(api_client):
    client, root = api_client
    metadata = root / "model" / "metadata.json"
    metadata.parent.mkdir()
    metadata.write_text("{}")
    assert client.get("/api/v1/health").json()["model"] == "trained"


def test_metrics_endpoint_exports_request_and_latency_metrics(api_client):
    client, _ = api_client
    client.get("/api/v1/health")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "conflictgraph_api_requests_total" in response.text
    assert "conflictgraph_api_request_duration_seconds" in response.text


def test_metrics_normalize_run_ids(api_client):
    client, _ = api_client
    client.get("/api/v1/runs/first-missing-run")
    response = client.get("/metrics")
    assert 'route="/api/v1/runs/{id}"' in response.text
    assert "first-missing-run" not in response.text


def test_runs_are_empty_without_artifacts(api_client):
    client, _ = api_client
    assert client.get("/api/v1/runs").json() == []


def test_runs_and_run_detail_use_artifact_fallback(api_client):
    client, root = api_client
    ArtifactStore(root).save("runs", "run-1", {"id": "run-1", "status": "COMPLETED"})
    assert client.get("/api/v1/runs").json()[0]["id"] == "run-1"
    response = client.get("/api/v1/runs/run-1")
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"


def test_missing_run_is_404(api_client):
    client, _ = api_client
    response = client.get("/api/v1/runs/missing")
    assert response.status_code == 404
    assert response.json()["detail"] == "run not found"


@pytest.mark.parametrize("limit", [0, -1, 501, 10000])
def test_run_limit_validation(limit, api_client):
    client, _ = api_client
    assert client.get("/api/v1/runs", params={"limit": limit}).status_code == 422


def test_unsafe_run_identifier_becomes_storage_error(api_client):
    client, _ = api_client
    response = client.get("/api/v1/runs/with%20space")
    assert response.status_code == 503
    assert response.json()["error"] == "storage_unavailable"


def test_default_benchmark_report_is_returned(api_client):
    client, root = api_client
    report = root / "benchmark" / "benchmark.json"
    report.parent.mkdir()
    report.write_text(json.dumps({"created_at": "now", "results": []}))
    assert client.get("/api/v1/benchmarks").json() == [{"created_at": "now", "results": []}]


@pytest.mark.parametrize("payload", ["{", "[]", '"not-an-object"'])
def test_malformed_default_benchmark_is_reported(payload, api_client):
    client, root = api_client
    report = root / "benchmark" / "benchmark.json"
    report.parent.mkdir()
    report.write_text(payload)
    response = client.get("/api/v1/benchmarks")
    assert response.status_code == 503


def test_default_benchmark_is_not_duplicated(api_client):
    client, root = api_client
    value = {"created_at": "same", "results": []}
    ArtifactStore(root).save("benchmarks", "saved", value)
    report = root / "benchmark" / "benchmark.json"
    report.parent.mkdir()
    report.write_text(json.dumps(value))
    assert client.get("/api/v1/benchmarks").json() == [value]


def test_models_returns_empty_then_valid_metadata(api_client):
    client, root = api_client
    assert client.get("/api/v1/models").json() == []
    metadata = root / "model" / "metadata.json"
    metadata.parent.mkdir()
    metadata.write_text(json.dumps({"version": "v1", "model_type": "graphsage"}))
    assert client.get("/api/v1/models").json()[0]["version"] == "v1"


@pytest.mark.parametrize("payload", ["{", "[]", "null", '"text"'])
def test_models_rejects_corrupt_or_nonobject_metadata(payload, api_client):
    client, root = api_client
    metadata = root / "model" / "metadata.json"
    metadata.parent.mkdir()
    metadata.write_text(payload)
    response = client.get("/api/v1/models")
    assert response.status_code == 503


def test_empty_graph_has_explicit_empty_shape(api_client):
    client, _ = api_client
    response = client.get("/api/v1/graph")
    assert response.status_code == 200
    assert response.json() == {
        "tests": [],
        "resources": [],
        "edges": [],
        "predictions": [],
        "empty": True,
    }


def test_graph_filters_and_limits_predictions(api_client):
    client, root = api_client
    ArtifactStore(root).save(
        "graphs",
        "latest",
        {
            "tests": ["a", "b"],
            "resources": [],
            "edges": [],
            "predictions": [
                {"test_a": "a", "test_b": "b", "probability": 0.9},
                {"test_a": "a", "test_b": "c", "probability": 0.8},
                {"test_a": "b", "test_b": "c", "probability": 0.1},
            ],
        },
    )
    response = client.get("/api/v1/graph", params={"min_risk": 0.5, "limit": 1})
    assert response.status_code == 200
    assert len(response.json()["predictions"]) == 1
    assert response.json()["predictions"][0]["probability"] == 0.9


@pytest.mark.parametrize(
    "predictions",
    ["not-a-list", ["not-an-object"], [{"probability": "high"}]],
)
def test_graph_rejects_malformed_predictions(predictions, api_client):
    client, root = api_client
    ArtifactStore(root).save("graphs", "latest", {"predictions": predictions})
    response = client.get("/api/v1/graph")
    assert response.status_code == 503


@pytest.mark.parametrize(
    "params",
    [
        {"min_risk": -0.1},
        {"min_risk": 1.1},
        {"limit": 0},
        {"limit": 10001},
    ],
)
def test_graph_query_validation(params, api_client):
    client, _ = api_client
    assert client.get("/api/v1/graph", params=params).status_code == 422


def test_multiple_app_factories_do_not_collide_on_prometheus_names(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api_module, "PostgresStore", OfflinePostgres)
    settings = Config(artifact_dir=str(tmp_path), model=ModelConfig(artifact=str(tmp_path / "m")))
    first = create_app(settings)
    second = create_app(settings)
    assert first is not second
