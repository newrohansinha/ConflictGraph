from pathlib import Path

import pytest
from conflictgraph.benchmark import (
    PROFILE_TESTS,
    aggregate_trials,
    build_synthetic_world,
    run_replay_benchmark,
    simulate_schedule,
    write_report,
)
from conflictgraph.graph import HeuristicPredictor
from conflictgraph.scheduler import ConflictScheduler, RandomScheduler, SerialScheduler
from conflictgraph.types import RiskPolicy


def test_synthetic_profiles_have_declared_size():
    for profile, count in PROFILE_TESTS.items():
        assert len(build_synthetic_world(profile).tests) == count


def test_world_contains_positive_ground_truth_and_safe_controls():
    world = build_synthetic_world("quick")
    assert len(world.labels) > 40
    assert len(world.graph.resources) > len(world.labels)
    assert all(label.conflict for label in world.labels)


def test_synthetic_world_is_deterministic():
    left, right = build_synthetic_world("quick", 99), build_synthetic_world("quick", 99)
    assert left.probabilities == right.probabilities
    assert [(test.id, left.stats[test.id].duration_ema) for test in left.tests] == [
        (test.id, right.stats[test.id].duration_ema) for test in right.tests
    ]


def test_serial_simulation_has_no_conflict_failure():
    world = build_synthetic_world("quick")
    schedule = SerialScheduler().schedule(world.tests, world.stats)
    result = simulate_schedule(schedule, world, 0, 42, "serial")
    assert result.failures == 0
    assert result.conflicting_overlaps == 0
    assert result.worker_utilization == 1


def test_risk_scheduler_reduces_expected_exposure():
    world = build_synthetic_world("quick")
    predictions = HeuristicPredictor().predict_graph(world.graph)
    intelligent = ConflictScheduler(4, RiskPolicy.BALANCED).static_schedule(
        world.tests, world.stats, predictions
    )
    random = RandomScheduler(4, 42).schedule(world.tests, world.stats)
    intelligent_result = simulate_schedule(intelligent, world, 0, 42, "heuristic")
    random_result = simulate_schedule(random, world, 0, 42, "random")
    assert intelligent_result.expected_risk_exposure < random_result.expected_risk_exposure


def test_aggregate_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        aggregate_trials([])


def test_replay_report_uses_measured_values(tmp_path: Path):
    report = run_replay_benchmark("quick", 4, 7, trials=2)
    assert report.total_test_executions == len(report.results) * 2 * PROFILE_TESTS["quick"]
    assert report.replayed is True
    assert "offline replay" in report.impact_summary.lower()
    write_report(report, tmp_path)
    assert (tmp_path / "benchmark.json").exists()
    markdown = (tmp_path / "benchmark.md").read_text()
    assert "Random" in markdown or "random" in markdown
    assert "not presented as measured" in markdown


def test_unknown_profile_rejected():
    with pytest.raises(ValueError, match="unknown"):
        build_synthetic_world("enormous")
