from pathlib import Path

import pytest
from conflictgraph.storage import ArtifactStore, StorageError


def test_artifact_store_roundtrip(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    path = store.save("runs", "abc-123", {"id": "abc-123", "status": "COMPLETED"})
    assert path.exists()
    assert store.load("runs", "abc-123")["status"] == "COMPLETED"


def test_artifact_store_uses_atomic_temporary_file(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    store.save("runs", "abc", {"value": 1})
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("identifier", ["../escape", "with space", "a/b", ""])
def test_unsafe_identifier_rejected(tmp_path: Path, identifier: str):
    with pytest.raises(StorageError, match="unsafe"):
        ArtifactStore(tmp_path).save("runs", identifier, {})


def test_list_is_newest_first(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    store.save("runs", "first", {"id": "first"})
    store.save("runs", "second", {"id": "second"})
    assert {item["id"] for item in store.list("runs")} == {"first", "second"}


def test_corrupt_artifact_is_explicit(tmp_path: Path):
    path = tmp_path / "runs" / "bad.json"
    path.parent.mkdir()
    path.write_text("{")
    with pytest.raises(StorageError, match="corrupt"):
        ArtifactStore(tmp_path).load("runs", "bad")


def test_missing_artifact_is_none(tmp_path: Path):
    assert ArtifactStore(tmp_path).load("runs", "missing") is None


@pytest.mark.parametrize("category", ["../runs", "a/b", "with space", "", ".", ".."])
def test_unsafe_category_rejected_for_every_operation(tmp_path: Path, category: str):
    store = ArtifactStore(tmp_path)
    with pytest.raises(StorageError, match="category"):
        store.save(category, "safe", {})
    with pytest.raises(StorageError, match="category"):
        store.load(category, "safe")
    with pytest.raises(StorageError, match="category"):
        store.list(category)


@pytest.mark.parametrize("identifier", ["../escape", "a/b", "with space", "", ".", ".."])
def test_unsafe_identifier_rejected_on_load(tmp_path: Path, identifier: str):
    with pytest.raises(StorageError, match="identifier"):
        ArtifactStore(tmp_path).load("runs", identifier)


@pytest.mark.parametrize("payload", ["[]", '"text"', "123", "true", "null"])
def test_load_rejects_nonobject_json(tmp_path: Path, payload: str):
    path = tmp_path / "runs" / "value.json"
    path.parent.mkdir()
    path.write_text(payload)
    with pytest.raises(StorageError, match="JSON object"):
        ArtifactStore(tmp_path).load("runs", "value")


def test_list_rejects_corrupt_and_nonobject_entries(tmp_path: Path):
    path = tmp_path / "runs" / "value.json"
    path.parent.mkdir()
    path.write_text("[]")
    with pytest.raises(StorageError, match="JSON object"):
        ArtifactStore(tmp_path).list("runs")
    path.write_text("{")
    with pytest.raises(StorageError, match="corrupt"):
        ArtifactStore(tmp_path).list("runs")


def test_nonpositive_list_limit_returns_no_entries(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    store.save("runs", "value", {"id": "value"})
    assert store.list("runs", 0) == []
    assert store.list("runs", -1) == []
