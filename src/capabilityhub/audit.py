"""Payload-minimizing audit events and sinks."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

from capabilityhub.models import JsonValue
from capabilityhub.tenancy import SqliteScopedState, TenantScope


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    sequence: int
    task_id: str
    event_type: str
    capability_revision: str | None
    outcome: str
    portable_tokens: int = 0
    payload_bytes: int = 0
    reason_codes: tuple[str, ...] = ()
    metadata: dict[str, JsonValue] | None = None


class AuditSink(Protocol):
    def emit(self, event: AuditEvent) -> None: ...


class MemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class JsonlAuditSink:
    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._lock = RLock()

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: AuditEvent) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = asdict(event)
            with self._path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())


class ScopedAuditSink:
    """Write a minimal audit projection into caller-scoped durable state."""

    def __init__(
        self,
        state: SqliteScopedState,
        *,
        tenant_id: str,
        principal_id: str,
        session_id: str,
        identity_source: str,
        delegate: AuditSink | None = None,
    ) -> None:
        self._state = state
        self._tenant_id = tenant_id
        self._principal_id = principal_id
        self._session_id = session_id
        self.identity_source = identity_source
        self._delegate = delegate

    def emit(self, event: AuditEvent) -> None:
        scope = TenantScope(
            self._tenant_id,
            self._principal_id,
            self._session_id,
            event.task_id,
        )
        self._state.append_event(
            scope,
            {
                "capability_revision": event.capability_revision,
                "event_type": event.event_type,
                "outcome": event.outcome,
                "payload_bytes": event.payload_bytes,
                "portable_tokens": event.portable_tokens,
                "reason_codes": list(event.reason_codes),
            },
            stream="audit",
        )
        if self._delegate is not None:
            self._delegate.emit(event)


def read_scoped_audit(
    state: SqliteScopedState,
    scope: TenantScope,
    *,
    limit: int = 50,
) -> tuple[AuditEvent, ...]:
    """Read only the authenticated scope; an absent foreign record is indistinguishable."""

    events = state.list_events(scope, stream="audit", limit=limit)
    result: list[AuditEvent] = []
    for stored in events:
        value = stored.value
        if not isinstance(value, dict):
            continue
        try:
            portable_tokens = value.get("portable_tokens", 0)
            payload_bytes = value.get("payload_bytes", 0)
            reason_codes = value.get("reason_codes", [])
            if (
                isinstance(portable_tokens, bool)
                or not isinstance(portable_tokens, int)
                or isinstance(payload_bytes, bool)
                or not isinstance(payload_bytes, int)
                or not isinstance(reason_codes, list)
            ):
                continue
            result.append(
                AuditEvent(
                    event_id=stored.event_id,
                    sequence=stored.sequence,
                    task_id=scope.task,
                    event_type=str(value["event_type"]),
                    capability_revision=(
                        str(value["capability_revision"])
                        if value.get("capability_revision") is not None
                        else None
                    ),
                    outcome=str(value["outcome"]),
                    portable_tokens=portable_tokens,
                    payload_bytes=payload_bytes,
                    reason_codes=tuple(str(item) for item in reason_codes),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(result)


def read_jsonl_audit(path: Path, *, limit: int = 50) -> tuple[AuditEvent, ...]:
    """Read a bounded safe tail, ignoring incomplete or invalid records."""

    if not 1 <= limit <= 500:
        raise ValueError("audit limit must be from 1 to 500")
    lines = _tail_lines(path.resolve(), limit)
    events: list[AuditEvent] = []
    for line in reversed(lines):
        try:
            data = json.loads(line)
            event = AuditEvent(
                event_id=data["event_id"],
                sequence=data["sequence"],
                task_id=data["task_id"],
                event_type=data["event_type"],
                capability_revision=data.get("capability_revision"),
                outcome=data["outcome"],
                portable_tokens=data.get("portable_tokens", 0),
                payload_bytes=data.get("payload_bytes", 0),
                reason_codes=tuple(data.get("reason_codes", ())),
                metadata=data.get("metadata"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(event.event_id, str) or not isinstance(event.sequence, int):
            continue
        events.append(event)
        if len(events) == limit:
            break
    events.reverse()
    return tuple(events)


def _tail_lines(path: Path, limit: int, *, max_bytes: int = 1_048_576) -> list[str]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            chunks: list[bytes] = []
            loaded = 0
            newlines = 0
            while position and loaded < max_bytes and newlines <= limit:
                size = min(8_192, position, max_bytes - loaded)
                position -= size
                stream.seek(position)
                chunk = stream.read(size)
                chunks.append(chunk)
                loaded += len(chunk)
                newlines += chunk.count(b"\n")
    except FileNotFoundError:
        return []
    content = b"".join(reversed(chunks))
    if position:
        first_newline = content.find(b"\n")
        content = b"" if first_newline < 0 else content[first_newline + 1 :]
    return content.decode("utf-8", errors="replace").splitlines()
