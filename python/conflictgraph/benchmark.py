from __future__ import annotations

import random
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .dataset import ConflictLabel, DatasetBuilder, pair_disjoint_split
from .errors import ArtifactError
from .graph import HeuristicPredictor, TestResourceGraph
from .metrics import bootstrap_interval, classification_metrics
from .scheduler import ConflictScheduler, RandomScheduler, SerialScheduler
from .types import (
    AccessMode,
    ConflictCause,
    EventSource,
    Operation,
    PairPrediction,
    ResourceType,
    RiskPolicy,
    Schedule,
    TestIdentity,
    TestStats,
    TraceEvent,
    dump_json,
    stable_pair,
)


@dataclass(frozen=True)
class ConflictFamily:
    name: str
    cause: ConflictCause
    resource_type: ResourceType
    operation: Operation
    resource_prefix: str
    pair_count: int
    failure_probability: float
    base_duration: float


FAMILIES = (
    ConflictFamily(
        "tcp-port",
        ConflictCause.PORT_COLLISION,
        ResourceType.TCP_ENDPOINT,
        Operation.BIND,
        "TCP:127.0.0.1:",
        8,
        0.88,
        0.09,
    ),
    ConflictFamily(
        "shared-file",
        ConflictCause.FILE_COLLISION,
        ResourceType.FILE,
        Operation.WRITE,
        "/tmp/conflictgraph/files/",
        12,
        0.70,
        0.07,
    ),
    ConflictFamily(
        "sqlite",
        ConflictCause.DATABASE_LOCK,
        ResourceType.DATABASE_RESOURCE,
        Operation.LOCK,
        "/tmp/conflictgraph/sqlite/",
        10,
        0.64,
        0.12,
    ),
    ConflictFamily(
        "unix-socket",
        ConflictCause.UNIX_SOCKET_COLLISION,
        ResourceType.UNIX_SOCKET,
        Operation.BIND,
        "/tmp/conflictgraph/sockets/",
        6,
        0.82,
        0.08,
    ),
    ConflictFamily(
        "directory",
        ConflictCause.FILE_COLLISION,
        ResourceType.DIRECTORY,
        Operation.DELETE,
        "/tmp/conflictgraph/directories/",
        8,
        0.68,
        0.06,
    ),
    ConflictFamily(
        "redis-key",
        ConflictCause.SHARED_STATE,
        ResourceType.REDIS_KEY,
        Operation.WRITE,
        "0:benchmark:",
        10,
        0.72,
        0.05,
    ),
    ConflictFamily(
        "contention",
        ConflictCause.RESOURCE_CONTENTION,
        ResourceType.OTHER_LOGICAL_RESOURCE,
        Operation.WRITE,
        "CPU_POOL:",
        4,
        0.40,
        0.20,
    ),
)


PROFILE_TESTS = {"quick": 80, "standard": 220, "full": 320}
PROFILE_TRIALS = {"quick": 3, "standard": 12, "full": 32}


@dataclass
class SyntheticWorld:
    tests: list[TestIdentity]
    graph: TestResourceGraph
    labels: list[ConflictLabel]
    probabilities: dict[tuple[str, str], float]
    stats: dict[str, TestStats]


