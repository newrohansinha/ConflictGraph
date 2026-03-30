from __future__ import annotations

import heapq
import random
import time
import uuid
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from .types import (
    PairPrediction,
    RiskPolicy,
    Schedule,
    ScheduledTest,
    TestIdentity,
    TestStats,
    stable_pair,
)


@dataclass(frozen=True)
class PolicyParameters:
    risk_weight: float
    hard_threshold: float
    utilization_bias: float
    lookahead: int


POLICIES: dict[RiskPolicy, PolicyParameters] = {
    RiskPolicy.AGGRESSIVE: PolicyParameters(0.55, 0.985, 1.5, 8),
    RiskPolicy.BALANCED: PolicyParameters(2.0, 0.90, 1.0, 16),
    RiskPolicy.SAFE: PolicyParameters(7.5, 0.68, 0.5, 32),
}


class RiskIndex:
    def __init__(self, predictions: Iterable[PairPrediction]) -> None:
        self._risks: dict[tuple[str, str], PairPrediction] = {
            item.key: item for item in predictions
        }

    def probability(self, left: str, right: str) -> float:
        if left == right:
            return 1.0
        prediction = self._risks.get(stable_pair(left, right))
        return prediction.probability if prediction else 0.0

    def prediction(self, left: str, right: str) -> Optional[PairPrediction]:
        return self._risks.get(stable_pair(left, right))


