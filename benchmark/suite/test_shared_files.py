from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from conftest import BENCHMARK_ROOT


@pytest.mark.conflict_family("shared-file")
@pytest.mark.parametrize(
    "group,role", [(group, role) for group in range(12) for role in ("writer", "validator")]
)
def test_shared_json_transaction(group: int, role: str, rng) -> None:
    path = BENCHMARK_ROOT / "shared-json" / f"settings-{group}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{role}-{os.getpid()}"
    path.write_text(json.dumps({"token": token, "complete": False}))
    time.sleep(rng.uniform(0.015, 0.055))
    path.write_text(json.dumps({"token": token, "complete": True}))
    time.sleep(rng.uniform(0.004, 0.018))
    assert json.loads(path.read_text()) == {"token": token, "complete": True}


@pytest.mark.conflict_family("file-delete-read")
@pytest.mark.parametrize(
    "group,role", [(group, role) for group in range(8) for role in ("consumer", "cleaner")]
)
def test_shared_artifact_lifecycle(group: int, role: str, rng) -> None:
    path = BENCHMARK_ROOT / "artifacts" / f"payload-{group}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = f"payload:{os.getpid()}:{role}"
    path.write_text(expected)
    time.sleep(rng.uniform(0.012, 0.035))
    if role == "cleaner":
        observed = path.read_text()
        path.unlink()
        assert observed == expected
    else:
        assert path.read_text() == expected


@pytest.mark.conflict_family("safe-isolated-file")
@pytest.mark.parametrize("case", range(18))
def test_isolated_file_transaction(case: int, isolated_directory: Path, rng) -> None:
    source = isolated_directory / "source.json"
    destination = isolated_directory / "committed.json"
    payload = {"case": case, "values": list(range(case % 7))}
    source.write_text(json.dumps(payload))
    time.sleep(rng.uniform(0, 0.008))
    source.replace(destination)
    assert json.loads(destination.read_text()) == payload
