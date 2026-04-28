from __future__ import annotations

import hashlib
import os
import random
import shutil
from pathlib import Path
from typing import Iterator

import pytest

BENCHMARK_ROOT = Path(os.getenv("CONFLICTGRAPH_BENCHMARK_ROOT", "/tmp/conflictgraph-benchmark"))


def seeded_random(nodeid: str) -> random.Random:
    seed = int(os.getenv("CONFLICTGRAPH_BENCHMARK_SEED", "42"))
    digest = hashlib.sha256(f"{seed}\0{nodeid}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


@pytest.fixture
def rng(request: pytest.FixtureRequest) -> random.Random:
    return seeded_random(request.node.nodeid)


@pytest.fixture(scope="session", autouse=True)
def benchmark_root() -> Iterator[Path]:
    BENCHMARK_ROOT.mkdir(parents=True, exist_ok=True)
    yield BENCHMARK_ROOT


@pytest.fixture
def isolated_directory(request: pytest.FixtureRequest) -> Iterator[Path]:
    digest = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    path = BENCHMARK_ROOT / "isolated" / digest
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def redis_client():
    redis = pytest.importorskip("redis")
    url = os.getenv("CONFLICTGRAPH_REDIS_URL", "redis://localhost:6379/15")
    client = redis.Redis.from_url(url, socket_timeout=1)
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"benchmark Redis is unavailable: {exc}")
    return client


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        family = next(
            (marker.args[0] for marker in item.iter_markers("conflict_family") if marker.args),
            "safe",
        )
        item.user_properties.append(("conflictgraph_family", family))
