from __future__ import annotations

import socket
import time

import pytest

FIXED_BASE = 38100


@pytest.mark.conflict_family("tcp-port")
@pytest.mark.parametrize(
    "group,role",
    [(group, role) for group in range(12) for role in ("api", "health")],
    ids=lambda value: str(value),
)
def test_fixed_port_service_collision(group: int, role: str, rng) -> None:
    """Two logical service checks in each group intentionally bind one fixed port."""
    time.sleep(rng.uniform(0.002, 0.025))
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        server.bind(("127.0.0.1", FIXED_BASE + group))
        server.listen(2)
        time.sleep(rng.uniform(0.035, 0.075))
    finally:
        server.close()


@pytest.mark.conflict_family("safe-dynamic-port")
@pytest.mark.parametrize("case", range(16))
def test_dynamic_port_service_isolated(case: int, rng) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        client = socket.create_connection(("127.0.0.1", port), timeout=1)
        accepted, _ = server.accept()
        client.sendall(f"case={case}".encode())
        assert accepted.recv(64) == f"case={case}".encode()
        accepted.close()
        client.close()
        time.sleep(rng.uniform(0, 0.008))
    finally:
        server.close()
