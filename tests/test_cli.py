import json
from pathlib import Path

import conflictgraph.cli as cli_module
import conflictgraph.model as model_module
import pytest
import yaml
from conflictgraph.cli import app
from conflictgraph.dataset import Dataset
from conflictgraph.diagnostics import Diagnostic
from conflictgraph.types import (
    AccessMode,
    EventSource,
    Operation,
    ResourceType,
    TestIdentity,
    TraceEvent,
)
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "conflictgraph.example.yaml"
    value = yaml.safe_load(source.read_text())
    value["artifact_dir"] = str(tmp_path / "artifacts")
    value["model"]["artifact"] = str(tmp_path / "model")
    destination = tmp_path / "conflictgraph.yaml"
    destination.write_text(yaml.safe_dump(value))
    return destination


def trace_event(test_id: str, timestamp: int = 1) -> TraceEvent:
    return TraceEvent(
        execution_id=f"execution-{test_id}",
        test_id=test_id,
        timestamp_ns=timestamp,
        pid=1,
        tid=1,
        cgroup_id=1,
        resource_type=ResourceType.FILE,
        resource_identifier="/tmp/shared.db",
        operation=Operation.WRITE,
        access_mode=AccessMode.WRITE,
        source=EventSource.REPLAY,
    )


def write_trace(path: Path, events: list[TraceEvent]) -> None:
    path.write_text("".join(json.dumps(event.to_dict()) + "\n" for event in events))


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "ConflictGraph 0.1.0"


def test_init_creates_valid_configuration(tmp_path: Path):
    destination = tmp_path / "conflictgraph.yaml"
    result = runner.invoke(app, ["init", "--path", str(destination)])
    assert result.exit_code == 0
    assert destination.exists()
    assert cli_module.load_config(destination).tracing.control_socket.endswith("tracer.sock")


def test_init_refuses_overwrite_without_force(tmp_path: Path):
    destination = tmp_path / "conflictgraph.yaml"
    destination.write_text("keep-me")
    result = runner.invoke(app, ["init", "--path", str(destination)])
    assert result.exit_code == 2
    assert "already exists" in result.output
    assert destination.read_text() == "keep-me"


def test_init_force_replaces_existing_file(tmp_path: Path):
    destination = tmp_path / "conflictgraph.yaml"
    destination.write_text("replace-me")
    result = runner.invoke(app, ["init", "--path", str(destination), "--force"])
    assert result.exit_code == 0
    assert "version: 1" in destination.read_text()


