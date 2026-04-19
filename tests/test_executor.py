import asyncio
import time
from pathlib import Path

import pytest
from conflictgraph.executor import PytestAdapter, PytestConfig, ScheduleExecutor
from conflictgraph.scheduler import SerialScheduler
from conflictgraph.types import ExecutionStatus, RiskPolicy, Schedule, ScheduledTest, TestStats


def create_suite(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n\n"
        "def test_pass(): assert 2 + 2 == 4\n\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_parameter(value): assert value > 0\n"
    )


def test_pytest_collection_preserves_parameter_node_ids(tmp_path: Path):
    create_suite(tmp_path)
    adapter = PytestAdapter(PytestConfig(working_directory=tmp_path))
    tests = adapter.collect()
    assert len(tests) == 3
    assert {test.parameters for test in tests} == {"", "1", "2"}


def test_executor_runs_collected_tests(tmp_path: Path):
    create_suite(tmp_path)
    adapter = PytestAdapter(PytestConfig(working_directory=tmp_path, timeout_seconds=10))
    tests = adapter.collect()
    schedule = SerialScheduler().schedule(
        tests, {test.id: TestStats(duration_ema=0.01) for test in tests}
    )
    results = asyncio.run(ScheduleExecutor(adapter).execute(schedule))
    assert len(results) == 3
    assert all(result.status is ExecutionStatus.PASSED for result in results)
    assert all(result.duration_seconds > 0 for result in results)


def test_collection_failure_is_actionable(tmp_path: Path):
    (tmp_path / "test_broken.py").write_text("this is not valid python !")
    adapter = PytestAdapter(PytestConfig(working_directory=tmp_path))
    with pytest.raises(Exception, match="collection failed"):
        adapter.collect()


def test_failure_output_is_preserved(tmp_path: Path):
    (tmp_path / "test_fail.py").write_text(
        "def test_bad():\n    print('diagnostic-token')\n    assert False\n"
    )
    adapter = PytestAdapter(PytestConfig(working_directory=tmp_path))
    tests = adapter.collect()
    schedule = SerialScheduler().schedule(tests, {tests[0].id: TestStats(duration_ema=0.01)})
    result = asyncio.run(ScheduleExecutor(adapter).execute(schedule))[0]
    assert result.status is ExecutionStatus.FAILED
    assert "diagnostic-token" in result.stdout
    assert result.failure_message


def test_timeout_is_not_hidden_as_test_failure(tmp_path: Path):
    (tmp_path / "test_slow.py").write_text("import time\ndef test_slow(): time.sleep(2)\n")
    adapter = PytestAdapter(PytestConfig(working_directory=tmp_path, timeout_seconds=0.1))
    tests = adapter.collect()
    schedule = SerialScheduler().schedule(tests, {tests[0].id: TestStats(duration_ema=0.01)})
    result = asyncio.run(ScheduleExecutor(adapter).execute(schedule))[0]
    assert result.status is ExecutionStatus.TIMED_OUT
    assert result.timed_out


def test_executor_honors_planned_idle_time(tmp_path: Path):
    create_suite(tmp_path)
    adapter = PytestAdapter(PytestConfig(working_directory=tmp_path, timeout_seconds=10))
    test = adapter.collect()[0]
    schedule = Schedule(
        "schedule",
        "run",
        1,
        RiskPolicy.BALANCED,
        [ScheduledTest(test.id, test.node_id, 0, 0.12, 0.13, 0.01)],
        0,
        0.13,
        0,
        42,
    )
    started = time.perf_counter()
    asyncio.run(ScheduleExecutor(adapter).execute(schedule))
    assert time.perf_counter() - started >= 0.1


def test_cancelling_execution_terminates_the_test_process(tmp_path: Path):
    (tmp_path / "test_wait.py").write_text("import time\ndef test_wait(): time.sleep(30)\n")
    adapter = PytestAdapter(PytestConfig(working_directory=tmp_path, timeout_seconds=60))
    test = adapter.collect()[0]
    schedule = SerialScheduler().schedule([test], {test.id: TestStats(duration_ema=1)})

    async def cancel_run() -> None:
        task = asyncio.create_task(ScheduleExecutor(adapter).execute(schedule))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started = time.perf_counter()
    asyncio.run(cancel_run())
    assert time.perf_counter() - started < 3
