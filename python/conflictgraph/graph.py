from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional

from .normalize import is_mutating
from .types import (
    AccessMode,
    ConflictCause,
    Operation,
    PairPrediction,
    ResourceIdentity,
    ResourceType,
    TestIdentity,
    TestStats,
    TraceEvent,
    stable_pair,
)


@dataclass
class AccessAggregate:
    test_id: str
    resource: ResourceIdentity
    counts: Counter[Operation] = field(default_factory=Counter)
    modes: set[AccessMode] = field(default_factory=set)
    first_ns: int = 0
    last_ns: int = 0
    sources: set[str] = field(default_factory=set)
    execution_ids: set[str] = field(default_factory=set)

    def observe(self, event: TraceEvent) -> None:
        self.counts[event.operation] += 1
        self.modes.add(event.access_mode)
        self.first_ns = (
            event.timestamp_ns if not self.first_ns else min(self.first_ns, event.timestamp_ns)
        )
        self.last_ns = max(self.last_ns, event.timestamp_ns)
        self.sources.add(event.source.value)
        self.execution_ids.add(event.execution_id)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def mutating(self) -> bool:
        return any(is_mutating(operation, mode) for operation in self.counts for mode in self.modes)

    @property
    def active_ns(self) -> int:
        return max(0, self.last_ns - self.first_ns)

    def vector(self) -> list[float]:
        ordered = list(Operation)
        scale = math.log1p(self.total)
        return [self.counts[operation] / max(1, self.total) for operation in ordered] + [
            scale,
            float(self.mutating),
        ]


@dataclass
class PairFeatures:
    test_a: str
    test_b: str
    shared_resources: int = 0
    shared_files: int = 0
    shared_writes: int = 0
    read_write: int = 0
    write_write: int = 0
    create_create: int = 0
    delete_read: int = 0
    shared_bind: int = 0
    shared_locks: int = 0
    shared_redis_writes: int = 0
    shared_unix_sockets: int = 0
    shared_databases: int = 0
    resource_jaccard: float = 0.0
    minimum_rarity: float = 0.0
    mean_rarity: float = 0.0
    duration_a: float = 1.0
    duration_b: float = 1.0
    failure_rate_a: float = 0.0
    failure_rate_b: float = 0.0
    historical_concurrency: int = 0
    historical_cofailures: int = 0
    temporal_overlap: float = 0.0
    evidence: list[dict[str, Any]] = field(default_factory=list)

    FEATURE_NAMES = (
        "shared_resources",
        "shared_files",
        "shared_writes",
        "read_write",
        "write_write",
        "create_create",
        "delete_read",
        "shared_bind",
        "shared_locks",
        "shared_redis_writes",
        "shared_unix_sockets",
        "shared_databases",
        "resource_jaccard",
        "minimum_rarity",
        "mean_rarity",
        "duration_a",
        "duration_b",
        "failure_rate_a",
        "failure_rate_b",
        "historical_concurrency",
        "historical_cofailures",
        "temporal_overlap",
    )

    def vector(self) -> list[float]:
        return [float(getattr(self, name)) for name in self.FEATURE_NAMES]

    def as_mapping(self) -> dict[str, float]:
        return dict(zip(self.FEATURE_NAMES, self.vector(), strict=True))