def test_doctor_json_output(config_path: Path, monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "run_diagnostics",
        lambda _settings: [Diagnostic("python", "pass", "/usr/bin/python3")],
    )
    result = runner.invoke(app, ["doctor", "--config", str(config_path), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == [
        {"name": "python", "status": "pass", "detail": "/usr/bin/python3", "remediation": ""}
    ]


def test_invalid_config_has_actionable_exit_code(tmp_path: Path):
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("version: 1\nworkers: 0\n")
    result = runner.invoke(app, ["doctor", "--config", str(invalid)])
    assert result.exit_code == 2
    assert "workers must be between" in result.output


def test_trace_summary_json(tmp_path: Path, config_path: Path):
    trace = tmp_path / "trace.jsonl"
    write_trace(trace, [trace_event("a"), trace_event("b", 2)])
    result = runner.invoke(
        app, ["trace", "summary", str(trace), "--config", str(config_path), "--json"]
    )
    assert result.exit_code == 0
    value = json.loads(result.stdout)
    assert value["events"] == 2
    assert value["tests"] == 2
    assert value["resources"] == 1


def test_trace_summary_human_output(tmp_path: Path, config_path: Path):
    trace = tmp_path / "trace.jsonl"
    write_trace(trace, [trace_event("a")])
    result = runner.invoke(app, ["trace", "summary", str(trace), "--config", str(config_path)])
    assert result.exit_code == 0
    assert "1 events, 1 tests, 1 resources, quality 100.0%" in result.stdout


def test_trace_replay_writes_graph_and_predictions(tmp_path: Path, config_path: Path):
    trace = tmp_path / "trace.jsonl"
    output = tmp_path / "graph.json"
    write_trace(trace, [trace_event("a"), trace_event("b", 2)])
    result = runner.invoke(
        app,
        ["trace", "replay", str(trace), "--config", str(config_path), "--output", str(output)],
    )
    assert result.exit_code == 0
    graph = json.loads(output.read_text())
    assert len(graph["tests"]) == 2
    assert len(graph["resources"]) == 1
    assert len(graph["predictions"]) == 1


def test_trace_replay_falls_back_when_model_runtime_is_unavailable(
    tmp_path: Path, config_path: Path, monkeypatch
):
    trace = tmp_path / "trace.jsonl"
    output = tmp_path / "graph.json"
    write_trace(trace, [trace_event("a"), trace_event("b", 2)])
    settings = cli_module.load_config(config_path)
    metadata = Path(settings.model.artifact) / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{}")
    monkeypatch.setattr(
        model_module,
        "predict_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ImportError("torch unavailable")),
    )
    result = runner.invoke(
        app,
        ["trace", "replay", str(trace), "--config", str(config_path), "--output", str(output)],
    )
    assert result.exit_code == 0
    assert json.loads(output.read_text())["prediction_mode"] == "heuristic"
    assert "using heuristic predictions" in result.output


def test_trace_replay_applies_configured_path_redaction(tmp_path: Path, config_path: Path):
    value = yaml.safe_load(config_path.read_text())
    value["tracing"]["redact_paths"] = True
    value["tracing"]["hash_salt"] = "test-salt"
    config_path.write_text(yaml.safe_dump(value))
    trace = tmp_path / "trace.jsonl"
    output = tmp_path / "graph.json"
    write_trace(trace, [trace_event("a")])
    result = runner.invoke(
        app,
        ["trace", "replay", str(trace), "--config", str(config_path), "--output", str(output)],
    )
    assert result.exit_code == 0
    resources = json.loads(output.read_text())["resources"]
    assert resources[0]["identifier"].startswith("sha256:")


def test_dataset_build_synthetic_writes_loadable_artifact(tmp_path: Path):
    output = tmp_path / "dataset"
    result = runner.invoke(
        app,
        [
            "dataset",
            "build-synthetic",
            "--profile",
            "quick",
            "--seed",
            "9",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0
    dataset = Dataset.load(output)
    assert dataset.metadata.example_count == len(dataset.examples)
    assert (output / "split.json").exists()


def test_collect_json_uses_stable_test_contract(config_path: Path, monkeypatch):
    tests = [TestIdentity("id-a", "tests/test_a.py::test_a")]
    monkeypatch.setattr(cli_module.PytestAdapter, "collect", lambda _self, _targets: tests)
    result = runner.invoke(app, ["collect", "--config", str(config_path), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["node_id"] == "tests/test_a.py::test_a"


def test_plan_json_contains_every_collected_test(config_path: Path, monkeypatch):
    tests = [
        TestIdentity("id-a", "tests/test_a.py::test_a"),
        TestIdentity("id-b", "tests/test_b.py::test_b"),
    ]
    monkeypatch.setattr(cli_module.PytestAdapter, "collect", lambda _self, _targets: tests)
    result = runner.invoke(app, ["plan", "--config", str(config_path), "--workers", "2", "--json"])
    assert result.exit_code == 0
    value = json.loads(result.stdout)
    assert value["workers"] == 2
    assert {item["test_id"] for item in value["tests"]} == {"id-a", "id-b"}


def test_plan_consumes_predictions_for_collected_tests(config_path: Path, monkeypatch):
    tests = [
        TestIdentity("id-a", "tests/test_a.py::test_a"),
        TestIdentity("id-b", "tests/test_b.py::test_b"),
    ]
    monkeypatch.setattr(cli_module.PytestAdapter, "collect", lambda _self, _targets: tests)
    settings = cli_module.load_config(config_path)
    cli_module.ArtifactStore(Path(settings.artifact_dir)).save(
        "graphs",
        "latest",
        {
            "predictions": [
                {"test_a": "id-a", "test_b": "id-b", "probability": 0.99},
                {"test_a": "stale-a", "test_b": "stale-b", "probability": 1.0},
            ]
        },
    )
    result = runner.invoke(app, ["plan", "--config", str(config_path), "--workers", "2", "--json"])
    assert result.exit_code == 0
    planned = {item["test_id"]: item for item in json.loads(result.stdout)["tests"]}
    assert (
        planned["id-a"]["estimated_end"] <= planned["id-b"]["estimated_start"]
        or planned["id-b"]["estimated_end"] <= planned["id-a"]["estimated_start"]
    )


@pytest.mark.parametrize("command", [["plan", "--workers", "0"], ["run", "--timeout", "0"]])
def test_invalid_execution_limits_are_actionable(command, config_path: Path, monkeypatch):
    monkeypatch.setattr(cli_module.PytestAdapter, "collect", lambda _self, _targets: [])
    result = runner.invoke(app, [*command, "--config", str(config_path)])
    assert result.exit_code == 2
