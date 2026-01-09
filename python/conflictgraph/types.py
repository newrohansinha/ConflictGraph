from __future__ import annotations

import enum
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResourceType(str, enum.Enum):
    FILE = "FILE"
    DIRECTORY = "DIRECTORY"
    TCP_ENDPOINT = "TCP_ENDPOINT"
    UDP_ENDPOINT = "UDP_ENDPOINT"
    UNIX_SOCKET = "UNIX_SOCKET"
    FILE_LOCK = "FILE_LOCK"
    PROCESS = "PROCESS"
    REDIS_KEY = "REDIS_KEY"
    DATABASE_RESOURCE = "DATABASE_RESOURCE"
    OTHER_LOGICAL_RESOURCE = "OTHER_LOGICAL_RESOURCE"


class Operation(str, enum.Enum):
    READ = "READ"
    WRITE = "WRITE"
    CREATE = "CREATE"
    DELETE = "DELETE"
    LOCK = "LOCK"
    RENAME = "RENAME"
    BIND = "BIND"
    LISTEN = "LISTEN"
    CONNECT = "CONNECT"
    EXEC = "EXEC"
    SPAWN = "SPAWN"
    EXIT = "EXIT"


class AccessMode(str, enum.Enum):
    READ = "READ"
    WRITE = "WRITE"
    EXCLUSIVE = "EXCLUSIVE"
    SHARED = "SHARED"
    EXECUTE = "EXECUTE"
    UNKNOWN = "UNKNOWN"


class EventSource(str, enum.Enum):
    EBPF = "EBPF"
    REPLAY = "REPLAY"
    REDIS_ADAPTER = "REDIS_ADAPTER"
    PYTEST_PLUGIN = "PYTEST_PLUGIN"


class ConflictCause(str, enum.Enum):
    FILE_COLLISION = "FILE_COLLISION"
    PORT_COLLISION = "PORT_COLLISION"
    FILE_LOCK = "FILE_LOCK"
    DATABASE_LOCK = "DATABASE_LOCK"
    UNIX_SOCKET_COLLISION = "UNIX_SOCKET_COLLISION"
    SHARED_STATE = "SHARED_STATE"
    RESOURCE_CONTENTION = "RESOURCE_CONTENTION"
    UNKNOWN = "UNKNOWN"


class RiskPolicy(str, enum.Enum):
    AGGRESSIVE = "aggressive"
    BALANCED = "balanced"
    SAFE = "safe"


class ExecutionStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    INFRA_ERROR = "INFRA_ERROR"


@dataclass(frozen=True)
class TestIdentity:
    __test__ = False

    id: str
    node_id: str
    repository: str = "local"
    suite: str = "pytest"
    framework: str = "pytest"
    test_file: str = ""
    test_class: str = ""
    test_function: str = ""
    parameters: str = ""
    source_revision: str = "unknown"

    @classmethod
    def from_pytest_nodeid(
        cls, node_id: str, repository: str = "local", revision: str = "unknown"
    ) -> "TestIdentity":
        parts = node_id.split("::")
        test_file = parts[0]
        test_class = parts[1] if len(parts) > 2 else ""
        raw_function = parts[-1] if len(parts) > 1 else parts[0]
        function, _, params = raw_function.partition("[")
        params = params[:-1] if params.endswith("]") else params
        stable = f"{repository}\0pytest\0{node_id}"
        digest = hashlib.sha256(stable.encode()).hexdigest()[:24]
        return cls(
            id=f"test_{digest}",
            node_id=node_id,
            repository=repository,
            test_file=test_file,
            test_class=test_class,
            test_function=function,
            parameters=params,
            source_revision=revision,
        )


@dataclass(frozen=True)
class ResourceIdentity:
    id: str
    type: ResourceType
    identifier: str
    redacted: bool = False

    @classmethod
    def create(
        cls, resource_type: ResourceType, identifier: str, redacted: bool = False
    ) -> "ResourceIdentity":
        digest = hashlib.sha256(f"{resource_type.value}\0{identifier}".encode()).hexdigest()[:24]
        return cls(f"res_{digest}", resource_type, identifier, redacted)


