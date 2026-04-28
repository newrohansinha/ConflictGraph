from __future__ import annotations

import hashlib
import json
import math
import time

import pytest


@pytest.mark.conflict_family("safe-read-only")
@pytest.mark.parametrize("case", range(26))
def test_read_only_shared_fixture(case: int, benchmark_root, rng) -> None:
    path = benchmark_root / "immutable-fixtures" / f"catalog-{case % 4}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps({"values": list(range(50)), "version": 1}))
    value = json.loads(path.read_text())
    time.sleep(rng.uniform(0, 0.004))
    assert sum(value["values"]) == 1225 and value["version"] == 1


@pytest.mark.conflict_family("safe-compute")
@pytest.mark.parametrize("case", range(18))
def test_pure_computation(case: int) -> None:
    payload = ":".join(str(math.factorial(value % 12)) for value in range(case, case + 20))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    assert len(digest) == 64
