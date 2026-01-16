from importlib.resources import files
from pathlib import Path

import pytest
from conflictgraph.config import Config, load_config
from conflictgraph.errors import ConfigurationError
from conflictgraph.types import RiskPolicy


def test_default_config_validates():
    Config().validate()


def test_mapping_constructs_nested_types():
    value = Config.from_mapping(
        {
            "version": 1,
            "workers": 8,
            "risk_policy": "safe",
            "tracing": {"mode": "disabled", "minimum_quality": 0.9},
            "scheduler": {"risk_weight": 3, "hard_threshold": 0.8},
        }
    )
    assert value.workers == 8
    assert value.risk_policy is RiskPolicy.SAFE
    assert value.scheduler.risk_weight == 3
    assert value.tracing.minimum_quality == 0.9


@pytest.mark.parametrize("field,value", [("workers", 0), ("workers", 1025)])
def test_invalid_worker_count(field, value):
    with pytest.raises(ConfigurationError, match="workers"):
        Config.from_mapping({field: value})


def test_unknown_tracing_mode_rejected():
    with pytest.raises(ConfigurationError, match=r"tracing\.mode"):
        Config.from_mapping({"tracing": {"mode": "pretend-ebpf"}})


def test_unknown_top_level_field_rejected():
    with pytest.raises(ConfigurationError, match="Unknown configuration field"):
        Config.from_mapping({"worker": 8})


@pytest.mark.parametrize(
    "value",
    [
        {"scheduler": {"refinement_rounds": -1}},
        {"model": {"default_no_edge_risk": 2}},
        {"artifact_dir": ""},
        {"database_url": ""},
        {"api_address": ""},
    ],
)
def test_invalid_runtime_paths_and_parameters_rejected(value):
    with pytest.raises(ConfigurationError):
        Config.from_mapping(value)


def test_redaction_requires_salt():
    with pytest.raises(ConfigurationError, match="hash_salt"):
        Config.from_mapping(
            {"tracing": {"mode": "disabled", "redact_paths": True, "hash_salt": ""}}
        )


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("CONFLICTGRAPH_WORKERS", "11")
    monkeypatch.setenv("CONFLICTGRAPH_DATABASE_URL", "postgresql://example/test")
    monkeypatch.setenv("CONFLICTGRAPH_HASH_SALT", "environment-salt")
    config = Config.from_mapping({})
    assert config.workers == 11
    assert config.database_url == "postgresql://example/test"
    assert config.tracing.hash_salt == "environment-salt"


def test_invalid_environment_override(monkeypatch):
    monkeypatch.setenv("CONFLICTGRAPH_WORKERS", "many")
    with pytest.raises(ConfigurationError, match="integer"):
        Config.from_mapping({})


def test_load_yaml(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nworkers: 3\nrisk_policy: aggressive\ntracing: {mode: disabled}\n")
    config = load_config(path)
    assert config.workers == 3
    assert config.risk_policy is RiskPolicy.AGGRESSIVE


def test_missing_config_reports_path(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_packaged_template_matches_repository_template():
    packaged = files("conflictgraph").joinpath("conflictgraph.example.yaml").read_text()
    repository = Path(__file__).parents[1].joinpath("conflictgraph.example.yaml").read_text()
    assert packaged == repository


def test_default_config_uses_packaged_template_outside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    config = load_config()
    assert config.version == 1
    assert config.tracing.mode == "replay"
