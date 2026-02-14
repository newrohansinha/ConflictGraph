from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config


@dataclass
class Diagnostic:
    name: str
    status: str
    detail: str
    remediation: str = ""


def run_diagnostics(config: Config) -> list[Diagnostic]:
    output: list[Diagnostic] = []
    linux = platform.system() == "Linux"
    output.append(
        Diagnostic(
            "operating-system",
            "pass" if linux else "warn",
            platform.platform(),
            "Use a modern Linux host for real eBPF tracing." if not linux else "",
        )
    )
    cgroup = Path("/sys/fs/cgroup/cgroup.controllers")
    output.append(
        Diagnostic(
            "cgroup-v2",
            "pass" if cgroup.exists() else "warn",
            "available" if cgroup.exists() else "not detected",
            "Mount cgroup v2 and boot with unified hierarchy."
            if linux and not cgroup.exists()
            else "",
        )
    )
    btf = Path("/sys/kernel/btf/vmlinux")
    output.append(
        Diagnostic(
            "kernel-btf",
            "pass" if btf.exists() else "warn",
            "available" if btf.exists() else "not detected",
            "Install kernel BTF data or configure compatible headers."
            if linux and not btf.exists()
            else "",
        )
    )
    for binary in ("docker", "go", "cargo", "node", "python3"):
        found = shutil.which(binary)
        output.append(
            Diagnostic(
                binary,
                "pass" if found else "warn",
                found or "not found",
                f"Install {binary} to build all project components." if not found else "",
            )
        )
    model = Path(config.model.artifact) / "metadata.json"
    output.append(
        Diagnostic(
            "model-artifact",
            "pass" if model.exists() else "warn",
            str(model) if model.exists() else "no trained model; heuristic fallback active",
            "Run `conflictgraph train` after building a dataset." if not model.exists() else "",
        )
    )
    return output