def build_synthetic_world(profile: str = "quick", seed: int = 42) -> SyntheticWorld:
    if profile not in PROFILE_TESTS:
        raise ValueError(f"unknown benchmark profile {profile}")
    randomizer = random.Random(seed)
    total_tests = PROFILE_TESTS[profile]
    tests = [
        TestIdentity.from_pytest_nodeid(
            f"benchmark/suite/test_generated.py::test_case_{index:03d}", "benchmark"
        )
        for index in range(total_tests)
    ]
    graph = TestResourceGraph()
    stats: dict[str, TestStats] = {}
    for test in tests:
        duration = randomizer.uniform(0.035, 0.16)
        stats[test.id] = TestStats(duration_ema=duration, duration_median=duration)
        graph.add_test(test, stats[test.id])
    labels: list[ConflictLabel] = []
    probabilities: dict[tuple[str, str], float] = {}
    cursor = 0
    sequence = 0
    for family in FAMILIES:
        for pair_index in range(family.pair_count):
            left = tests[cursor % total_tests]
            right = tests[(cursor + 1) % total_tests]
            cursor += 2
            resource = family.resource_prefix + str(pair_index)
            for offset, test in enumerate((left, right)):
                operation = family.operation
                mode = (
                    AccessMode.EXCLUSIVE
                    if operation in {Operation.BIND, Operation.LOCK, Operation.DELETE}
                    else AccessMode.WRITE
                )
                graph.add_event(
                    TraceEvent(
                        f"history-{family.name}-{offset}",
                        test.id,
                        1_000_000_000 + sequence * 1000,
                        1000 + sequence,
                        1000 + sequence,
                        10 + sequence,
                        family.resource_type,
                        resource,
                        operation,
                        mode,
                        EventSource.REPLAY,
                        {"family": family.name},
                        sequence,
                    )
                )
                sequence += 1
            pair = stable_pair(left.id, right.id)
            probabilities[pair] = family.failure_probability
            labels.append(ConflictLabel(*pair, True, family.cause, 1.0, "synthetic-ground-truth"))
    # Safe shared-read controls and isolated mutable resources.
    for index, test in enumerate(tests):
        common = f"/opt/benchmark/fixtures/shared_{index % 5}.json"
        graph.add_event(
            TraceEvent(
                f"history-safe-{index}",
                test.id,
                2_000_000_000 + index,
                2000 + index,
                2000 + index,
                200 + index,
                ResourceType.FILE,
                common,
                Operation.READ,
                AccessMode.READ,
                EventSource.REPLAY,
                {},
                sequence + index,
            )
        )
        isolated = f"/tmp/conflictgraph/isolated/{test.id}.db"
        graph.add_event(
            TraceEvent(
                f"history-isolated-{index}",
                test.id,
                2_100_000_000 + index,
                3000 + index,
                3000 + index,
                300 + index,
                ResourceType.FILE,
                isolated,
                Operation.WRITE,
                AccessMode.WRITE,
                EventSource.REPLAY,
                {},
                sequence + total_tests + index,
            )
        )
    graph._refresh_stats()
    return SyntheticWorld(tests, graph, labels, probabilities, stats)


@dataclass
class TrialResult:
    scheduler: str
    trial: int
    seed: int
    workers: int
    tests: int
    failures: int
    conflict_failures: int
    flake_rate: float
    makespan_seconds: float
    worker_utilization: float
    average_active_workers: float
    expected_risk_exposure: float
    conflicting_overlaps: int
    unnecessary_serialization_rate: float
    scheduler_overhead_ms: float


def simulate_schedule(
    schedule: Schedule, world: SyntheticWorld, trial: int, seed: int, scheduler_name: str
) -> TrialResult:
    randomizer = random.Random(seed)
    failures: set[str] = set()
    conflict_failures = 0
    conflicting_overlaps = 0
    risk_exposure = 0.0
    tests = schedule.tests
    for index, left in enumerate(tests):
        for right in tests[index + 1 :]:
            if left.worker == right.worker:
                continue
            overlap = max(
                0.0,
                min(left.estimated_end, right.estimated_end)
                - max(left.estimated_start, right.estimated_start),
            )
            if overlap <= 0:
                continue
            probability = world.probabilities.get(stable_pair(left.test_id, right.test_id), 0.0)
            if probability:
                conflicting_overlaps += 1
                exposure = min(
                    1.0,
                    overlap / max(0.001, min(left.estimated_duration, right.estimated_duration)),
                )
                risk_exposure += probability * exposure
                if randomizer.random() < probability * exposure:
                    victim = left.test_id if randomizer.random() < 0.5 else right.test_id
                    if victim not in failures:
                        conflict_failures += 1
                    failures.add(victim)
    makespan = max((item.estimated_end for item in tests), default=0.0)
    work = sum(item.estimated_duration for item in tests)
    utilization = work / (makespan * schedule.workers) if makespan else 0.0
    average_active = work / makespan if makespan else 0.0
    ideal = work / schedule.workers if schedule.workers else work
    serialization = max(0.0, makespan - ideal) / makespan if makespan else 0.0
    return TrialResult(
        scheduler_name,
        trial,
        seed,
        schedule.workers,
        len(tests),
        len(failures),
        conflict_failures,
        len(failures) / len(tests) if tests else 0.0,
        makespan,
        utilization,
        average_active,
        risk_exposure,
        conflicting_overlaps,
        serialization,
        schedule.scheduler_latency_ms,
    )


