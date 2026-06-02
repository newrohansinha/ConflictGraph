from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from dataclasses import asdict
from importlib.resources import as_file, files
from pathlib import Path
from typing import Optional

import typer

from .benchmark import build_synthetic_world, run_replay_benchmark, write_report
from .config import Config, load_config
from .dataset import DatasetBuilder, pair_disjoint_split
from .diagnostics import run_diagnostics
from .errors import ArtifactError, ConflictGraphError
from .executor import PytestAdapter, PytestConfig, ScheduleExecutor
from .graph import HeuristicPredictor, TestResourceGraph
from .normalize import NormalizationPolicy, ResourceNormalizer
from .scheduler import ConflictScheduler
from .storage import ArtifactStore
from .tracing import read_events, require_quality
from .types import ConflictCause, PairPrediction, RiskPolicy, TestIdentity, TestStats, dump_json

app = typer.Typer(
    help="Detect test interference and schedule reliable parallel CI runs.", no_args_is_help=True
)
dataset_app = typer.Typer(help="Build and inspect pairwise ML datasets.")
model_app = typer.Typer(help="Train and evaluate prediction models.")
trace_app = typer.Typer(help="Collect, replay, and summarize resource traces.")
app.add_typer(dataset_app, name="dataset")
app.add_typer(model_app, name="model")
app.add_typer(trace_app, name="trace")


def _config(path: Path) -> Config:
    try:
        return load_config(path)
    except ConflictGraphError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None


