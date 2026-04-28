from __future__ import annotations

import os
import shutil
import socket
import time
from pathlib import Path

import pytest
from conftest import BENCHMARK_ROOT


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets are unavailable")
@pytest.mark.conflict_family("unix-socket")
@pytest.mark.parametrize(
    "group,role", [(group, role) for group in range(8) for role in ("primary", "probe")]
)
def test_shared_unix_service(group: int, role: str, rng) -> None:
    path = BENCHMARK_ROOT / "sockets" / f"service-{group}.sock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    time.sleep(rng.uniform(0.003, 0.025))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        server.listen(1)
        time.sleep(rng.uniform(0.025, 0.060))
        assert path.exists()
    finally:
        server.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


@pytest.mark.conflict_family("directory-lifecycle")
@pytest.mark.parametrize(
    "group,role", [(group, role) for group in range(10) for role in ("publisher", "cleaner")]
)
def test_shared_release_directory(group: int, role: str, rng) -> None:
    root = BENCHMARK_ROOT / "releases" / f"release-{group}"
    staging = root.with_name(root.name + f"-{os.getpid()}.staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for index in range(3):
        (staging / f"asset-{index}.txt").write_text(f"{role}:{index}")
    time.sleep(rng.uniform(0.008, 0.030))
    shutil.rmtree(root, ignore_errors=True)
    staging.rename(root)
    assert len(list(root.iterdir())) == 3


@pytest.mark.conflict_family("safe-directory")
@pytest.mark.parametrize("case", range(14))
def test_isolated_release_directory(case: int, isolated_directory: Path) -> None:
    nested = isolated_directory / "cache" / f"shard-{case % 4}"
    nested.mkdir(parents=True)
    values = [nested / f"value-{index}" for index in range(4)]
    for index, path in enumerate(values):
        path.write_text(str(index * case))
    assert sum(int(path.read_text()) for path in values) == 6 * case
