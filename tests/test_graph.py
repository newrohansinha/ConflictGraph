import pytest
from conflictgraph.graph import HeuristicPredictor, TestResourceGraph
from conflictgraph.types import ConflictCause, Operation, ResourceType
from helpers import event, two_tests


def graph_with_pair(
    left_operation: Operation, right_operation: Operation, kind: ResourceType = ResourceType.FILE
):
    left, right = two_tests()
    graph = TestResourceGraph.from_events(
        [
            event(left.id, "/tmp/shared", left_operation, kind, 10),
            event(right.id, "/tmp/shared", right_operation, kind, 12),
        ],
        [left, right],
    )
    return graph, left, right


def test_graph_aggregates_counts_and_indexes():
    graph, left, right = graph_with_pair(Operation.WRITE, Operation.READ)
    resource = next(iter(graph.resources))
    graph.add_event(event(left.id, "/tmp/shared", Operation.WRITE, timestamp=15))
    assert graph.edges[(left.id, resource)].counts[Operation.WRITE] == 2
    assert graph.resource_tests[resource] == {left.id, right.id}


def test_read_read_is_not_default_candidate():
    graph, left, right = graph_with_pair(Operation.READ, Operation.READ)
    assert list(graph.candidate_pairs()) == []
    assert list(graph.candidate_pairs(include_readonly=True)) == [(left.id, right.id)]


@pytest.mark.parametrize(
    "left,right,field",
    [
        (Operation.WRITE, Operation.WRITE, "write_write"),
        (Operation.READ, Operation.WRITE, "read_write"),
        (Operation.CREATE, Operation.CREATE, "create_create"),
        (Operation.DELETE, Operation.READ, "delete_read"),
        (Operation.BIND, Operation.BIND, "shared_bind"),
        (Operation.LOCK, Operation.LOCK, "shared_locks"),
    ],
)
def test_pair_semantics(left, right, field):
    graph, a, b = graph_with_pair(left, right)
    assert getattr(graph.pair_features(a.id, b.id), field) == 1


def test_feature_jaccard_and_rarity():
    graph, left, right = graph_with_pair(Operation.WRITE, Operation.WRITE)
    graph.add_event(event(left.id, "/tmp/only-a", Operation.WRITE))
    features = graph.pair_features(left.id, right.id)
    assert features.shared_resources == 1
    assert features.resource_jaccard == pytest.approx(0.5)
    assert features.mean_rarity == pytest.approx(0.5)


def test_temporal_overlap_is_computed_from_access_windows():
    left, right = two_tests()
    graph = TestResourceGraph.from_events(
        [
            event(left.id, "/tmp/x", Operation.WRITE, timestamp=10),
            event(left.id, "/tmp/x", Operation.WRITE, timestamp=30),
            event(right.id, "/tmp/x", Operation.WRITE, timestamp=20),
            event(right.id, "/tmp/x", Operation.WRITE, timestamp=40),
        ],
        [left, right],
    )
    assert graph.pair_features(left.id, right.id).temporal_overlap == pytest.approx(10 / 30)


def test_heuristic_prioritizes_bind_collision():
    graph, left, right = graph_with_pair(Operation.BIND, Operation.BIND, ResourceType.TCP_ENDPOINT)
    prediction = HeuristicPredictor().score(graph.pair_features(left.id, right.id))
    assert prediction.probability > 0.99
    assert prediction.cause is ConflictCause.PORT_COLLISION
    assert "bind" in prediction.explanation


def test_heuristic_keeps_read_only_risk_low():
    graph, left, right = graph_with_pair(Operation.READ, Operation.READ)
    prediction = HeuristicPredictor().score(graph.pair_features(left.id, right.id))
    assert prediction.probability < 0.03
    assert "read-only" in prediction.explanation


def test_history_updates_risk():
    graph, left, right = graph_with_pair(Operation.READ, Operation.READ)
    graph.pair_history[(left.id, right.id)] = (10, 4)
    prediction = HeuristicPredictor().score(graph.pair_features(left.id, right.id))
    assert prediction.probability > 0.39


def test_graph_refreshes_resource_stats():
    graph, left, _ = graph_with_pair(Operation.WRITE, Operation.READ)
    assert graph.test_stats[left.id].resource_count == 1
    assert graph.test_stats[left.id].write_ratio == 1
