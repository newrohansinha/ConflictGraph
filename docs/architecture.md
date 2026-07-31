# Architecture

ConflictGraph separates observation, analysis, and execution so each boundary is inspectable.

1. The Go runner collects stable pytest node IDs and executes each scheduled test as a direct subprocess.
2. In eBPF mode, it places executions in cgroup v2 and registers the execution, test ID, cgroup inode, and root PID with the node-local Rust tracer.
3. The tracer emits normalized JSONL events. Replay mode feeds recorded events through the same userspace normalization path.
4. The Python replay command aggregates a bipartite test-resource graph and writes pair predictions to `artifacts/graphs/latest.json`.
5. Go and Python schedulers load that artifact, discard pairs outside the current collection, and use the remaining probabilities during placement.
6. The persistent Go path stores run, prediction, schedule, and execution records in PostgreSQL. The Python API combines those records with graph, model, and benchmark artifacts for the dashboard.

Stable test IDs are SHA-256 digests of repository, framework, and pytest node ID. Execution and run IDs are UUIDs. This keeps historical graph identity independent of process IDs and worker placement.

The graph contains typed test and resource nodes plus aggregated access edges. Pair features cover access modes, resource type and rarity, overlap, duration, failure history, and known concurrency outcomes. Candidate generation starts from shared resources or historical evidence instead of constructing every possible pair.

Scheduling is static for a collected run. Longest-processing-time ordering limits tail latency; predicted overlap contributes a duration-weighted cost; a policy threshold can delay a high-risk placement; bounded adjacent refinement improves the final objective for smaller suites. The seed controls deterministic tie-breaking.

## Failure boundaries

- A missing graph artifact selects duration-only scheduling. A malformed artifact stops planning or execution.
- A missing model selects the semantic heuristic during trace replay. A present but invalid model emits a warning and selects the heuristic.
- A trace below the configured quality threshold is rejected before graph construction.
- The persistent runner refuses to start without PostgreSQL and records setup or execution failures after a run row exists.
- A test failure is stored as an execution result, marks the run failed, and produces a nonzero command exit.
- Dashboard or analysis API failure does not affect an already running test schedule.

The graph, model, trace, and benchmark lifecycle is file-based. PostgreSQL is the durable store for run execution state. Kubernetes deployments therefore need a shared artifact volume for the analysis API and any process producing or consuming graph artifacts.