class TestResourceGraph:
    """In-memory heterogeneous graph used by data generation and cold-start scoring."""

    __test__ = False

    def __init__(self) -> None:
        self.tests: dict[str, TestIdentity] = {}
        self.resources: dict[str, ResourceIdentity] = {}
        self.edges: dict[tuple[str, str], AccessAggregate] = {}
        self.test_resources: dict[str, set[str]] = defaultdict(set)
        self.resource_tests: dict[str, set[str]] = defaultdict(set)
        self.test_stats: dict[str, TestStats] = defaultdict(TestStats)
        self.pair_history: dict[tuple[str, str], tuple[int, int]] = {}

    def add_test(self, test: TestIdentity, stats: Optional[TestStats] = None) -> None:
        self.tests[test.id] = test
        if stats is not None:
            self.test_stats[test.id] = stats

    def add_event(self, event: TraceEvent) -> None:
        if event.test_id not in self.tests:
            self.tests[event.test_id] = TestIdentity(id=event.test_id, node_id=event.test_id)
        resource = ResourceIdentity.create(event.resource_type, event.resource_identifier)
        self.resources[resource.id] = resource
        key = (event.test_id, resource.id)
        aggregate = self.edges.get(key)
        if aggregate is None:
            aggregate = AccessAggregate(event.test_id, resource)
            self.edges[key] = aggregate
        aggregate.observe(event)
        self.test_resources[event.test_id].add(resource.id)
        self.resource_tests[resource.id].add(event.test_id)

    @classmethod
    def from_events(
        cls, events: Iterable[TraceEvent], tests: Iterable[TestIdentity] = ()
    ) -> "TestResourceGraph":
        graph = cls()
        for test in tests:
            graph.add_test(test)
        for event in events:
            graph.add_event(event)
        graph._refresh_stats()
        return graph

    def _refresh_stats(self) -> None:
        for test_id, resource_ids in self.test_resources.items():
            stats = self.test_stats[test_id]
            stats.resource_count = len(resource_ids)
            total = sum(self.edges[(test_id, rid)].total for rid in resource_ids)
            mutating = sum(
                self.edges[(test_id, rid)].total
                for rid in resource_ids
                if self.edges[(test_id, rid)].mutating
            )
            stats.write_ratio = mutating / total if total else 0.0

    def candidate_pairs(self, include_readonly: bool = False) -> Iterator[tuple[str, str]]:
        emitted: set[tuple[str, str]] = set()
        for resource_id, test_ids in self.resource_tests.items():
            if len(test_ids) < 2:
                continue
            for a, b in itertools.combinations(sorted(test_ids), 2):
                if not include_readonly:
                    left, right = self.edges[(a, resource_id)], self.edges[(b, resource_id)]
                    if not left.mutating and not right.mutating:
                        continue
                pair = (a, b)
                if pair not in emitted:
                    emitted.add(pair)
                    yield pair
        for pair in sorted(self.pair_history):
            if pair not in emitted:
                yield pair

    def pair_features(self, test_a: str, test_b: str) -> PairFeatures:
        test_a, test_b = stable_pair(test_a, test_b)
        left_resources = self.test_resources.get(test_a, set())
        right_resources = self.test_resources.get(test_b, set())
        shared = sorted(left_resources & right_resources)
        union = left_resources | right_resources
        features = PairFeatures(test_a, test_b)
        features.shared_resources = len(shared)
        features.resource_jaccard = len(shared) / len(union) if union else 0.0
        rarities: list[float] = []
        overlaps: list[float] = []
        for resource_id in shared:
            left = self.edges[(test_a, resource_id)]
            right = self.edges[(test_b, resource_id)]
            resource = self.resources[resource_id]
            operations_a, operations_b = set(left.counts), set(right.counts)
            writes_a = left.mutating
            writes_b = right.mutating
            readers_a = Operation.READ in operations_a
            readers_b = Operation.READ in operations_b
            rarity = 1.0 / max(1, len(self.resource_tests[resource_id]))
            rarities.append(rarity)
            if resource.type in {ResourceType.FILE, ResourceType.DIRECTORY}:
                features.shared_files += 1
            if writes_a or writes_b:
                features.shared_writes += 1
            if writes_a and writes_b:
                features.write_write += 1
            if (writes_a and readers_b) or (writes_b and readers_a):
                features.read_write += 1
            if Operation.CREATE in operations_a and Operation.CREATE in operations_b:
                features.create_create += 1
            if (Operation.DELETE in operations_a and readers_b) or (
                Operation.DELETE in operations_b and readers_a
            ):
                features.delete_read += 1
            if Operation.BIND in operations_a and Operation.BIND in operations_b:
                features.shared_bind += 1
            if Operation.LOCK in operations_a and Operation.LOCK in operations_b:
                features.shared_locks += 1
            if resource.type == ResourceType.REDIS_KEY and writes_a and writes_b:
                features.shared_redis_writes += 1
            if resource.type == ResourceType.UNIX_SOCKET:
                features.shared_unix_sockets += 1
            if resource.type == ResourceType.DATABASE_RESOURCE or resource.identifier.endswith(
                (".db", ".sqlite", ".sqlite3")
            ):
                features.shared_databases += 1
            start = max(left.first_ns, right.first_ns)
            end = min(left.last_ns, right.last_ns)
            envelope = max(left.last_ns, right.last_ns) - min(left.first_ns, right.first_ns)
            overlaps.append(max(0, end - start) / envelope if envelope else 0.0)
            features.evidence.append(
                {
                    "resource_id": resource.id,
                    "resource": resource.identifier,
                    "type": resource.type.value,
                    "operations_a": sorted(op.value for op in operations_a),
                    "operations_b": sorted(op.value for op in operations_b),
                    "rarity": rarity,
                }
            )
        if rarities:
            features.minimum_rarity = min(rarities)
            features.mean_rarity = sum(rarities) / len(rarities)
        if overlaps:
            features.temporal_overlap = sum(overlaps) / len(overlaps)
        stats_a, stats_b = self.test_stats[test_a], self.test_stats[test_b]
        features.duration_a, features.duration_b = stats_a.duration_ema, stats_b.duration_ema
        features.failure_rate_a, features.failure_rate_b = (
            stats_a.failure_rate,
            stats_b.failure_rate,
        )
        features.historical_concurrency, features.historical_cofailures = self.pair_history.get(
            (test_a, test_b), (0, 0)
        )
        return features

    def to_dict(self, risk_threshold: float = 0.0) -> dict[str, Any]:
        return {
            "tests": [vars(test) for test in self.tests.values()],
            "resources": [
                {"id": item.id, "type": item.type.value, "identifier": item.identifier}
                for item in self.resources.values()
            ],
            "edges": [
                {
                    "test_id": test_id,
                    "resource_id": resource_id,
                    "counts": {key.value: value for key, value in edge.counts.items()},
                    "modes": sorted(mode.value for mode in edge.modes),
                    "first_ns": edge.first_ns,
                    "last_ns": edge.last_ns,
                }
                for (test_id, resource_id), edge in self.edges.items()
            ],
        }


