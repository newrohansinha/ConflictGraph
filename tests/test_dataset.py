from pathlib import Path

import pytest
from conflictgraph.dataset import (
    ConflictLabel,
    Dataset,
    DatasetBuilder,
    family_holdout_split,
    pair_disjoint_split,
)
from conflictgraph.graph import TestResourceGraph
from conflictgraph.types import ConflictCause, Operation, TestIdentity
from helpers import event


def fixture_graph():
    tests = [TestIdentity(str(index), f"test.py::test_{index}") for index in range(8)]
    events = []
    for index in range(0, 8, 2):
        events += [
            event(str(index), f"/tmp/{index}", Operation.WRITE),
            event(str(index + 1), f"/tmp/{index}", Operation.WRITE),
        ]
    graph = TestResourceGraph.from_events(events, tests)
    labels = [
        ConflictLabel("0", "1", True, ConflictCause.FILE_COLLISION),
        ConflictLabel("2", "3", False),
        ConflictLabel("4", "5", True, ConflictCause.DATABASE_LOCK),
        ConflictLabel("6", "7", False),
    ]
    return graph, labels


def test_dataset_is_deterministic():
    graph, labels = fixture_graph()
    left = DatasetBuilder(42).build(graph, labels)
    right = DatasetBuilder(42).build(graph, labels)
    assert left.examples == right.examples
    assert left.metadata.feature_names
    assert left.metadata.positive_examples == 2


def test_dataset_roundtrip_and_checksum(tmp_path: Path):
    graph, labels = fixture_graph()
    dataset = DatasetBuilder().build(graph, labels)
    dataset.save(tmp_path)
    restored = Dataset.load(tmp_path)
    assert restored.examples == dataset.examples
    assert restored.metadata.content_hash


def test_corrupt_dataset_detected(tmp_path: Path):
    graph, labels = fixture_graph()
    dataset = DatasetBuilder().build(graph, labels)
    dataset.save(tmp_path)
    with (tmp_path / "examples.jsonl").open("a") as output:
        output.write("{}\n")
    with pytest.raises(Exception, match="hash"):
        Dataset.load(tmp_path)


def test_pair_disjoint_split_has_no_leakage():
    graph, labels = fixture_graph()
    dataset = DatasetBuilder().build(graph, labels)
    split = pair_disjoint_split(dataset, 7)
    split.validate(dataset)
    assert sorted(split.train + split.validation + split.test) == list(range(len(dataset.examples)))


def test_split_is_seed_deterministic():
    graph, labels = fixture_graph()
    dataset = DatasetBuilder().build(graph, labels)
    assert pair_disjoint_split(dataset, 4) == pair_disjoint_split(dataset, 4)


def test_invalid_ratios_rejected():
    graph, labels = fixture_graph()
    with pytest.raises(ValueError, match="sum"):
        pair_disjoint_split(DatasetBuilder().build(graph, labels), ratios=(0.5, 0.5, 0.5))


def test_family_holdout_keeps_cause_out_of_train():
    graph, labels = fixture_graph()
    dataset = DatasetBuilder().build(graph, labels)
    split = family_holdout_split(dataset, {ConflictCause.DATABASE_LOCK.value})
    assert all(
        dataset.examples[index].cause == ConflictCause.DATABASE_LOCK.value for index in split.test
    )
    assert all(
        dataset.examples[index].cause != ConflictCause.DATABASE_LOCK.value for index in split.train
    )


def test_negative_sampling_limit():
    graph = TestResourceGraph()
    tests = [TestIdentity(str(index), str(index)) for index in range(20)]
    for test in tests:
        graph.add_test(test)
        graph.add_event(event(test.id, "/tmp/common", Operation.READ))
    labels = [ConflictLabel("0", "1", True)]
    dataset = DatasetBuilder(seed=1, negative_ratio=2).build(graph, labels)
    assert dataset.metadata.negative_examples <= 32


def test_inference_dataset_retains_every_candidate():
    graph = TestResourceGraph()
    tests = [TestIdentity(str(index), str(index)) for index in range(12)]
    for test in tests:
        graph.add_test(test)
        graph.add_event(event(test.id, "/tmp/common", Operation.READ))
    dataset = DatasetBuilder(seed=1, negative_ratio=None).build(graph, [])
    assert dataset.metadata.negative_examples == 66


def test_negative_sampling_ratio_cannot_be_negative():
    with pytest.raises(ValueError, match="non-negative"):
        DatasetBuilder(negative_ratio=-1)


def test_empty_array_selection_preserves_feature_width_and_dtype():
    graph, labels = fixture_graph()
    dataset = DatasetBuilder().build(graph, labels)
    features, targets, weights = dataset.arrays([])
    assert features.shape == (0, len(dataset.metadata.feature_names))
    assert targets.shape == weights.shape == (0,)
    assert features.dtype == targets.dtype == weights.dtype