def _graph_predictions(settings: Config) -> list[PairPrediction]:
    graph = ArtifactStore(Path(settings.artifact_dir)).load("graphs", "latest")
    if graph is None:
        return []
    raw = graph.get("predictions", [])
    if not isinstance(raw, list):
        raise ArtifactError("latest graph predictions must be a list")
    predictions: list[PairPrediction] = []
    try:
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("prediction must be an object")
            resources = item.get("shared_resources", [])
            if not isinstance(resources, list) or not all(
                isinstance(resource, str) for resource in resources
            ):
                raise TypeError("shared_resources must be a list of strings")
            predictions.append(
                PairPrediction(
                    str(item["test_a"]),
                    str(item["test_b"]),
                    float(item["probability"]),
                    ConflictCause(str(item.get("cause", ConflictCause.UNKNOWN.value))),
                    str(item.get("model_version", "unknown")),
                    resources,
                    str(item.get("explanation", "")),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError(f"latest graph contains an invalid prediction: {exc}") from exc
    return predictions


def _predictions_for_tests(settings: Config, tests: list[TestIdentity]) -> list[PairPrediction]:
    known = {test.id for test in tests}
    return [
        prediction
        for prediction in _graph_predictions(settings)
        if prediction.test_a in known and prediction.test_b in known
    ]


def _worker_count(requested: Optional[int], configured: int) -> int:
    workers = configured if requested is None else requested
    if not 1 <= workers <= 1024:
        raise typer.BadParameter("workers must be between 1 and 1024", param_hint="workers")
    return workers


def _normalizer(settings: Config) -> ResourceNormalizer:
    return ResourceNormalizer(
        NormalizationPolicy(
            exclude_prefixes=tuple(settings.tracing.exclude_prefixes),
            redact_paths=settings.tracing.redact_paths,
            hash_salt=settings.tracing.hash_salt,
        )
    )


@app.command()
def init(
    path: Path = typer.Option(Path("conflictgraph.yaml"), help="Configuration destination"),
    force: bool = False,
) -> None:
    """Initialize a validated local configuration."""
    if path.exists() and not force:
        typer.echo(f"error: {path} already exists; pass --force to replace it", err=True)
        raise typer.Exit(2)
    template = files("conflictgraph").joinpath("conflictgraph.example.yaml")
    with as_file(template) as source:
        shutil.copyfile(source, path)
    typer.echo(f"Created {path}. Replay mode is enabled; run `conflictgraph doctor` next.")


@app.command()
def doctor(
    config: Path = Path("conflictgraph.yaml"), json_output: bool = typer.Option(False, "--json")
) -> None:
    """Validate the host, build tools, tracing support, services, and model."""
    settings = _config(config)
    checks = run_diagnostics(settings)
    if json_output:
        typer.echo(json.dumps([asdict(item) for item in checks], indent=2))
        return
    for item in checks:
        marker = {"pass": "✓", "warn": "!", "fail": "✗"}[item.status]
        typer.echo(f"{marker} {item.name:18} {item.detail}")
        if item.remediation:
            typer.echo(f"  {item.remediation}")


@trace_app.command("summary")
def trace_summary(
    path: Path,
    config: Path = Path("conflictgraph.yaml"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    settings = _config(config)
    events, quality = read_events(path, _normalizer(settings))
    resources: dict[str, int] = {}
    tests: set[str] = set()
    for event in events:
        resources[event.resource_identifier] = resources.get(event.resource_identifier, 0) + 1
        tests.add(event.test_id)
    value = {
        "events": len(events),
        "tests": len(tests),
        "resources": len(resources),
        "quality": asdict(quality),
        "quality_score": quality.score,
        "top_resources": sorted(resources.items(), key=lambda item: item[1], reverse=True)[:20],
    }
    typer.echo(
        json.dumps(value, indent=2)
        if json_output
        else f"{len(events)} events, {len(tests)} tests, {len(resources)} resources, quality {quality.score:.1%}"
    )


@trace_app.command("replay")
def trace_replay(
    path: Path,
    output: Path = Path("artifacts/graphs/latest.json"),
    config: Path = Path("conflictgraph.yaml"),
) -> None:
    settings = _config(config)
    events, quality = read_events(path, _normalizer(settings))
    require_quality(quality, settings.tracing.minimum_quality)
    graph = TestResourceGraph.from_events(events)
    heuristic = HeuristicPredictor(settings.model.default_no_edge_risk).predict_graph(
        graph, include_readonly=True
    )
    predictions = heuristic
    prediction_mode = "heuristic"
    model_path = Path(settings.model.artifact)
    if (model_path / "metadata.json").exists():
        try:
            from .model import pair_predictions, predict_artifact

            inference_dataset = DatasetBuilder(settings.seed, negative_ratio=None).build(graph, [])
            probabilities, metadata = predict_artifact(model_path, graph, inference_dataset)
            predictions = pair_predictions(
                inference_dataset, probabilities, heuristic, metadata.version
            )
            prediction_mode = "model"
        except (ArtifactError, ImportError) as exc:
            typer.echo(f"warning: model unavailable, using heuristic predictions: {exc}", err=True)
    value = graph.to_dict()
    value["predictions"] = [asdict(item) for item in predictions]
    value["prediction_mode"] = prediction_mode
    value["trace_quality"] = asdict(quality)
    dump_json(value, output)
    typer.echo(
        f"Replayed {len(events)} events into {len(graph.resources)} resources and {len(predictions)} candidate pairs: {output}"
    )


@dataset_app.command("build-synthetic")
def dataset_build_synthetic(
    profile: str = "quick", output: Path = Path("artifacts/dataset"), seed: int = 42
) -> None:
    world = build_synthetic_world(profile, seed)
    dataset = DatasetBuilder(seed).build(world.graph, world.labels, [f"synthetic-{profile}"])
    split = pair_disjoint_split(dataset, seed)
    dataset.save(output)
    dump_json(asdict(split), output / "split.json")
    typer.echo(
        f"Wrote {len(dataset.examples)} examples ({dataset.metadata.positive_examples} positive) to {output}"
    )


@model_app.command("train")
def model_train(
    profile: str = "standard",
    output: Path = Path("artifacts/model"),
    seed: int = 42,
    epochs: int = 100,
) -> None:
    """Train tabular baselines and the hybrid PyTorch Geometric model."""
    from .model import GNNTrainer, TabularBaseline, TrainingConfig

    world = build_synthetic_world(profile, seed)
    dataset = DatasetBuilder(seed).build(world.graph, world.labels, [f"synthetic-{profile}"])
    split = pair_disjoint_split(dataset, seed)
    baseline = TabularBaseline("gradient_boosting", seed)
    baseline_metrics = baseline.fit(dataset, split)
    trainer = GNNTrainer(TrainingConfig(seed=seed, epochs=epochs))
    gnn_metrics = trainer.fit(world.graph, dataset, split)
    metadata = trainer.save(output, dataset, gnn_metrics)
    dump_json(
        {
            "tabular": {key: asdict(value) for key, value in baseline_metrics.items()},
            "gnn": {key: asdict(value) for key, value in gnn_metrics.items()},
        },
        output / "comparison.json",
    )
    typer.echo(
        f"Saved {metadata.model_type} {metadata.version}; test PR-AUC={gnn_metrics['test'].pr_auc:.3f}, Brier={gnn_metrics['test'].brier_score:.3f}"
    )


@app.command()
def collect(
    target: list[str] = typer.Argument(None),
    config: Path = Path("conflictgraph.yaml"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _config(config)
    tests = PytestAdapter(PytestConfig()).collect(target or [])
    if json_output:
        typer.echo(json.dumps([asdict(test) for test in tests], indent=2))
    else:
        for test in tests:
            typer.echo(f"{test.id}  {test.node_id}")
        typer.echo(f"Collected {len(tests)} tests")


@app.command()
def plan(
    target: list[str] = typer.Argument(None),
    workers: Optional[int] = None,
    policy: Optional[RiskPolicy] = None,
    config: Path = Path("conflictgraph.yaml"),
    output: Optional[Path] = None,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    settings = _config(config)
    tests = PytestAdapter(PytestConfig()).collect(target or [])
    stats = {test.id: TestStats() for test in tests}
    predictions = _predictions_for_tests(settings, tests)
    worker_count = _worker_count(workers, settings.workers)
    schedule = ConflictScheduler(
        worker_count,
        policy or settings.risk_policy,
        settings.seed,
        settings.scheduler.risk_weight,
        settings.scheduler.hard_threshold,
        settings.scheduler.refinement_rounds,
    ).static_schedule(tests, stats, predictions)
    value = asdict(schedule)
    if output:
        dump_json(value, output)
    if json_output:
        typer.echo(json.dumps(value, default=str, indent=2))
    else:
        for worker in range(schedule.workers):
            typer.echo(f"Worker {worker + 1}:")
            for item in (entry for entry in schedule.tests if entry.worker == worker):
                typer.echo(f"  {item.estimated_start:7.2f}s  {item.node_id}")
        typer.echo(
            f"Expected makespan {schedule.expected_makespan:.2f}s; scheduling {schedule.scheduler_latency_ms:.2f}ms"
        )


@app.command(name="run")
def run_tests(
    target: list[str] = typer.Argument(None),
    workers: Optional[int] = None,
    policy: Optional[RiskPolicy] = None,
    config: Path = Path("conflictgraph.yaml"),
    timeout: float = 300.0,
    fail_fast: bool = False,
) -> None:
    settings = _config(config)
    if timeout <= 0:
        raise typer.BadParameter("timeout must be positive", param_hint="timeout")
    adapter = PytestAdapter(PytestConfig(timeout_seconds=timeout))
    tests = adapter.collect(target or [])
    stats = {test.id: TestStats() for test in tests}
    predictions = _predictions_for_tests(settings, tests)
    worker_count = _worker_count(workers, settings.workers)
    run_id = str(uuid.uuid4())
    schedule = ConflictScheduler(
        worker_count,
        policy or settings.risk_policy,
        settings.seed,
        settings.scheduler.risk_weight,
        settings.scheduler.hard_threshold,
        settings.scheduler.refinement_rounds,
    ).static_schedule(tests, stats, predictions, run_id)
    results = asyncio.run(ScheduleExecutor(adapter).execute(schedule, fail_fast))
    passed = sum(result.status.value == "PASSED" for result in results)
    model_versions = {prediction.model_version for prediction in predictions}
    model_version = "duration-only"
    if len(model_versions) == 1:
        model_version = next(iter(model_versions))
    elif model_versions:
        model_version = "mixed"
    artifact = {
        "id": run_id,
        "status": "COMPLETED" if passed == len(results) else "FAILED",
        "scheduler_policy": schedule.policy.value,
        "worker_count": schedule.workers,
        "trace_mode": settings.tracing.mode,
        "model_version": model_version,
        "tests": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "test_seconds": sum(result.duration_seconds for result in results),
        "schedule": asdict(schedule),
        "executions": [asdict(result) for result in results],
        "predictions": [asdict(item) for item in predictions],
    }
    ArtifactStore(Path(settings.artifact_dir)).save("runs", run_id, artifact)
    typer.echo(f"Run {run_id}: {passed}/{len(results)} passed")
    if passed != len(results):
        raise typer.Exit(1)


@app.command()
def benchmark(
    profile: str = "quick",
    workers: int = 4,
    seed: int = 42,
    trials: Optional[int] = None,
    output: Path = Path("artifacts/benchmark"),
) -> None:
    report = run_replay_benchmark(profile, workers, seed, trials)
    write_report(report, output)
    report_id = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in report.created_at
    )
    ArtifactStore(output.parent).save("benchmarks", report_id, asdict(report))
    typer.echo(report.impact_summary)
    typer.echo(f"Reports: {output / 'benchmark.json'}, {output / 'benchmark.md'}")


@app.command()
def serve(
    config: Path = Path("conflictgraph.yaml"), host: str = "0.0.0.0", port: int = 8080
) -> None:
    try:
        import uvicorn
    except ImportError:
        typer.echo("error: install conflictgraph[api] to run the API", err=True)
        raise typer.Exit(2) from None
    from .api import create_app

    uvicorn.run(create_app(_config(config)), host=host, port=port)


@app.command()
def version() -> None:
    from . import __version__

    typer.echo(f"ConflictGraph {__version__}")


def main() -> None:
    try:
        app()
    except ConflictGraphError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None


if __name__ == "__main__":
    main()