class HeuristicPredictor:
    """Deterministic semantic baseline and production cold-start fallback."""

    def __init__(self, floor: float = 0.001) -> None:
        self.floor = floor

    def score(self, features: PairFeatures) -> PairPrediction:
        signals = [
            (
                features.shared_bind,
                0.995,
                ConflictCause.PORT_COLLISION,
                "both bind the same endpoint",
            ),
            (
                features.create_create,
                0.97,
                ConflictCause.FILE_COLLISION,
                "both create the same resource",
            ),
            (
                features.shared_locks,
                0.92,
                ConflictCause.DATABASE_LOCK,
                "both lock the same resource",
            ),
            (
                features.shared_unix_sockets,
                0.94,
                ConflictCause.UNIX_SOCKET_COLLISION,
                "share a Unix socket",
            ),
            (
                features.shared_redis_writes,
                0.90,
                ConflictCause.SHARED_STATE,
                "mutate the same Redis key",
            ),
            (
                features.delete_read,
                0.88,
                ConflictCause.FILE_COLLISION,
                "one deletes a resource read by the other",
            ),
            (
                features.write_write,
                0.82,
                ConflictCause.FILE_COLLISION,
                "both mutate the same resource",
            ),
            (
                features.read_write,
                0.65,
                ConflictCause.FILE_COLLISION,
                "one reads a resource mutated by the other",
            ),
        ]
        probability = self.floor
        cause = ConflictCause.UNKNOWN
        reasons: list[str] = []
        for count, weight, candidate_cause, reason in signals:
            if count:
                probability = 1.0 - (1.0 - probability) * ((1.0 - weight) ** count)
                reasons.append(f"{count}x {reason}")
                if cause == ConflictCause.UNKNOWN or weight > 0.9:
                    cause = candidate_cause
        # Common read-only dependencies contribute essentially no risk.
        if features.shared_resources and not reasons:
            probability = min(
                0.02, self.floor + 0.001 * features.shared_resources * features.mean_rarity
            )
            reasons.append(f"{features.shared_resources} shared read-only resource(s)")
        historical_rate = (
            features.historical_cofailures / features.historical_concurrency
            if features.historical_concurrency
            else 0.0
        )
        if historical_rate:
            probability = 1.0 - (1.0 - probability) * (1.0 - min(0.8, historical_rate))
            reasons.append(f"historical concurrent co-failure rate {historical_rate:.1%}")
        resources = [entry["resource"] for entry in features.evidence[:8]]
        explanation = "; ".join(reasons) if reasons else "no meaningful shared mutable resource"
        return PairPrediction(
            features.test_a,
            features.test_b,
            probability,
            cause,
            "heuristic-v1",
            resources,
            explanation,
        )

    def predict_graph(
        self, graph: TestResourceGraph, include_readonly: bool = False
    ) -> list[PairPrediction]:
        return [
            self.score(graph.pair_features(a, b))
            for a, b in graph.candidate_pairs(include_readonly)
        ]
