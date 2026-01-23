import hashlib

import pytest
from conflictgraph.normalize import NormalizationPolicy, ResourceNormalizer, is_mutating
from conflictgraph.types import AccessMode, Operation, ResourceType


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/tmp/../tmp/shared.db", "/tmp/shared.db"),
        ("tmp/app.sock", "/tmp/app.sock"),
        ("/workspace/repo/tests/a.py", "$REPO/tests/a.py"),
    ],
)
def test_path_normalization(raw, expected):
    normalizer = ResourceNormalizer(NormalizationPolicy(repository_root="/workspace/repo"))
    assert normalizer.normalize_identifier(ResourceType.FILE, raw, Operation.WRITE) == expected


def test_known_pytest_temp_container_is_stabilized():
    normalizer = ResourceNormalizer()
    value = normalizer.normalize_identifier(
        ResourceType.FILE, "/tmp/pytest-of-user/pytest-123/test/data.db", Operation.WRITE
    )
    assert value == "/tmp/$RUN_TMP/$RUN_TMP/test/data.db"


def test_arbitrary_isolated_paths_are_not_over_normalized():
    normalizer = ResourceNormalizer()
    left = normalizer.normalize_identifier(
        ResourceType.FILE, "/tmp/account-123/data.db", Operation.WRITE
    )
    right = normalizer.normalize_identifier(
        ResourceType.FILE, "/tmp/account-456/data.db", Operation.WRITE
    )
    assert left != right


def test_system_read_is_filtered_but_write_is_retained():
    normalizer = ResourceNormalizer()
    assert (
        normalizer.normalize_identifier(ResourceType.FILE, "/usr/lib/libc.so", Operation.READ)
        is None
    )
    assert (
        normalizer.normalize_identifier(ResourceType.FILE, "/usr/lib/dangerous", Operation.WRITE)
        == "/usr/lib/dangerous"
    )


def test_path_hashing_is_stable_and_salted():
    normalizer = ResourceNormalizer(NormalizationPolicy(redact_paths=True, hash_salt="pepper"))
    value = normalizer.normalize_identifier(ResourceType.FILE, "/tmp/secret-name", Operation.WRITE)
    assert value == "sha256:" + hashlib.sha256(b"pepper\0/tmp/secret-name").hexdigest()
    assert "secret" not in value


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("127.0.0.1:8080", "TCP:127.0.0.1:8080"),
        ("tcp:localhost:12", None),
        ("[::1]:443", "TCP:127.0.0.1:443"),
        ("0.0.0.0:65536", None),
    ],
)
def test_tcp_normalization(raw, expected):
    assert (
        ResourceNormalizer().normalize_identifier(ResourceType.TCP_ENDPOINT, raw, Operation.BIND)
        == expected
    )


def test_udp_has_protocol_identity():
    value = ResourceNormalizer().normalize_identifier(
        ResourceType.UDP_ENDPOINT, "127.0.0.1:53", Operation.BIND
    )
    assert value == "UDP:127.0.0.1:53"


@pytest.mark.parametrize(
    "raw,expected",
    [("user:123", "REDIS:0:user:123"), ("4:user:123", "REDIS:4:user:123"), ("", None)],
)
def test_redis_normalization(raw, expected):
    assert (
        ResourceNormalizer().normalize_identifier(ResourceType.REDIS_KEY, raw, Operation.WRITE)
        == expected
    )


@pytest.mark.parametrize(
    "operation,mode,expected",
    [
        (Operation.READ, AccessMode.READ, False),
        (Operation.WRITE, AccessMode.UNKNOWN, True),
        (Operation.CONNECT, AccessMode.UNKNOWN, False),
        (Operation.CONNECT, AccessMode.EXCLUSIVE, True),
    ],
)
def test_mutation_semantics(operation, mode, expected):
    assert is_mutating(operation, mode) is expected
