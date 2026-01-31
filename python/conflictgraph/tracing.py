from __future__ import annotations

import contextlib
import json
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TextIO

from .errors import TraceQualityError
from .normalize import ResourceNormalizer
from .types import AccessMode, EventSource, Operation, ResourceType, TraceEvent, TraceQuality


@dataclass
class ExecutionRegistration:
    execution_id: str
    test_id: str
    cgroup_id: int = 0
    process_group_id: int = 0
    root_pid: int = 0
    registered_at_ns: int = 0


class AttributionRegistry:
    """Thread-safe cgroup-first attribution with process ancestry fallback."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cgroups: dict[int, ExecutionRegistration] = {}
        self._pids: dict[int, ExecutionRegistration] = {}
        self._parents: dict[int, int] = {}

    def register(self, registration: ExecutionRegistration) -> None:
        with self._lock:
            if registration.cgroup_id:
                self._cgroups[registration.cgroup_id] = registration
            if registration.root_pid:
                self._pids[registration.root_pid] = registration

    def register_fork(self, parent_pid: int, child_pid: int) -> None:
        with self._lock:
            self._parents[child_pid] = parent_pid
            if parent_pid in self._pids:
                self._pids[child_pid] = self._pids[parent_pid]

    def unregister(self, execution_id: str) -> None:
        with self._lock:
            self._cgroups = {
                key: value
                for key, value in self._cgroups.items()
                if value.execution_id != execution_id
            }
            removed = {
                key for key, value in self._pids.items() if value.execution_id == execution_id
            }
            self._pids = {
                key: value
                for key, value in self._pids.items()
                if value.execution_id != execution_id
            }
            self._parents = {
                child: parent
                for child, parent in self._parents.items()
                if child not in removed and parent not in removed
            }

    def resolve(self, cgroup_id: int, pid: int) -> Optional[ExecutionRegistration]:
        with self._lock:
            if cgroup_id and cgroup_id in self._cgroups:
                return self._cgroups[cgroup_id]
            seen: set[int] = set()
            current = pid
            while current and current not in seen:
                seen.add(current)
                if current in self._pids:
                    registration = self._pids[current]
                    self._pids[pid] = registration
                    return registration
                current = self._parents.get(current, 0)
            return None


class EventSink:
    def __init__(
        self, path: Path, capacity: int = 65536, normalizer: Optional[ResourceNormalizer] = None
    ) -> None:
        self.path = path
        self.capacity = capacity
        self.normalizer = normalizer or ResourceNormalizer()
        self.quality = TraceQuality()
        self._queue: queue.Queue[Optional[TraceEvent]] = queue.Queue(capacity)
        self._thread: Optional[threading.Thread] = None
        self._failure: Optional[BaseException] = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._consume, name="trace-event-writer", daemon=True
        )
        self._thread.start()

    def submit(self, event: TraceEvent) -> bool:
        self.quality.captured += 1
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            self.quality.dropped += 1
            return False

    def close(self, timeout: float = 10.0) -> TraceQuality:
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("trace writer did not stop within timeout")
        if self._failure:
            raise RuntimeError("trace writer failed") from self._failure
        return self.quality

    def _consume(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as output:
                while True:
                    event = self._queue.get()
                    if event is None:
                        break
                    normalized = self.normalizer.normalize_event(event)
                    if normalized is None:
                        continue
                    output.write(json.dumps(normalized.to_dict(), sort_keys=True) + "\n")
                    self.quality.processed += 1
                    if not normalized.test_id:
                        self.quality.unattributed += 1
        except BaseException as exc:
            self._failure = exc


class ReplayTracer:
    """Feeds recorded JSONL events through the same normalized ingestion interface."""

    def __init__(self, normalizer: Optional[ResourceNormalizer] = None) -> None:
        self.normalizer = normalizer or ResourceNormalizer()
        self.quality = TraceQuality()

    def replay(
        self, source: Path | TextIO, consumer: Callable[[TraceEvent], None], speed: float = 0.0
    ) -> TraceQuality:
        close = False
        stream: TextIO
        if isinstance(source, Path):
            stream = source.open(encoding="utf-8")
            close = True
        else:
            stream = source
        last_timestamp: Optional[int] = None
        try:
            for line in stream:
                stripped = line.strip()
                if not stripped:
                    continue
                self.quality.captured += 1
                try:
                    event = TraceEvent.from_dict(json.loads(stripped))
                    normalized = self.normalizer.normalize_event(event)
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    self.quality.parse_failures += 1
                    continue
                if normalized is None:
                    continue
                if speed and last_timestamp is not None:
                    delay = max(0, normalized.timestamp_ns - last_timestamp) / 1e9 / speed
                    if delay:
                        time.sleep(min(delay, 1.0))
                consumer(normalized)
                self.quality.processed += 1
                if not normalized.test_id:
                    self.quality.unattributed += 1
                last_timestamp = normalized.timestamp_ns
        finally:
            if close:
                stream.close()
        return self.quality


def read_events(
    path: Path, normalizer: Optional[ResourceNormalizer] = None
) -> tuple[list[TraceEvent], TraceQuality]:
    events: list[TraceEvent] = []
    tracer = ReplayTracer(normalizer)
    quality = tracer.replay(path, events.append)
    return events, quality


def require_quality(quality: TraceQuality, minimum: float = 0.8) -> None:
    if quality.score < minimum:
        raise TraceQualityError(
            f"Trace quality {quality.score:.1%} is below required {minimum:.1%}: "
            f"{quality.dropped} dropped, {quality.unattributed} unattributed, "
            f"{quality.parse_failures} parse failures"
        )


class LogicalResourceClient:
    """Unix datagram adapter used by supported clients to emit logical resources."""

    def __init__(
        self, socket_path: str, execution_id: Optional[str] = None, test_id: Optional[str] = None
    ) -> None:
        self.socket_path = socket_path
        self.execution_id = (
            execution_id
            if execution_id is not None
            else os.getenv("CONFLICTGRAPH_EXECUTION_ID") or ""
        )
        self.test_id = test_id if test_id is not None else os.getenv("CONFLICTGRAPH_TEST_ID") or ""

    def emit(
        self,
        resource_type: ResourceType,
        identifier: str,
        operation: Operation,
        mode: AccessMode,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        event = TraceEvent(
            self.execution_id,
            self.test_id,
            time.time_ns(),
            os.getpid(),
            threading.get_native_id(),
            0,
            resource_type,
            identifier,
            operation,
            mode,
            EventSource.REDIS_ADAPTER,
            metadata or {},
        )
        payload = json.dumps(event.to_dict(), separators=(",", ":")).encode()
        if len(payload) > 8192:
            raise ValueError("logical resource event exceeds 8 KiB")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            client.sendto(payload, self.socket_path)
        finally:
            client.close()


class RedisTelemetry:
    def __init__(self, client: LogicalResourceClient, database: int = 0) -> None:
        self.client, self.database = client, database

    def read(self, key: str) -> None:
        self.client.emit(
            ResourceType.REDIS_KEY, f"{self.database}:{key}", Operation.READ, AccessMode.READ
        )

    def write(self, key: str) -> None:
        self.client.emit(
            ResourceType.REDIS_KEY, f"{self.database}:{key}", Operation.WRITE, AccessMode.WRITE
        )

    def delete(self, key: str) -> None:
        self.client.emit(
            ResourceType.REDIS_KEY, f"{self.database}:{key}", Operation.DELETE, AccessMode.WRITE
        )

    def flush_database(self) -> None:
        self.client.emit(
            ResourceType.REDIS_KEY,
            f"{self.database}:*",
            Operation.DELETE,
            AccessMode.EXCLUSIVE,
            {"scope": "database"},
        )


@contextlib.contextmanager
def event_sink(path: Path, capacity: int = 65536) -> Iterator[EventSink]:
    sink = EventSink(path, capacity)
    sink.start()
    try:
        yield sink
    finally:
        sink.close()
