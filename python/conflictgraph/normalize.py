from __future__ import annotations

import hashlib
import ipaddress
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, Optional

from .types import AccessMode, Operation, ResourceIdentity, ResourceType, TraceEvent

_EPHEMERAL_SEGMENTS = (
    re.compile(r"^pytest-of-[^/]+$"),
    re.compile(r"^pytest-\d+$"),
    re.compile(r"^tmp[a-zA-Z0-9_-]{6,}$"),
)


@dataclass
class NormalizationPolicy:
    exclude_prefixes: tuple[str, ...] = ("/proc", "/sys", "/usr/lib", "/lib", "/System")
    redact_paths: bool = False
    hash_salt: str = ""
    repository_root: Optional[str] = None
    include_readonly_system: bool = False
    ignored_suffixes: tuple[str, ...] = (".pyc", ".so", ".dylib")
    loopback_alias: str = "127.0.0.1"


class ResourceNormalizer:
    """Converts raw kernel/application identifiers into stable, privacy-aware resources."""

    def __init__(self, policy: Optional[NormalizationPolicy] = None) -> None:
        self.policy = policy or NormalizationPolicy()

    def normalize_event(self, event: TraceEvent) -> Optional[TraceEvent]:
        identifier = self.normalize_identifier(
            event.resource_type, event.resource_identifier, event.operation
        )
        if identifier is None:
            return None
        return TraceEvent(
            execution_id=event.execution_id,
            test_id=event.test_id,
            timestamp_ns=event.timestamp_ns,
            pid=event.pid,
            tid=event.tid,
            cgroup_id=event.cgroup_id,
            resource_type=event.resource_type,
            resource_identifier=identifier,
            operation=event.operation,
            access_mode=event.access_mode,
            source=event.source,
            metadata=event.metadata,
            sequence=event.sequence,
        )

    def normalize_identifier(
        self, kind: ResourceType, raw: str, operation: Operation
    ) -> Optional[str]:
        if kind in {
            ResourceType.FILE,
            ResourceType.DIRECTORY,
            ResourceType.FILE_LOCK,
            ResourceType.UNIX_SOCKET,
            ResourceType.DATABASE_RESOURCE,
        }:
            return self._path(raw, operation)
        if kind in {ResourceType.TCP_ENDPOINT, ResourceType.UDP_ENDPOINT}:
            return self._endpoint(raw, kind)
        if kind == ResourceType.REDIS_KEY:
            return self._redis(raw)
        if kind == ResourceType.PROCESS:
            return self._process(raw)
        stripped = raw.strip()
        return stripped if stripped else None

    def identity_for(self, event: TraceEvent) -> ResourceIdentity:
        return ResourceIdentity.create(
            event.resource_type, event.resource_identifier, self.policy.redact_paths
        )

    def _path(self, raw: str, operation: Operation) -> Optional[str]:
        value = raw.strip().replace("\\", "/")
        if not value or value in {"(null)", "<unknown>"}:
            return None
        if not value.startswith("/"):
            value = "/" + value
        value = posixpath.normpath(value)
        prefixes = tuple(posixpath.normpath(prefix) for prefix in self.policy.exclude_prefixes)
        if operation == Operation.READ and not self.policy.include_readonly_system:
            if any(value == prefix or value.startswith(prefix + "/") for prefix in prefixes):
                return None
            if value.endswith(self.policy.ignored_suffixes):
                return None
        root = self.policy.repository_root
        if root:
            normalized_root = posixpath.normpath(root.replace("\\", "/"))
            if value == normalized_root or value.startswith(normalized_root + "/"):
                value = "$REPO" + value[len(normalized_root) :]
        # Preserve random temp-directory isolation: only canonicalize known runner containers,
        # never arbitrary numeric/name segments that could be deliberate resource identities.
        parts = list(PurePosixPath(value).parts)
        for index, part in enumerate(parts):
            if any(pattern.match(part) for pattern in _EPHEMERAL_SEGMENTS):
                parts[index] = "$RUN_TMP"
        value = str(PurePosixPath(*parts))
        if self.policy.redact_paths:
            digest = hashlib.sha256((self.policy.hash_salt + "\0" + value).encode()).hexdigest()
            return f"sha256:{digest}"
        return value

    def _endpoint(self, raw: str, kind: ResourceType) -> Optional[str]:
        value = raw.strip().lower()
        protocol = "tcp" if kind == ResourceType.TCP_ENDPOINT else "udp"
        if value.startswith(protocol + ":"):
            value = value[len(protocol) + 1 :]
        if value.startswith("["):
            closing = value.find("]")
            if closing < 0:
                return None
            host, port = value[1:closing], value[closing + 1 :].lstrip(":")
        else:
            host, separator, port = value.rpartition(":")
            if not separator:
                return None
        try:
            parsed_port = int(port)
            if not 0 <= parsed_port <= 65535:
                return None
            address = ipaddress.ip_address(host or "0.0.0.0")
            if address.is_loopback:
                host = self.policy.loopback_alias
            else:
                host = address.compressed
        except ValueError:
            return None
        return f"{protocol.upper()}:{host}:{parsed_port}"

    @staticmethod
    def _redis(raw: str) -> Optional[str]:
        value = raw.strip()
        if not value:
            return None
        db = "0"
        key = value
        if ":" in value and value.split(":", 1)[0].isdigit():
            db, key = value.split(":", 1)
        if not key:
            return None
        return f"REDIS:{db}:{key}"

    @staticmethod
    def _process(raw: str) -> Optional[str]:
        value = raw.strip()
        if not value:
            return None
        executable = os.path.basename(value.split("\0", 1)[0].split()[0])
        return f"EXEC:{executable}" if executable else None


def is_mutating(operation: Operation, mode: AccessMode) -> bool:
    return operation in {
        Operation.WRITE,
        Operation.CREATE,
        Operation.DELETE,
        Operation.RENAME,
        Operation.BIND,
        Operation.LOCK,
    } or mode in {AccessMode.WRITE, AccessMode.EXCLUSIVE}


def normalize_events(
    events: Iterable[TraceEvent], normalizer: ResourceNormalizer
) -> list[TraceEvent]:
    output: list[TraceEvent] = []
    for event in events:
        normalized = normalizer.normalize_event(event)
        if normalized is not None:
            output.append(normalized)
    output.sort(key=lambda item: (item.timestamp_ns, item.sequence))
    return output