class ConflictScheduler:
    """Duration-aware list scheduler with conflict costs and deterministic refinement."""

    def __init__(
        self,
        workers: int,
        policy: RiskPolicy = RiskPolicy.BALANCED,
        seed: int = 42,
        risk_weight: Optional[float] = None,
        hard_threshold: Optional[float] = None,
        refinement_rounds: int = 4,
    ) -> None:
        if workers <= 0:
            raise ValueError("workers must be positive")
        self.workers = workers
        self.policy = policy
        self.seed = seed
        base = POLICIES[policy]
        self.params = PolicyParameters(
            risk_weight if risk_weight is not None else base.risk_weight,
            hard_threshold if hard_threshold is not None else base.hard_threshold,
            base.utilization_bias,
            base.lookahead,
        )
        self.refinement_rounds = refinement_rounds

    def static_schedule(
        self,
        tests: Sequence[TestIdentity],
        stats: Mapping[str, TestStats],
        predictions: Iterable[PairPrediction],
        run_id: Optional[str] = None,
    ) -> Schedule:
        started = time.perf_counter()
        risk = RiskIndex(predictions)
        randomizer = random.Random(self.seed)
        # Seeded tie-break ensures determinism without always favoring lexicographic IDs.
        tie = {test.id: randomizer.random() for test in tests}
        durations = {
            test.id: max(0.001, stats.get(test.id, TestStats()).duration_ema) for test in tests
        }
        by_id = {test.id: test for test in tests}
        remaining = sorted(
            by_id,
            key=lambda test_id: (-durations[test_id], tie[test_id], test_id),
        )
        lanes: list[list[ScheduledTest]] = [[] for _ in range(self.workers)]
        lane_ends = [0.0] * self.workers

        while remaining:
            worker = min(range(self.workers), key=lambda index: (lane_ends[index], index))
            start = lane_ends[worker]
            running = self._overlapping_at(lanes, start, worker)
            candidates = remaining[: self.params.lookahead]
            chosen = min(
                candidates,
                key=lambda test_id: self._placement_cost(
                    test_id, start, durations[test_id], lanes, risk, worker
                ),
            )
            probability, reasons = self._placement_risk(
                chosen, start, start + durations[chosen], lanes, risk, worker
            )

            # A hard-risk candidate may leave the worker briefly idle until the earliest
            # conflicting test finishes, provided another safe candidate is unavailable.
            if probability >= self.params.hard_threshold and running:
                safe = [
                    candidate
                    for candidate in candidates
                    if self._placement_risk(
                        candidate, start, start + durations[candidate], lanes, risk, worker
                    )[0]
                    < self.params.hard_threshold
                ]
                if safe:
                    chosen = min(
                        safe,
                        key=lambda test_id: self._placement_cost(
                            test_id, start, durations[test_id], lanes, risk, worker
                        ),
                    )
                    probability, reasons = self._placement_risk(
                        chosen, start, start + durations[chosen], lanes, risk, worker
                    )
                else:
                    next_end = min(
                        item.estimated_end
                        for lane_index, lane in enumerate(lanes)
                        if lane_index != worker
                        for item in lane
                        if item.estimated_start <= start < item.estimated_end
                    )
                    start = max(start, next_end)
                    probability, reasons = self._placement_risk(
                        chosen, start, start + durations[chosen], lanes, risk, worker
                    )
            scheduled = ScheduledTest(
                chosen,
                by_id[chosen].node_id,
                worker,
                start,
                start + durations[chosen],
                durations[chosen],
                probability,
                reasons,
            )
            lanes[worker].append(scheduled)
            lane_ends[worker] = scheduled.estimated_end
            remaining.remove(chosen)

        # Refinement is quadratic per attempted swap. Bounded list scheduling is
        # used directly for large suites so the production path stays responsive.
        if len(tests) <= 64:
            self._refine(lanes, risk)
        flattened = sorted(
            (item for lane in lanes for item in lane),
            key=lambda item: (item.estimated_start, item.worker, item.test_id),
        )
        expected_risk = self._schedule_risk(flattened, risk)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return Schedule(
            id=str(uuid.uuid4()),
            run_id=run_id or str(uuid.uuid4()),
            workers=self.workers,
            policy=self.policy,
            tests=flattened,
            scheduler_latency_ms=elapsed_ms,
            expected_makespan=max((item.estimated_end for item in flattened), default=0.0),
            expected_risk=expected_risk,
            seed=self.seed,
        )

    def _placement_cost(
        self,
        test_id: str,
        start: float,
        duration: float,
        lanes: list[list[ScheduledTest]],
        risk: RiskIndex,
        worker: int,
    ) -> float:
        probability, _ = self._placement_risk(test_id, start, start + duration, lanes, risk, worker)
        # LPT pressure starts long tail work early; conflict exposure can still make a
        # shorter safe test preferable when a long candidate would overlap real risk.
        return (
            start
            - self.params.utilization_bias * duration
            + self.params.risk_weight * probability * duration
        )

    @staticmethod
    def _overlapping_at(
        lanes: list[list[ScheduledTest]], instant: float, excluded_worker: int
    ) -> list[ScheduledTest]:
        return [
            item
            for index, lane in enumerate(lanes)
            if index != excluded_worker
            for item in lane
            if item.estimated_start <= instant < item.estimated_end
        ]

    def _placement_risk(
        self,
        test_id: str,
        start: float,
        end: float,
        lanes: list[list[ScheduledTest]],
        risk: RiskIndex,
        worker: int,
    ) -> tuple[float, list[str]]:
        safe = 1.0
        reasons: list[str] = []
        for index, lane in enumerate(lanes):
            if index == worker:
                continue
            # Lane end times are monotonic. Skip completed work with binary search,
            # then inspect only the tests that actually overlap this placement.
            low, high = 0, len(lane)
            while low < high:
                middle = (low + high) // 2
                if lane[middle].estimated_end <= start:
                    low = middle + 1
                else:
                    high = middle
            for other in lane[low:]:
                if other.estimated_start >= end:
                    break
                overlap = max(
                    0.0, min(end, other.estimated_end) - max(start, other.estimated_start)
                )
                if overlap <= 0:
                    continue
                probability = risk.probability(test_id, other.test_id)
                exposure = min(
                    1.0, overlap / max(0.001, min(end - start, other.estimated_duration))
                )
                safe *= 1.0 - probability * exposure
                if probability >= 0.25:
                    prediction = risk.prediction(test_id, other.test_id)
                    explanation = (
                        prediction.explanation if prediction else "predicted shared-resource risk"
                    )
                    reasons.append(f"{other.node_id}: {probability:.1%} ({explanation})")
        return 1.0 - safe, reasons

    def _refine(self, lanes: list[list[ScheduledTest]], risk: RiskIndex) -> None:
        """Perform deterministic adjacent swaps that strictly improve makespan+risk."""
        if self.refinement_rounds <= 0:
            return
        for _ in range(self.refinement_rounds):
            changed = False
            baseline = self._objective([item for lane in lanes for item in lane], risk)
            for lane_index, lane in enumerate(lanes):
                for index in range(len(lane) - 1):
                    lane[index], lane[index + 1] = lane[index + 1], lane[index]
                    self._reflow_lane(lane, lane_index)
                    candidate = self._objective([item for group in lanes for item in group], risk)
                    if candidate + 1e-9 < baseline:
                        baseline, changed = candidate, True
                    else:
                        lane[index], lane[index + 1] = lane[index + 1], lane[index]
                        self._reflow_lane(lane, lane_index)
            if not changed:
                break

    @staticmethod
    def _reflow_lane(lane: list[ScheduledTest], worker: int) -> None:
        cursor = 0.0
        for item in lane:
            item.worker = worker
            item.estimated_start = cursor
            item.estimated_end = cursor + item.estimated_duration
            cursor = item.estimated_end

    def _objective(self, tests: list[ScheduledTest], risk: RiskIndex) -> float:
        makespan = max((item.estimated_end for item in tests), default=0.0)
        return makespan + self.params.risk_weight * self._schedule_risk(tests, risk)

    @staticmethod
    def _schedule_risk(tests: Sequence[ScheduledTest], risk: RiskIndex) -> float:
        total = 0.0
        for index, left in enumerate(tests):
            for right in tests[index + 1 :]:
                if left.worker == right.worker:
                    continue
                overlap = max(
                    0.0,
                    min(left.estimated_end, right.estimated_end)
                    - max(left.estimated_start, right.estimated_start),
                )
                if overlap:
                    total += risk.probability(left.test_id, right.test_id) * overlap
        return total