@dataclass
class AggregateResult:
    scheduler: str
    trials: int
    executions: int
    failures: int
    conflict_failures: int
    flake_rate: float
    flake_rate_ci95: tuple[float, float]
    mean_makespan_seconds: float
    makespan_ci95: tuple[float, float]
    worker_utilization: float
    average_active_workers: float
    mean_risk_exposure: float
    mean_scheduler_overhead_ms: float


def aggregate_trials(trials: Sequence[TrialResult], seed: int = 42) -> AggregateResult:
    if not trials:
        raise ValueError("cannot aggregate an empty trial set")
    rates = [trial.flake_rate for trial in trials]
    makespans = [trial.makespan_seconds for trial in trials]
    return AggregateResult(
        trials[0].scheduler,
        len(trials),
        sum(item.tests for item in trials),
        sum(item.failures for item in trials),
        sum(item.conflict_failures for item in trials),
        statistics.mean(rates),
        bootstrap_interval(rates, seed),
        statistics.mean(makespans),
        bootstrap_interval(makespans, seed),
        statistics.mean(item.worker_utilization for item in trials),
        statistics.mean(item.average_active_workers for item in trials),
        statistics.mean(item.expected_risk_exposure for item in trials),
        statistics.mean(item.scheduler_overhead_ms for item in trials),
    )


@dataclass
class BenchmarkReport:
    version: str
    created_at: str
    profile: str
    seed: int
    workers: int
    trial_count: int
    total_test_executions: int
    results: list[AggregateResult]
    model_metrics: dict[str, Any]
    impact_summary: str
    environment: dict[str, Any]
    replayed: bool = True


