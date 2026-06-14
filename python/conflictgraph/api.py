from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from .config import Config, load_config
from .storage import ArtifactStore, PostgresStore, StorageError


def create_app(config: Optional[Config] = None) -> Any:
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.responses import JSONResponse
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            Counter,
            Histogram,
            generate_latest,
        )
        from starlette.responses import Response
    except ImportError as exc:
        raise RuntimeError("API dependencies are missing; install conflictgraph[api]") from exc

    settings = config or load_config()
    artifact_store = ArtifactStore(Path(settings.artifact_dir))
    postgres: Optional[PostgresStore] = None
    metrics_registry = CollectorRegistry()
    request_count = Counter(
        "conflictgraph_api_requests_total",
        "API requests",
        ("method", "route", "status"),
        registry=metrics_registry,
    )
    latency = Histogram(
        "conflictgraph_api_request_duration_seconds",
        "API request latency",
        ("method", "route"),
        registry=metrics_registry,
    )

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        nonlocal postgres
        try:
            postgres = PostgresStore(settings.database_url)
            postgres.open()
            app.state.database_status = "connected"
        except StorageError as exc:
            postgres = None
            app.state.database_status = f"unavailable: {exc}"
        yield
        if postgres:
            postgres.close()

    app = FastAPI(title="ConflictGraph API", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def metrics_middleware(request: Any, call_next: Any) -> Any:
        started = time.perf_counter()
        status = 500
        route = request.url.path
        if route.startswith("/api/v1/runs/"):
            route = "/api/v1/runs/{id}"
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            request_count.labels(request.method, route, str(status)).inc()
            latency.labels(request.method, route).observe(time.perf_counter() - started)

    @app.exception_handler(StorageError)
    async def storage_exception(_: Any, exc: StorageError) -> Any:
        return JSONResponse(
            status_code=503, content={"error": "storage_unavailable", "detail": str(exc)}
        )

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        database_status = app.state.database_status
        return {
            "status": "ok" if database_status == "connected" else "degraded",
            "version": "0.1.0",
            "database": database_status,
            "tracing_mode": settings.tracing.mode,
            "model": "trained"
            if (Path(settings.model.artifact) / "metadata.json").exists()
            else "heuristic-fallback",
        }

    @app.get("/metrics")
    async def metrics() -> Any:
        return Response(generate_latest(metrics_registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/v1/runs")
    async def runs(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        if postgres:
            return postgres.recent_runs(limit)
        return artifact_store.list("runs", limit)

    @app.get("/api/v1/runs/{run_id}")
    async def run_detail(run_id: str) -> dict[str, Any]:
        value = postgres.run_detail(run_id) if postgres else artifact_store.load("runs", run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="run not found")
        return value

    @app.get("/api/v1/benchmarks")
    async def benchmarks(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
        reports = artifact_store.list("benchmarks", limit)
        default = Path(settings.artifact_dir) / "benchmark" / "benchmark.json"
        if default.exists():
            try:
                current = json.loads(default.read_text())
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=503, detail="benchmark report is corrupt") from exc
            if not isinstance(current, dict):
                raise HTTPException(status_code=503, detail="benchmark report must be an object")
            if not any(item.get("created_at") == current.get("created_at") for item in reports):
                reports.insert(0, current)
        return reports[:limit]

    @app.get("/api/v1/models")
    async def models() -> list[dict[str, Any]]:
        metadata = Path(settings.model.artifact) / "metadata.json"
        if not metadata.exists():
            return []
        try:
            value = json.loads(metadata.read_text())
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=503, detail="active model metadata is corrupt"
            ) from None
        if not isinstance(value, dict):
            raise HTTPException(status_code=503, detail="active model metadata must be an object")
        return [value]

    @app.get("/api/v1/graph")
    async def graph(
        min_risk: float = Query(0.25, ge=0, le=1), limit: int = Query(2000, ge=1, le=10000)
    ) -> dict[str, Any]:
        value = artifact_store.load("graphs", "latest")
        if not value:
            return {"tests": [], "resources": [], "edges": [], "predictions": [], "empty": True}
        raw_predictions = value.get("predictions", [])
        if not isinstance(raw_predictions, list) or not all(
            isinstance(item, dict) for item in raw_predictions
        ):
            raise HTTPException(status_code=503, detail="graph predictions are malformed")
        if any(
            not isinstance(item.get("probability", 0), (int, float)) for item in raw_predictions
        ):
            raise HTTPException(status_code=503, detail="graph prediction probability is malformed")
        predictions = [item for item in raw_predictions if item.get("probability", 0) >= min_risk][
            :limit
        ]
        return {**value, "predictions": predictions}

    return app
