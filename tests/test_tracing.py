import io
import json

import pytest
from conflictgraph.errors import TraceQualityError
from conflictgraph.tracing import (
    AttributionRegistry,
    ExecutionRegistration,
    LogicalResourceClient,
    ReplayTracer,
    require_quality,
)
from conflictgraph.types import (
    AccessMode,
    EventSource,
    Operation,
    ResourceType,
    TraceEvent,
    TraceQuality,
)


def make_event(**changes):
    values = dict(
        execution_id="e",
        test_id="t",
        timestamp_ns=1,
        pid=1,
        tid=1,
        cgroup_id=2,
        resource_type=ResourceType.FILE,
        resource_identifier="/tmp/a",
        operation=Operation.WRITE,
        access_mode=AccessMode.WRITE,
        source=EventSource.REPLAY,
    )
    values.update(changes)
    return TraceEvent(**values)


def test_cgroup_attribution_has_priority():
    registry = AttributionRegistry()
    registry.register(ExecutionRegistration("cgroup", "tc", cgroup_id=10))
    registry.register(ExecutionRegistration("process", "tp", root_pid=100))
    assert registry.resolve(10, 100).execution_id == "cgroup"


def test_fork_inherits_process_attribution():
    registry = AttributionRegistry()
    registry.register(ExecutionRegistration("e", "t", root_pid=100))
    registry.register_fork(100, 101)
    registry.register_fork(101, 102)
    assert registry.resolve(0, 102).test_id == "t"


def test_unregister_removes_attribution():
    registry = AttributionRegistry()
    registry.register(ExecutionRegistration("e", "t", cgroup_id=5, root_pid=10))
    registry.unregister("e")
    assert registry.resolve(5, 10) is None


def test_replay_parses_and_normalizes():
    source = io.StringIO(json.dumps(make_event().to_dict()) + "\n")
    events = []
    quality = ReplayTracer().replay(source, events.append)
    assert len(events) == 1
    assert quality.score == 1


def test_replay_counts_parse_failure():
    source = io.StringIO("not-json\n" + json.dumps(make_event().to_dict()) + "\n")
    events = []
    quality = ReplayTracer().replay(source, events.append)
    assert quality.captured == 2
    assert quality.processed == 1
    assert quality.parse_failures == 1


def test_replay_filters_irrelevant_system_read():
    source = io.StringIO(
        json.dumps(
            make_event(
                resource_identifier="/usr/lib/libc.so",
                operation=Operation.READ,
                access_mode=AccessMode.READ,
            ).to_dict()
        )
        + "\n"
    )
    events = []
    quality = ReplayTracer().replay(source, events.append)
    assert not events
    assert quality.captured == 1 and quality.processed == 0


def test_trace_quality_gate():
    with pytest.raises(TraceQualityError, match="below"):
        require_quality(TraceQuality(captured=10, processed=5, dropped=5), 0.8)
    require_quality(TraceQuality(captured=10, processed=10), 0.8)


def test_logical_client_uses_environment_identity(monkeypatch):
    monkeypatch.setenv("CONFLICTGRAPH_EXECUTION_ID", "execution-from-env")
    monkeypatch.setenv("CONFLICTGRAPH_TEST_ID", "test-from-env")
    client = LogicalResourceClient("/tmp/unused.sock")
    assert client.execution_id == "execution-from-env"
    assert client.test_id == "test-from-env"


@pytest.mark.parametrize(
    ("execution_id", "test_id"), [("", ""), ("explicit", ""), ("", "explicit")]
)
def test_logical_client_explicit_identity_overrides_environment(
    monkeypatch, execution_id: str, test_id: str
):
    monkeypatch.setenv("CONFLICTGRAPH_EXECUTION_ID", "execution-from-env")
    monkeypatch.setenv("CONFLICTGRAPH_TEST_ID", "test-from-env")
    client = LogicalResourceClient("/tmp/unused.sock", execution_id, test_id)
    assert client.execution_id == execution_id
    assert client.test_id == test_id
