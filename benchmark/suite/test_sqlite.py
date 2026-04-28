from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from conftest import BENCHMARK_ROOT


def prepare(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS events (owner TEXT, value INTEGER)")
    connection.commit()


@pytest.mark.conflict_family("sqlite-write-lock")
@pytest.mark.parametrize(
    "group,role", [(group, role) for group in range(10) for role in ("ingest", "compact")]
)
def test_shared_database_transaction(group: int, role: str, rng) -> None:
    path = BENCHMARK_ROOT / "sqlite" / f"shared-{group}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=0.018, isolation_level=None)
    try:
        prepare(connection)
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("INSERT INTO events VALUES (?, ?)", (role, group))
        time.sleep(rng.uniform(0.025, 0.060))
        connection.execute("COMMIT")
    finally:
        connection.close()


@pytest.mark.conflict_family("sqlite-schema")
@pytest.mark.parametrize(
    "group,role", [(group, role) for group in range(6) for role in ("migration", "query")]
)
def test_shared_schema_lifecycle(group: int, role: str, rng) -> None:
    path = BENCHMARK_ROOT / "sqlite-schema" / f"application-{group}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=0.025)
    try:
        prepare(connection)
        if role == "migration":
            connection.execute("DROP TABLE IF EXISTS derived")
            time.sleep(rng.uniform(0.015, 0.040))
            connection.execute("CREATE TABLE derived (value INTEGER)")
        else:
            connection.execute("INSERT INTO events VALUES ('query', ?)", (group,))
        connection.commit()
    finally:
        connection.close()


@pytest.mark.conflict_family("safe-isolated-sqlite")
@pytest.mark.parametrize("case", range(18))
def test_isolated_database(case: int, isolated_directory: Path) -> None:
    connection = sqlite3.connect(isolated_directory / "application.db")
    try:
        prepare(connection)
        connection.executemany(
            "INSERT INTO events VALUES (?, ?)", [(f"owner-{case}", value) for value in range(5)]
        )
        connection.commit()
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 5
    finally:
        connection.close()
