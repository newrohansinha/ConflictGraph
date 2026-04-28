from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest


@pytest.mark.requires_redis
@pytest.mark.conflict_family("redis-key")
@pytest.mark.parametrize(
    "group,role", [(group, role) for group in range(8) for role in ("producer", "consumer")]
)
def test_shared_redis_state(group: int, role: str, redis_client, rng) -> None:
    key = f"conflictgraph:shared:{group}"
    token = f"{role}:{os.getpid()}"
    redis_client.set(key, token, ex=30)
    time.sleep(rng.uniform(0.012, 0.040))
    assert redis_client.get(key).decode() == token


@pytest.mark.requires_redis
@pytest.mark.conflict_family("safe-redis-key")
@pytest.mark.parametrize("case", range(14))
def test_isolated_redis_state(case: int, redis_client) -> None:
    key = f"conflictgraph:isolated:{os.getpid()}:{case}"
    redis_client.hset(key, mapping={"case": case, "state": "ready"})
    assert redis_client.hget(key, "state") == b"ready"
    redis_client.delete(key)


@pytest.mark.contention
@pytest.mark.conflict_family("cpu-contention")
@pytest.mark.parametrize("case", range(8))
def test_cpu_budget(case: int) -> None:
    script = "import hashlib\nvalue=b'x'*65536\nfor _ in range(850): value=hashlib.sha256(value).digest()*2048\n"
    started = time.perf_counter()
    completed = subprocess.run([sys.executable, "-c", script], timeout=1.8, check=False)
    elapsed = time.perf_counter() - started
    assert completed.returncode == 0
    assert elapsed < 1.8, f"CPU budget exceeded for case {case}: {elapsed:.3f}s"
