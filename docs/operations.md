# Operations

Docker Compose starts PostgreSQL, the Go API, the Python analysis API, the dashboard, Prometheus, and Grafana. The Linux tracer remains a host process because BPF and cgroup delegation vary by runtime and distribution.

PostgreSQL does not migrate implicitly at API startup. For a new Compose volume, the image runs `controlplane/migrations/001_initial.sql`. Apply the same SQL through the organization’s migration process for an existing or external database. The active schema stores tests, runs, executions, schedules, and pair predictions.

The Go health endpoint returns unavailable when PostgreSQL cannot be reached. The analysis API remains available with an explicit database status and can serve file-backed graph, model, benchmark, and local run artifacts. Both expose Prometheus request counts and latency metrics.

The Helm chart requires a secret containing the PostgreSQL URL and a `conflictgraph-tracer` secret containing `hash-salt`. It creates a shared artifact claim unless `artifacts.existingClaim` is set. The default access mode is `ReadWriteMany`; select a storage class that supports it or supply a compatible existing claim.

Set the four image values to tags published by your build pipeline. The defaults follow this repository's GHCR naming convention, but installing the chart does not publish images.

The chart deploys the tracer as a capability-scoped DaemonSet. Its control and logical-resource sockets are node-local. Test execution is intentionally not exposed as a network endpoint; the CI runner must be placed on the node, mount the runtime directory, and register its cgroups.

Operational signals to investigate include:

- Rising dropped events: increase queue capacity and inspect tracer CPU pressure.
- Rising unattributed events: verify cgroup v2, inode registration, process ancestry, and socket permissions.
- Scheduler latency growth: inspect candidate pair count and configured refinement rounds.
- Lower utilization without fewer failures: compare balanced and safe policy replays.
- Healthy model metrics with worse runs: check graph staleness, calibration drift, and trace quality.

Back up PostgreSQL and artifact storage together. Models and graph files are not recoverable from database run rows alone.
