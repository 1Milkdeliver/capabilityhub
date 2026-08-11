"""Payload-minimizing audit events and sinks."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from capabilityhub.models import JsonValue


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
        self._path = path

    def emit(self, event: AuditEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(event)
        with self._path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            stream.write("\n")
