from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

import yaml

from .errors import ConfigurationError
from .types import RiskPolicy


@dataclass
class TracingConfig:
    mode: str = "replay"
    redact_paths: bool = False
    hash_salt: str = ""
    exclude_prefixes: list[str] = field(
        default_factory=lambda: ["/proc", "/sys", "/usr/lib", "/System"]
    )
    minimum_quality: float = 0.8
    cgroup_root: str = "/sys/fs/cgroup/conflictgraph"
    control_socket: str = "/run/conflictgraph/tracer.sock"


@dataclass
class SchedulerConfig:
    risk_weight: float = 2.0
    hard_threshold: float = 0.9
    refinement_rounds: int = 4


@dataclass
class ModelConfig:
    artifact: str = "artifacts/model"
    default_no_edge_risk: float = 0.001


@dataclass
class Config:
    version: int = 1
    workers: int = 4
    risk_policy: RiskPolicy = RiskPolicy.BALANCED
    seed: int = 42
    artifact_dir: str = "artifacts"
    database_url: str = "postgresql://conflictgraph:conflictgraph@localhost:5432/conflictgraph"
    api_address: str = "http://localhost:8080"
    tracing: TracingConfig = field(default_factory=TracingConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    model: ModelConfig = field(default_factory=ModelConfig)

    def validate(self) -> None:
        problems: list[str] = []
        if self.version != 1:
            problems.append(f"unsupported configuration version {self.version}")
        if not 1 <= self.workers <= 1024:
            problems.append("workers must be between 1 and 1024")
        if self.tracing.mode not in {"ebpf", "replay", "disabled"}:
            problems.append("tracing.mode must be ebpf, replay, or disabled")
        if self.tracing.mode == "ebpf" and platform.system() != "Linux":
            problems.append("real eBPF tracing is Linux-only; select replay mode on this host")
        if self.tracing.redact_paths and not self.tracing.hash_salt:
            problems.append("tracing.hash_salt is required when path redaction is enabled")
        if not 0 <= self.tracing.minimum_quality <= 1:
            problems.append("tracing.minimum_quality must be in [0, 1]")
        if self.scheduler.risk_weight < 0:
            problems.append("scheduler.risk_weight must be non-negative")
        if not 0 <= self.scheduler.hard_threshold <= 1:
            problems.append("scheduler.hard_threshold must be in [0, 1]")
        if self.scheduler.refinement_rounds < 0:
            problems.append("scheduler.refinement_rounds must be non-negative")
        if not 0 <= self.model.default_no_edge_risk <= 1:
            problems.append("model.default_no_edge_risk must be in [0, 1]")
        if not self.artifact_dir:
            problems.append("artifact_dir is required")
        if not self.model.artifact:
            problems.append("model.artifact is required")
        if not self.database_url:
            problems.append("database_url is required")
        if not self.api_address:
            problems.append("api_address is required")
        if problems:
            raise ConfigurationError("Invalid configuration:\n- " + "\n- ".join(problems))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Config":
        allowed = {
            "version",
            "workers",
            "risk_policy",
            "seed",
            "artifact_dir",
            "database_url",
            "api_address",
            "tracing",
            "scheduler",
            "model",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ConfigurationError(f"Unknown configuration field: {unknown[0]}")
        try:
            config = cls(
                version=int(value.get("version", 1)),
                workers=int(value.get("workers", 4)),
                risk_policy=RiskPolicy(value.get("risk_policy", "balanced")),
                seed=int(value.get("seed", 42)),
                artifact_dir=str(value.get("artifact_dir", "artifacts")),
                database_url=str(value.get("database_url", cls.database_url)),
                api_address=str(value.get("api_address", cls.api_address)),
                tracing=TracingConfig(**value.get("tracing", {})),
                scheduler=SchedulerConfig(**value.get("scheduler", {})),
                model=ModelConfig(**value.get("model", {})),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Could not parse configuration: {exc}") from exc
        config.apply_environment()
        config.validate()
        return config

    def apply_environment(self) -> None:
        if value := os.getenv("CONFLICTGRAPH_DATABASE_URL"):
            self.database_url = value
        if value := os.getenv("CONFLICTGRAPH_API_ADDRESS"):
            self.api_address = value
        if value := os.getenv("CONFLICTGRAPH_WORKERS"):
            try:
                self.workers = int(value)
            except ValueError as exc:
                raise ConfigurationError("CONFLICTGRAPH_WORKERS must be an integer") from exc
        if value := os.getenv("CONFLICTGRAPH_MODEL_ARTIFACT"):
            self.model.artifact = value
        if value := os.getenv("CONFLICTGRAPH_TRACING_MODE"):
            self.tracing.mode = value
        if value := os.getenv("CONFLICTGRAPH_HASH_SALT"):
            self.tracing.hash_salt = value


def load_config(path: Path | str = "conflictgraph.yaml") -> Config:
    config_path = Path(path)
    source_name = str(config_path)
    if config_path.exists():
        raw_text = config_path.read_text()
    else:
        fallback = Path("conflictgraph.example.yaml")
        if config_path.name == "conflictgraph.yaml" and fallback.exists():
            config_path = fallback
            source_name = str(config_path)
            raw_text = config_path.read_text()
        elif config_path.name == "conflictgraph.yaml" and config_path == Path(config_path.name):
            template = files("conflictgraph").joinpath("conflictgraph.example.yaml")
            try:
                raw_text = template.read_text()
            except FileNotFoundError as exc:
                raise ConfigurationError(f"Configuration file not found: {config_path}") from exc
            source_name = "packaged conflictgraph.example.yaml"
        else:
            raise ConfigurationError(f"Configuration file not found: {config_path}")
    try:
        raw = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {source_name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Top level of {source_name} must be an object")
    return Config.from_mapping(raw)
