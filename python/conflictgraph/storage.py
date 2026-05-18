from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Optional, cast

from .errors import ConflictGraphError
from .types import ExecutionResult, PairPrediction, Schedule, TestIdentity, json_default


class StorageError(ConflictGraphError):
    pass


class PostgresStore:
    def __init__(
        self, connection_string: str, minimum_size: int = 1, maximum_size: int = 8
    ) -> None:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise StorageError(
                "PostgreSQL support requires `pip install conflictgraph[api]`"
            ) from exc
        self._pool = ConnectionPool(
            connection_string, min_size=minimum_size, max_size=maximum_size, open=False
        )

    def open(self) -> None:
        try:
            self._pool.open(wait=True)
        except Exception as exc:
            raise StorageError(f"PostgreSQL connection failed: {exc}") from exc

    def close(self) -> None:
        self._pool.close()

    def upsert_tests(self, tests: Iterable[TestIdentity]) -> None:
        query = """
            INSERT INTO tests (id, node_id, repository, suite, framework, test_file, test_class,
                               test_function, parameters, source_revision)
            VALUES (%(id)s, %(node_id)s, %(repository)s, %(suite)s, %(framework)s, %(test_file)s,
                    %(test_class)s, %(test_function)s, %(parameters)s, %(source_revision)s)
            ON CONFLICT (id) DO UPDATE SET node_id=excluded.node_id, source_revision=excluded.source_revision,
                updated_at=now()
        """
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(query, [asdict(test) for test in tests])

    def create_run(
        self,
        run_id: str,
        scheduler: str,
        workers: int,
        seed: int,
        revision: str = "unknown",
        trace_mode: str = "disabled",
    ) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                "INSERT INTO runs(id,status,scheduler_policy,worker_count,seed,source_revision,trace_mode) VALUES(%s,'RUNNING',%s,%s,%s,%s,%s)",
                (run_id, scheduler, workers, seed, revision, trace_mode),
            )

    def save_schedule(self, schedule: Schedule) -> None:
        payload = json.dumps(asdict(schedule), default=json_default)
        with self._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO schedules(id,run_id,policy,worker_count,expected_makespan,expected_risk,latency_ms,seed,plan) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                    (
                        schedule.id,
                        schedule.run_id,
                        schedule.policy.value,
                        schedule.workers,
                        schedule.expected_makespan,
                        schedule.expected_risk,
                        schedule.scheduler_latency_ms,
                        schedule.seed,
                        payload,
                    ),
                )

    def save_result(self, result: ExecutionResult) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """INSERT INTO executions(id,run_id,test_id,worker_id,status,started_at,ended_at,duration_seconds,
                    exit_code,stdout,stderr,failure_message,timed_out) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO NOTHING""",
                (
                    result.execution_id,
                    result.run_id,
                    result.test_id,
                    result.worker,
                    result.status.value,
                    result.started_at,
                    result.ended_at,
                    result.duration_seconds,
                    result.exit_code,
                    result.stdout,
                    result.stderr,
                    result.failure_message,
                    result.timed_out,
                ),
            )

    def save_predictions(self, run_id: str, predictions: Iterable[PairPrediction]) -> None:
        rows = [
            (
                run_id,
                item.test_a,
                item.test_b,
                item.probability,
                item.cause.value,
                item.model_version,
                item.shared_resources,
                item.explanation,
                item.predicted_at,
            )
            for item in predictions
        ]
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO predictions(run_id,test_a_id,test_b_id,probability,cause,model_version,
                       shared_resources,explanation,predicted_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(run_id,test_a_id,test_b_id) DO UPDATE SET probability=excluded.probability,
                       cause=excluded.cause,model_version=excluded.model_version,explanation=excluded.explanation""",
                    rows,
                )

    def finalize_run(
        self, run_id: str, trace_quality: Optional[float] = None, error: str = ""
    ) -> None:
        status = "FAILED" if error else "COMPLETED"
        with self._pool.connection() as connection:
            connection.execute(
                "UPDATE runs SET status=%s,ended_at=now(),trace_quality=%s,error=%s WHERE id=%s",
                (status, trace_quality, error, run_id),
            )

    def recent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT r.id,r.status,r.started_at,r.ended_at,r.scheduler_policy,r.worker_count,r.trace_mode,
                   r.trace_quality,count(e.id) tests,count(e.id) FILTER (WHERE e.status='PASSED') passed,
                   count(e.id) FILTER (WHERE e.status!='PASSED') failed,
                   coalesce(sum(e.duration_seconds),0) test_seconds
            FROM runs r LEFT JOIN executions e ON e.run_id=r.id GROUP BY r.id ORDER BY r.started_at DESC LIMIT %s
        """
        try:
            with self._pool.connection() as connection:
                cursor = connection.execute(query, (max(1, min(limit, 500)),))
                rows = cursor.fetchall()
                if cursor.description is None:
                    raise StorageError("recent-runs query returned no column metadata")
                columns = [description.name for description in cursor.description]
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"could not read recent runs: {exc}") from exc
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def run_detail(self, run_id: str) -> Optional[dict[str, Any]]:
        try:
            with self._pool.connection() as connection:
                run = connection.execute(
                    "SELECT row_to_json(r) FROM runs r WHERE id=%s", (run_id,)
                ).fetchone()
                if not run:
                    return None
                executions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT row_to_json(e) FROM executions e WHERE run_id=%s ORDER BY started_at",
                        (run_id,),
                    )
                ]
                predictions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT row_to_json(p) FROM predictions p WHERE run_id=%s ORDER BY probability DESC LIMIT 5000",
                        (run_id,),
                    )
                ]
                result = cast(dict[str, Any], run[0])
                result["executions"] = executions
                result["predictions"] = predictions
                return result
        except Exception as exc:
            raise StorageError(f"could not read run {run_id}: {exc}") from exc


class ArtifactStore:
    """Durable local read model used when PostgreSQL services are not running."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_segment(value: str, label: str) -> str:
        safe = "".join(character for character in value if character.isalnum() or character in "-_")
        if not safe or safe != value:
            raise StorageError(f"artifact {label} contains unsafe characters")
        return safe

    def save(self, category: str, identifier: str, value: Any) -> Path:
        category = self._safe_segment(category, "category")
        identifier = self._safe_segment(identifier, "identifier")
        path = self.root / category / f"{identifier}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with self._lock:
            temporary.write_text(
                json.dumps(value, default=json_default, indent=2, sort_keys=True) + "\n"
            )
            temporary.replace(path)
        return path

    def load(self, category: str, identifier: str) -> Optional[dict[str, Any]]:
        category = self._safe_segment(category, "category")
        identifier = self._safe_segment(identifier, "identifier")
        path = self.root / category / f"{identifier}.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise StorageError(f"artifact {path} is corrupt: {exc}") from exc
        if not isinstance(value, dict):
            raise StorageError(f"artifact {path} must contain a JSON object")
        return cast(dict[str, Any], value)

    def list(self, category: str, limit: int = 50) -> list[dict[str, Any]]:
        category = self._safe_segment(category, "category")
        if limit <= 0:
            return []
        directory = self.root / category
        if not directory.exists():
            return []
        paths = sorted(
            directory.glob("*.json"), key=lambda value: value.stat().st_mtime, reverse=True
        )[: min(limit, 500)]
        values: list[dict[str, Any]] = []
        for path in paths:
            try:
                value = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise StorageError(f"artifact {path} is corrupt: {exc}") from exc
            if not isinstance(value, dict):
                raise StorageError(f"artifact {path} must contain a JSON object")
            values.append(cast(dict[str, Any], value))
        return values