@dataclass(frozen=True)
class TraceEvent:
    execution_id: str
    test_id: str
    timestamp_ns: int
    pid: int
    tid: int
    cgroup_id: int
    resource_type: ResourceType
    resource_identifier: str
    operation: Operation
    access_mode: AccessMode
    source: EventSource
    metadata: Mapping[str, Any] = field(default_factory=dict)
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("resource_type", "operation", "access_mode", "source"):
            value[key] = getattr(self, key).value
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TraceEvent":
        return cls(
            execution_id=str(value["execution_id"]),
            test_id=str(value["test_id"]),
            timestamp_ns=int(value["timestamp_ns"]),
            pid=int(value.get("pid", 0)),
            tid=int(value.get("tid", value.get("pid", 0))),
            cgroup_id=int(value.get("cgroup_id", 0)),
            resource_type=ResourceType(value["resource_type"]),
            resource_identifier=str(value["resource_identifier"]),
            operation=Operation(value["operation"]),
            access_mode=AccessMode(value["access_mode"]),
            source=EventSource(value.get("source", "REPLAY")),
            metadata=value.get("metadata", {}),
            sequence=int(value.get("sequence", 0)),
        )


@dataclass
class TraceQuality:
    captured: int = 0
    processed: int = 0
    dropped: int = 0
    unattributed: int = 0
    parse_failures: int = 0

    @property
    def completeness(self) -> float:
        denominator = self.captured + self.dropped
        return self.processed / denominator if denominator else 1.0

    @property
    def attribution_rate(self) -> float:
        return 1.0 - self.unattributed / self.processed if self.processed else 1.0

    @property
    def score(self) -> float:
        return max(0.0, min(1.0, self.completeness * self.attribution_rate))


@dataclass
class TestStats:
    __test__ = False

    duration_ema: float = 1.0
    duration_median: float = 1.0
    failure_rate: float = 0.0
    executions: int = 0
    process_count: int = 1
    resource_count: int = 0
    write_ratio: float = 0.0


@dataclass
class PairPrediction:
    test_a: str
    test_b: str
    probability: float
    cause: ConflictCause = ConflictCause.UNKNOWN
    model_version: str = "heuristic"
    shared_resources: list[str] = field(default_factory=list)
    explanation: str = ""
    predicted_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.test_b < self.test_a:
            self.test_a, self.test_b = self.test_b, self.test_a
        self.probability = max(0.0, min(1.0, float(self.probability)))

    @property
    def key(self) -> tuple[str, str]:
        return self.test_a, self.test_b


@dataclass
class ScheduledTest:
    test_id: str
    node_id: str
    worker: int
    estimated_start: float
    estimated_end: float
    estimated_duration: float
    risk_cost: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class Schedule:
    id: str
    run_id: str
    workers: int
    policy: RiskPolicy
    tests: list[ScheduledTest]
    scheduler_latency_ms: float
    expected_makespan: float
    expected_risk: float
    seed: int

    @classmethod
    def empty(
        cls,
        run_id: Optional[str] = None,
        workers: int = 1,
        policy: RiskPolicy = RiskPolicy.BALANCED,
        seed: int = 42,
    ) -> "Schedule":
        return cls(
            str(uuid.uuid4()), run_id or str(uuid.uuid4()), workers, policy, [], 0.0, 0.0, 0.0, seed
        )


@dataclass
class ExecutionResult:
    execution_id: str
    run_id: str
    test_id: str
    node_id: str
    worker: int
    status: ExecutionStatus
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    exit_code: int
    stdout: str
    stderr: str
    failure_message: str = ""
    timed_out: bool = False


def json_default(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def dump_json(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, default=json_default, indent=2, sort_keys=True) + "\n")


def stable_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)
