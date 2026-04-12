from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

from .errors import ExecutionError
from .types import (
    ExecutionResult,
    ExecutionStatus,
    Schedule,
    ScheduledTest,
    TestIdentity,
    TestStats,
)

_NODE_LINE = re.compile(r"^(?P<node>[^\s]+::[^\s]+)$")


@dataclass
class PytestConfig:
    executable: str = sys.executable
    working_directory: Path = field(default_factory=Path.cwd)
    timeout_seconds: float = 300.0
    extra_args: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    output_limit_bytes: int = 1_000_000


class PytestAdapter:
    def __init__(self, config: Optional[PytestConfig] = None) -> None:
        self.config = config or PytestConfig()

    def collect(self, targets: Sequence[str] = ()) -> list[TestIdentity]:
        command = [
            self.config.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            *targets,
            *self.config.extra_args,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.config.working_directory,
                env=self._environment(),
                text=True,
                capture_output=True,
                timeout=max(30.0, self.config.timeout_seconds),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"pytest collection could not run: {exc}") from exc
        tests: list[TestIdentity] = []
        for line in completed.stdout.splitlines():
            candidate = line.strip()
            if _NODE_LINE.match(candidate) and not candidate.startswith(("=", "<")):
                tests.append(TestIdentity.from_pytest_nodeid(candidate))
        if completed.returncode not in {0, 5}:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ExecutionError(
                f"pytest collection failed with exit code {completed.returncode}:\n{detail}"
            )
        if not tests and completed.returncode != 5:
            raise ExecutionError("pytest reported success but no stable node IDs were discovered")
        return tests

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(self.config.environment)
        environment.setdefault("PYTHONHASHSEED", "0")
        return environment


class ScheduleExecutor:
    def __init__(
        self, adapter: PytestAdapter, on_result: Optional[Callable[[ExecutionResult], None]] = None
    ) -> None:
        self.adapter = adapter
        self.on_result = on_result
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    async def execute(self, schedule: Schedule, fail_fast: bool = False) -> list[ExecutionResult]:
        schedule_started = asyncio.get_running_loop().time()
        queues: list[asyncio.Queue[Optional[ScheduledTest]]] = [
            asyncio.Queue() for _ in range(schedule.workers)
        ]
        for item in sorted(schedule.tests, key=lambda value: (value.worker, value.estimated_start)):
            await queues[item.worker].put(item)
        for worker_queue in queues:
            await worker_queue.put(None)
        results: list[ExecutionResult] = []
        lock = asyncio.Lock()

        async def run_worker(worker: int) -> None:
            while not self._cancelled:
                scheduled = await queues[worker].get()
                if scheduled is None:
                    return
                delay = (
                    schedule_started + scheduled.estimated_start - asyncio.get_running_loop().time()
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                result = await self._run_one(schedule.run_id, scheduled)
                async with lock:
                    results.append(result)
                if self.on_result:
                    self.on_result(result)
                if fail_fast and result.status != ExecutionStatus.PASSED:
                    self._cancelled = True

        tasks = [
            asyncio.create_task(run_worker(worker), name=f"cg-worker-{worker}")
            for worker in range(schedule.workers)
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            self._cancelled = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return sorted(results, key=lambda item: item.started_at)

    async def _run_one(self, run_id: str, scheduled: ScheduledTest) -> ExecutionResult:
        execution_id = str(uuid.uuid4())
        environment = self.adapter._environment()
        environment.update(
            {
                "CONFLICTGRAPH_RUN_ID": run_id,
                "CONFLICTGRAPH_EXECUTION_ID": execution_id,
                "CONFLICTGRAPH_TEST_ID": scheduled.test_id,
                "CONFLICTGRAPH_WORKER_ID": str(scheduled.worker),
            }
        )
        command = [
            self.adapter.config.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            scheduled.node_id,
            *self.adapter.config.extra_args,
        ]
        started_wall = datetime.now(timezone.utc)
        started = time.perf_counter()
        process: Optional[asyncio.subprocess.Process] = None
        timed_out = False
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.adapter.config.working_directory,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout_raw, stderr_raw = await asyncio.wait_for(
                    process.communicate(), self.adapter.config.timeout_seconds
                )
            except asyncio.TimeoutError:
                timed_out = True
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), 5.0)
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
                stdout_raw, stderr_raw = b"", b"test exceeded configured timeout"
            exit_code = process.returncode if process.returncode is not None else -1
            stdout = self._decode(stdout_raw)
            stderr = self._decode(stderr_raw)
            status = (
                ExecutionStatus.TIMED_OUT
                if timed_out
                else (ExecutionStatus.PASSED if exit_code == 0 else ExecutionStatus.FAILED)
            )
            failure = (
                "" if status == ExecutionStatus.PASSED else self._failure_summary(stdout, stderr)
            )
        except OSError as exc:
            exit_code, stdout, stderr = -1, "", str(exc)
            status, failure = ExecutionStatus.INFRA_ERROR, str(exc)
        except asyncio.CancelledError:
            if process and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        ended_wall = datetime.now(timezone.utc)
        return ExecutionResult(
            execution_id,
            run_id,
            scheduled.test_id,
            scheduled.node_id,
            scheduled.worker,
            status,
            started_wall,
            ended_wall,
            time.perf_counter() - started,
            exit_code,
            stdout,
            stderr,
            failure,
            timed_out,
        )

    def _decode(self, payload: bytes) -> str:
        limit = self.adapter.config.output_limit_bytes
        if len(payload) > limit:
            payload = payload[:limit] + b"\n...[output truncated by ConflictGraph]"
        return payload.decode("utf-8", errors="replace")

    @staticmethod
    def _failure_summary(stdout: str, stderr: str) -> str:
        lines = [line.strip() for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
        for line in reversed(lines):
            if "failed" in line.lower() or "error" in line.lower():
                return line[:1000]
        return lines[-1][:1000] if lines else "test process failed without output"


def update_duration_stats(
    stats: Mapping[str, TestStats], results: Iterable[ExecutionResult], alpha: float = 0.3
) -> None:
    for result in results:
        item = stats[result.test_id]
        item.duration_ema = alpha * result.duration_seconds + (1 - alpha) * item.duration_ema
        item.executions += 1
        failures = item.failure_rate * (item.executions - 1) + (
            result.status != ExecutionStatus.PASSED
        )
        item.failure_rate = failures / item.executions
