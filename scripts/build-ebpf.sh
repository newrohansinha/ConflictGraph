#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "ConflictGraph eBPF programs can only be built and validated on Linux." >&2
  exit 2
fi

command -v cargo >/dev/null || { echo "cargo is required" >&2; exit 2; }
command -v clang >/dev/null || { echo "clang is required" >&2; exit 2; }
command -v bpf-linker >/dev/null || { echo "bpf-linker is required (cargo install bpf-linker)" >&2; exit 2; }

task_target="bpfel-unknown-none"
task_profile="release"
cargo +nightly build \
  --manifest-path tracer/ebpf/Cargo.toml \
  --target "${task_target}" \
  --profile "${task_profile}" \
  -Z build-std=core

task_object="tracer/ebpf/target/${task_target}/${task_profile}/conflictgraph-ebpf"
task_output="tracer/target/${task_target}/${task_profile}/conflictgraph-ebpf"
mkdir -p "$(dirname "${task_output}")"
cp "${task_object}" "${task_output}"
cargo build --manifest-path tracer/Cargo.toml --release
echo "Built ${task_output}"
echo "Built tracer/target/release/conflictgraph-tracer"
