#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this controlled eBPF integration test as root." >&2
  exit 2
fi
if [[ ! -f /sys/fs/cgroup/cgroup.controllers ]]; then
  echo "cgroup v2 is required." >&2
  exit 2
fi

task_artifacts="artifacts/ebpf-integration"
mkdir -p "${task_artifacts}"
timeout 20s tracer/target/release/conflictgraph-tracer \
  --mode ebpf \
  --output "${task_artifacts}/events.jsonl" \
  --metrics-address 127.0.0.1:0 \
  --control-socket /run/conflictgraph/test-tracer.sock &
task_tracer_pid=$!
trap 'kill ${task_tracer_pid} 2>/dev/null || true' EXIT

for attempt in $(seq 1 30); do
  [[ -S /run/conflictgraph/test-tracer.sock ]] && break
  sleep 0.1
done

python3 - <<'PY'
import json
import os
from pathlib import Path
import socket
import time

cgroup_path = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1].lstrip('/')
control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
control.connect('/run/conflictgraph/test-tracer.sock')
control.sendall((json.dumps({
    'action': 'register',
    'execution_id': 'ebpf-integration',
    'test_id': 'ebpf-integration',
    'cgroup_id': cgroup_path.stat().st_ino,
    'root_pid': os.getpid(),
}) + '\n').encode())
path = Path('/tmp/conflictgraph-ebpf-known-file')
path.write_text('metadata-only')
server = socket.socket()
server.bind(('127.0.0.1', 0))
server.close()
path.unlink()
time.sleep(0.5)
control.sendall(b'{"action":"unregister","execution_id":"ebpf-integration"}\n')
control.close()
PY

sleep 1
kill "${task_tracer_pid}"
wait "${task_tracer_pid}" || true
test -s "${task_artifacts}/events.jsonl"
grep -q 'conflictgraph-ebpf-known-file' "${task_artifacts}/events.jsonl"
grep -q 'TCP:' "${task_artifacts}/events.jsonl"
