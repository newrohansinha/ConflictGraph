# Linux tracing

## Requirements

- Linux with cgroup v2 and BTF at `/sys/kernel/btf/vmlinux`.
- Nightly Rust with `rust-src`, Clang, and `bpf-linker`.
- bpffs mounted at `/sys/fs/bpf`.
- `BPF`, `PERFMON`, and `SYS_RESOURCE` capabilities where supported.

`conflictgraph doctor` checks these conditions separately. macOS and Windows support replay mode only.

## Attribution

The trusted coordinator writes newline-delimited messages to `/run/conflictgraph/tracer.sock`:

```json
{"action":"register","execution_id":"uuid","test_id":"stable-id","cgroup_id":1234,"root_pid":5678}
```

The cgroup inode is the primary identity. Root PID and observed fork events provide a bounded ancestry fallback. Unknown events increment the unattributed metric and are not assigned to another test. An unregister message removes the execution mapping after its process exits.

The logical-resource adapter sends normalized datagrams to `/run/conflictgraph/logical.sock`. `RedisTelemetry` uses this path to identify logical keys without decoding network traffic.

## Captured data

Tracepoints cover file open, unlink, rename, socket bind and connect, and process exec, fork, and exit. Kernel events use fixed-size identifiers and socket buffers. Userspace assigns access modes, filters noise, normalizes resources, applies optional repository-relative paths or salted hashes, and writes JSONL.

Read-only system and interpreter paths are filtered by default; mutations remain visible. File contents, network payloads, environment variables, tokens, and Redis protocol values are never captured.

## Quality and validation

Prometheus metrics include captured, processed, kernel/logical dropped, unattributed, parse-failure, and last-poll ring-buffer counts. Python trace quality combines completeness and attribution rate. `trace replay` rejects input below `tracing.minimum_quality`.

Build and run the privileged smoke test on a compatible Linux host:

```bash
./scripts/build-ebpf.sh
sudo ./scripts/test-ebpf.sh
```

The script registers its workload cgroup, writes and removes a known file, binds a loopback socket, and verifies both normalized resources in the output. This is a smoke test, not a tracing-overhead benchmark.
