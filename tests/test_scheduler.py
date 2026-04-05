import random

import pytest
from conflictgraph.scheduler import (
    ConflictScheduler,
    RandomScheduler,
    SerialScheduler,
)
from conflictgraph.types import PairPrediction, RiskPolicy, TestIdentity, TestStats


def suite(count=6):
    tests = [TestIdentity(f"t{index}", f"test_suite.py::test_{index}") for index in range(count)]
    stats = {
        test.id: TestStats(duration_ema=float(index % 3 + 1)) for index, test in enumerate(tests)
    }
    return tests, stats


def overlaps(left, right):
    return left.worker != right.worker and max(left.estimated_start, right.estimated_start) < min(
        left.estimated_end, right.estimated_end
    )


def test_serial_schedule_preserves_all_tests():
    tests, stats = suite()
    schedule = SerialScheduler().schedule(tests, stats)
    assert schedule.workers == 1
    assert [item.test_id for item in schedule.tests] == [item.id for item in tests]
    assert schedule.expected_makespan == sum(stats[item.id].duration_ema for item in tests)


def test_random_schedule_is_seed_deterministic():
    tests, stats = suite(20)
    left = RandomScheduler(4, 10).schedule(tests, stats)
    right = RandomScheduler(4, 10).schedule(tests, stats)
    assert [(x.test_id, x.worker) for x in left.tests] == [
        (x.test_id, x.worker) for x in right.tests
    ]


def test_conflict_scheduler_separates_high_risk_pair():
    tests, stats = suite(4)
    predictions = [PairPrediction("t0", "t1", 0.99, explanation="same port")]
    schedule = ConflictScheduler(2, RiskPolicy.BALANCED, 42).static_schedule(
        tests, stats, predictions
    )
    by_id = {item.test_id: item for item in schedule.tests}
    assert not overlaps(by_id["t0"], by_id["t1"])


def test_low_risk_pair_may_overlap_to_retain_parallelism():
    tests = [TestIdentity("a", "a"), TestIdentity("b", "b")]
    stats = {test.id: TestStats(duration_ema=2) for test in tests}
    schedule = ConflictScheduler(2, RiskPolicy.BALANCED, 1).static_schedule(
        tests, stats, [PairPrediction("a", "b", 0.01)]
    )
    assert overlaps(schedule.tests[0], schedule.tests[1])
    assert schedule.expected_makespan == 2


def test_longest_processing_tests_begin_early():
    tests, stats = suite(9)
    schedule = ConflictScheduler(3, seed=1).static_schedule(tests, stats, [])
    first = [item for item in schedule.tests if item.estimated_start == 0]
    assert len(first) == 3
    assert all(item.estimated_duration == 3 for item in first)


def test_every_test_scheduled_once_and_valid_worker():
    tests, stats = suite(100)
    randomizer = random.Random(3)
    predictions = [
        PairPrediction(
            *sorted(randomizer.sample([test.id for test in tests], 2)), randomizer.random()
        )
        for _ in range(100)
    ]
    schedule = ConflictScheduler(8, seed=3).static_schedule(tests, stats, predictions)
    assert len({item.test_id for item in schedule.tests}) == 100
    assert all(0 <= item.worker < 8 for item in schedule.tests)


@pytest.mark.parametrize(
    "policy,weight",
    [(RiskPolicy.AGGRESSIVE, 0.55), (RiskPolicy.BALANCED, 2), (RiskPolicy.SAFE, 7.5)],
)
def test_policy_parameters(policy, weight):
    assert ConflictScheduler(2, policy).params.risk_weight == weight


def test_invalid_workers_rejected():
    with pytest.raises(ValueError, match="positive"):
        ConflictScheduler(0)
