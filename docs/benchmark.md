# Benchmark methodology

`benchmark/suite` contains 280 collectable pytest cases. It includes shared and isolated files, directories, SQLite databases, TCP ports, Unix sockets, logical Redis keys, process contention, and read-only controls. The synthetic replay generator builds related graph families at larger profile sizes.

`conflictgraph benchmark` does not execute those pytest cases or load eBPF. It simulates first-attempt outcomes from a seeded synthetic world and reports `replayed: true`. Every scheduling strategy receives the same tests, durations, conflict probabilities, worker count, and trial seeds.

The comparison includes serial, random parallel, semantic heuristic, conservative locking, and an oracle upper bound. Tabular ML is added when its dependencies are installed. A GNN row is added only when a valid checksummed model artifact is available; otherwise the report records why it is absent.

Reports contain execution count, conflict failures, flake rate with a trial-level bootstrap confidence interval, makespan, worker utilization, risk exposure, and scheduling latency. Quick is for local regression feedback, standard for broader comparison, and full for higher-trial simulation. Generated JSON and Markdown reports are ignored by Git.

The source pytest suite is a separate executed fixture. Install the `benchmark` extra and start the Compose `benchmark` profile to include Redis cases; otherwise those cases skip explicitly. A normal serial run checks that cases are isolated when they do not overlap. Concurrency-induced failure rates require a harness that launches known conflicting pairs together.

Replay results answer whether scheduling logic behaves sensibly under controlled assumptions. They do not measure kernel overhead, real-world attribution accuracy, or production flake reduction. Those require repeated serial and concurrent executions on a representative Linux runner, with tracing enabled and disabled when measuring overhead.
