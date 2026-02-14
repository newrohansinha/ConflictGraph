from conflictgraph.types import (
    AccessMode,
    EventSource,
    Operation,
    ResourceType,
    TestIdentity,
    TraceEvent,
)


def event(
    test_id: str,
    resource: str,
    operation: Operation,
    kind: ResourceType = ResourceType.FILE,
    timestamp: int = 1,
) -> TraceEvent:
    mode = (
        AccessMode.READ
        if operation == Operation.READ
        else AccessMode.EXCLUSIVE
        if operation in {Operation.BIND, Operation.LOCK}
        else AccessMode.WRITE
    )
    return TraceEvent(
        f"exec-{test_id}",
        test_id,
        timestamp,
        1,
        1,
        1,
        kind,
        resource,
        operation,
        mode,
        EventSource.REPLAY,
    )


def two_tests():
    return TestIdentity("a", "test.py::test_a"), TestIdentity("b", "test.py::test_b")