def run_replay_benchmark(
    profile: str = "quick",
    workers: int = 4,
    seed: int = 42,
    trials: Optional[int] = None,
    model_artifact: Optional[Path] = Path("artifacts/model"),
) -> BenchmarkReport:
    world = build_synthetic_world(profile, seed)
    trial_count = trials or PROFILE_TRIALS[profile]
    heuristic_predictions = HeuristicPredictor().predict_graph(world.graph, include_readonly=True)
    # An oracle-calibrated synthetic stand-in is labeled explicitly as such; real GNN results
    # are only added by the training workflow and are never fabricated here.
    label_by_pair = {label.key: label for label in world.labels}
    oracle_predictions = []
    for prediction in heuristic_predictions:
        label = label_by_pair.get(prediction.key)
        probability = world.probabilities.get(prediction.key, prediction.probability * 0.75)
        oracle_predictions.append(
            PairPrediction(
                prediction.test_a,
                prediction.test_b,
                probability,
                label.cause if label else prediction.cause,
                "synthetic-oracle",
                prediction.shared_resources,
                prediction.explanation,
            )
        )
    dataset = DatasetBuilder(seed).build(world.graph, world.labels)
    split = pair_disjoint_split(dataset, seed)
    y = [example.label for example in dataset.examples]
    heuristic_map = {item.key: item.probability for item in heuristic_predictions}
    heuristic_probabilities = [
        heuristic_map.get(stable_pair(item.test_a, item.test_b), 0.001) for item in dataset.examples
    ]
    model_metrics = {"heuristic": asdict(classification_metrics(y, heuristic_probabilities))}
    strategies: dict[str, Any] = {
        "serial": lambda run: SerialScheduler().schedule(world.tests, world.stats, run),
        "random": lambda run: RandomScheduler(workers, seed + run).schedule(
            world.tests, world.stats, str(run)
        ),
        "heuristic": lambda run: ConflictScheduler(
            workers, RiskPolicy.BALANCED, seed + run
        ).static_schedule(world.tests, world.stats, heuristic_predictions, str(run)),
        "conservative-locking": lambda run: ConflictScheduler(
            workers, RiskPolicy.SAFE, seed + run, hard_threshold=0.05
        ).static_schedule(world.tests, world.stats, heuristic_predictions, str(run)),
        "oracle-upper-bound": lambda run: ConflictScheduler(
            workers, RiskPolicy.BALANCED, seed + run
        ).static_schedule(world.tests, world.stats, oracle_predictions, str(run)),
    }
    try:
        from .model import TabularBaseline, pair_predictions, predict_artifact

        tabular = TabularBaseline("gradient_boosting", seed)
        tabular_metrics = tabular.fit(dataset, split)
        tabular_probabilities = tabular.predict(dataset.arrays()[0])
        tabular_predictions = pair_predictions(
            dataset, tabular_probabilities, heuristic_predictions, "tabular-gradient-boosting"
        )
        strategies["tabular-ml"] = lambda run: ConflictScheduler(
            workers, RiskPolicy.BALANCED, seed + run
        ).static_schedule(world.tests, world.stats, tabular_predictions, str(run))
        model_metrics["tabular"] = {key: asdict(value) for key, value in tabular_metrics.items()}
        if model_artifact and (model_artifact / "metadata.json").exists():
            gnn_probabilities, metadata = predict_artifact(model_artifact, world.graph, dataset)
            gnn_predictions = pair_predictions(
                dataset, gnn_probabilities, heuristic_predictions, metadata.version
            )
            strategies["gnn"] = lambda run: ConflictScheduler(
                workers, RiskPolicy.BALANCED, seed + run
            ).static_schedule(world.tests, world.stats, gnn_predictions, str(run))
            model_metrics["gnn"] = {
                "version": metadata.version,
                "validation": metadata.validation_metrics,
                "test": metadata.test_metrics,
            }
        else:
            model_metrics["gnn"] = {"available": False, "reason": "no artifact installed"}
    except (ImportError, ArtifactError) as exc:
        model_metrics["ml_schedulers"] = {"available": False, "reason": str(exc)}
    grouped: dict[str, list[TrialResult]] = {name: [] for name in strategies}
    for name, create_schedule in strategies.items():
        for trial in range(trial_count):
            schedule = create_schedule(trial)
            grouped[name].append(
                simulate_schedule(schedule, world, trial, seed + trial * 104729, name)
            )
    aggregates = [aggregate_trials(grouped[name], seed) for name in strategies]
    by_name = {item.scheduler: item for item in aggregates}
    random_result, intelligent = by_name["random"], by_name["heuristic"]
    reduction = (
        100 * (1 - intelligent.flake_rate / random_result.flake_rate)
        if random_result.flake_rate
        else 0.0
    )
    throughput = (
        100 * random_result.mean_makespan_seconds / intelligent.mean_makespan_seconds
        if intelligent.mean_makespan_seconds
        else 100.0
    )
    total = sum(item.executions for item in aggregates)
    summary = (
        f"Heuristic ConflictGraph replay reduced conflict-induced failures by {reduction:.1f}% "
        f"while retaining {throughput:.1f}% of random-parallel throughput across {total:,} simulated "
        "test executions. This is an offline replay result, not an eBPF runtime measurement."
    )
    return BenchmarkReport(
        "benchmark-v1",
        datetime.now(timezone.utc).isoformat(),
        profile,
        seed,
        workers,
        trial_count,
        total,
        aggregates,
        model_metrics,
        summary,
        {
            "mode": "synthetic-replay",
            "tests": len(world.tests),
            "conflict_pairs": len(world.labels),
        },
        True,
    )


def write_report(report: BenchmarkReport, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    dump_json(report, directory / "benchmark.json")
    lines = [
        "# ConflictGraph benchmark report",
        "",
        f"Generated: {report.created_at}",
        "",
        f"> {report.impact_summary}",
        "",
        "| Scheduler | Flake rate | Makespan | Utilization | Conflict failures | Scheduler overhead |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for result in report.results:
        lines.append(
            f"| {result.scheduler} | {result.flake_rate:.2%} [{result.flake_rate_ci95[0]:.2%}, {result.flake_rate_ci95[1]:.2%}] "
            f"| {result.mean_makespan_seconds:.3f}s | {result.worker_utilization:.1%} | {result.conflict_failures} "
            f"| {result.mean_scheduler_overhead_ms:.2f}ms |"
        )
    lines += [
        "",
        "## Provenance",
        "",
        f"- Profile: `{report.profile}`",
        f"- Seed: `{report.seed}`",
        f"- Workers: `{report.workers}`",
        f"- Trials per scheduler: `{report.trial_count}`",
        f"- Total simulated executions: `{report.total_test_executions}`",
        "- Replay results estimate schedule behavior from recorded durations and known conflict probabilities.",
        "- They are intentionally not presented as measured test-process or tracing overhead.",
        "",
    ]
    (directory / "benchmark.md").write_text("\n".join(lines))