class RandomScheduler:
    def __init__(self, workers: int, seed: int = 42) -> None:
        self.workers, self.seed = workers, seed

    def schedule(
        self,
        tests: Sequence[TestIdentity],
        stats: Mapping[str, TestStats],
        run_id: Optional[str] = None,
    ) -> Schedule:
        started = time.perf_counter()
        order = list(tests)
        random.Random(self.seed).shuffle(order)
        heap = [(0.0, worker) for worker in range(self.workers)]
        lanes: list[ScheduledTest] = []
        for test in order:
            start, worker = heapq.heappop(heap)
            duration = max(0.001, stats.get(test.id, TestStats()).duration_ema)
            lanes.append(
                ScheduledTest(test.id, test.node_id, worker, start, start + duration, duration)
            )
            heapq.heappush(heap, (start + duration, worker))
        elapsed = (time.perf_counter() - started) * 1000
        return Schedule(
            str(uuid.uuid4()),
            run_id or str(uuid.uuid4()),
            self.workers,
            RiskPolicy.AGGRESSIVE,
            lanes,
            elapsed,
            max((x.estimated_end for x in lanes), default=0),
            0,
            self.seed,
        )


class SerialScheduler:
    def schedule(
        self,
        tests: Sequence[TestIdentity],
        stats: Mapping[str, TestStats],
        run_id: Optional[str] = None,
    ) -> Schedule:
        started = time.perf_counter()
        cursor = 0.0
        scheduled: list[ScheduledTest] = []
        for test in tests:
            duration = max(0.001, stats.get(test.id, TestStats()).duration_ema)
            scheduled.append(
                ScheduledTest(test.id, test.node_id, 0, cursor, cursor + duration, duration)
            )
            cursor += duration
        return Schedule(
            str(uuid.uuid4()),
            run_id or str(uuid.uuid4()),
            1,
            RiskPolicy.SAFE,
            scheduled,
            (time.perf_counter() - started) * 1000,
            cursor,
            0,
            0,
        )
