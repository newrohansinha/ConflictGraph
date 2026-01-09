import pytest
from conflictgraph.types import (
    PairPrediction,
    ResourceIdentity,
    ResourceType,
    TestIdentity,
    TraceQuality,
    stable_pair,
)


def test_pytest_identity_is_stable_and_preserves_parameters():
    first = TestIdentity.from_pytest_nodeid(
        "tests/test_api.py::TestClient::test_get[user-1]", "repo"
    )
    second = TestIdentity.from_pytest_nodeid(
        "tests/test_api.py::TestClient::test_get[user-1]", "repo", "new-revision"
    )
    assert first.id == second.id
    assert first.test_file == "tests/test_api.py"
    assert first.test_class == "TestClient"
    assert first.test_function == "test_get"
    assert first.parameters == "user-1"
    assert second.source_revision == "new-revision"


def test_repository_is_part_of_test_identity():
    left = TestIdentity.from_pytest_nodeid("test_a.py::test_one", "left")
    right = TestIdentity.from_pytest_nodeid("test_a.py::test_one", "right")
    assert left.id != right.id


def test_resource_identity_includes_type():
    file = ResourceIdentity.create(ResourceType.FILE, "/tmp/service.sock")
    socket = ResourceIdentity.create(ResourceType.UNIX_SOCKET, "/tmp/service.sock")
    assert file.id != socket.id


def test_prediction_normalizes_pair_and_probability():
    prediction = PairPrediction("z", "a", 1.4)
    assert prediction.key == ("a", "z")
    assert prediction.probability == 1


@pytest.mark.parametrize(
    "a,b,expected",
    [("a", "b", ("a", "b")), ("b", "a", ("a", "b")), ("same", "same", ("same", "same"))],
)
def test_stable_pair(a, b, expected):
    assert stable_pair(a, b) == expected


def test_trace_quality_accounts_for_drop_and_attribution():
    quality = TraceQuality(captured=90, processed=80, dropped=10, unattributed=8)
    assert quality.completeness == pytest.approx(0.8)
    assert quality.attribution_rate == pytest.approx(0.9)
    assert quality.score == pytest.approx(0.72)


def test_empty_trace_is_complete():
    assert TraceQuality().score == 1
