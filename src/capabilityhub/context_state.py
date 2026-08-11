"""Persistent, observable local state for disclosed context residency."""

from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import cast

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.residency import ContextInventory, Eviction, ResidentSection

_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ContextEntrySnapshot:
    key: str
    revision: str
    section: str
    portable_tokens: int
    reload_cost: int
    reuse_score: int
    sensitive: bool
    pinned: bool
    last_access: int


@dataclass(frozen=True, slots=True)
class ContextStateSnapshot:
    generation: int
    max_portable_tokens: int
    used_portable_tokens: int
    entries: tuple[ContextEntrySnapshot, ...]


@dataclass(frozen=True, slots=True)
class ContextStateEvent:
    sequence: int
    action: str
    key: str
    used_portable_tokens: int
    evictions: tuple[Eviction, ...] = ()


class LocalContextState:
    """Own one ContextInventory and atomically persist every successful change."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_portable_tokens: int,
        event_limit: int = 100,
    ) -> None:
        if not 1 <= event_limit <= 10_000:
            raise ValueError("event_limit must be from 1 to 10000")
        self._path = Path(path).resolve()
        self._lock = RLock()
        self._inventory = ContextInventory(max_portable_tokens)
        self._generation = 0
        self._events: deque[ContextStateEvent] = deque(maxlen=event_limit)
        if self._path.is_file():
            self._load(max_portable_tokens)

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> ContextStateSnapshot:
        with self._lock:
            return ContextStateSnapshot(
                generation=self._generation,
                max_portable_tokens=self._inventory.max_portable_tokens,
                used_portable_tokens=self._inventory.used_portable_tokens,
                entries=tuple(_entry_snapshot(item) for item in self._inventory.entries),
            )

    @property
    def events(self) -> tuple[ContextStateEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def add(self, entry: ResidentSection) -> tuple[Eviction, ...]:
        with self._lock:
            backup = self._backup()
            try:
                selected = _validated_clone(entry)
                evictions = self._inventory.add(selected)
                self._save(self._generation + 1)
            except Exception:
                self._restore(backup)
                raise
            self._generation += 1
            self._record("add", selected.key, evictions)
            return evictions

    def access(self, key: str) -> ContextEntrySnapshot:
        with self._lock:
            backup = self._backup()
            try:
                entry = self._inventory.access(key)
                self._save(self._generation + 1)
            except Exception:
                self._restore(backup)
                raise
            self._generation += 1
            self._record("access", key)
            return _entry_snapshot(entry)

    def pin(self, key: str, value: bool = True) -> None:
        if not isinstance(value, bool):
            raise TypeError("pin value must be a boolean")
        with self._lock:
            backup = self._backup()
            try:
                self._inventory.pin(key, value)
                self._save(self._generation + 1)
            except Exception:
                self._restore(backup)
                raise
            self._generation += 1
            self._record("pin" if value else "unpin", key)

    def remove(self, key: str) -> Eviction:
        with self._lock:
            backup = self._backup()
            try:
                eviction = self._inventory.remove(key)
                self._save(self._generation + 1)
            except Exception:
                self._restore(backup)
                raise
            self._generation += 1
            self._record("remove", key, (eviction,))
            return eviction

    def _record(
        self,
        action: str,
        key: str,
        evictions: tuple[Eviction, ...] = (),
    ) -> None:
        self._events.append(
            ContextStateEvent(
                sequence=self._generation,
                action=action,
                key=key,
                used_portable_tokens=self._inventory.used_portable_tokens,
                evictions=evictions,
            )
        )

    def _backup(self) -> tuple[ResidentSection, ...]:
        return tuple(_clone(item) for item in self._inventory.entries)

    def _restore(self, entries: tuple[ResidentSection, ...]) -> None:
        restored = ContextInventory(self._inventory.max_portable_tokens)
        for entry in sorted(entries, key=lambda item: item.last_access):
            restored.add(_clone(entry))
        self._inventory = restored

    def _load(self, expected_limit: int) -> None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            generation, limit, entries = _decode(payload)
            if limit != expected_limit:
                raise ValueError("context token limit does not match persisted state")
            self._restore(entries)
            self._generation = generation
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise _state_error(
                "context_state_invalid",
                ErrorCategory.INPUT,
                "The persisted context state is invalid or incompatible.",
            ) from error

    def _save(self, generation: int) -> None:
        payload = {
            "entries": [asdict(_entry_snapshot(item)) for item in self._inventory.entries],
            "generation": generation,
            "max_portable_tokens": self._inventory.max_portable_tokens,
            "schema_version": _SCHEMA_VERSION,
        }
        temporary: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    payload,
                    stream,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            temporary = None
        except OSError as error:
            raise _state_error(
                "context_state_write_failed",
                ErrorCategory.INTERNAL,
                "The local context state could not be saved.",
            ) from error
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)


def _decode(payload: object) -> tuple[int, int, tuple[ResidentSection, ...]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported context state schema")
    generation = payload.get("generation")
    limit = payload.get("max_portable_tokens")
    raw_entries = payload.get("entries")
    if not _natural(generation) or not _positive(limit) or not isinstance(raw_entries, list):
        raise ValueError("invalid context state envelope")
    entries = tuple(_decode_entry(value) for value in raw_entries)
    if len({entry.key for entry in entries}) != len(entries):
        raise ValueError("duplicate resident section key")
    return cast(int, generation), cast(int, limit), entries


def _decode_entry(value: object) -> ResidentSection:
    if not isinstance(value, dict):
        raise ValueError("invalid resident section")
    required = ("key", "revision", "section")
    if any(not isinstance(value.get(name), str) or not value[name] for name in required):
        raise ValueError("invalid resident section identity")
    if not _natural(value.get("portable_tokens")):
        raise ValueError("invalid resident token count")
    if not _positive(value.get("reload_cost")) or not _positive(value.get("reuse_score")):
        raise ValueError("invalid resident scoring value")
    if not isinstance(value.get("sensitive"), bool) or not isinstance(value.get("pinned"), bool):
        raise ValueError("invalid resident flags")
    if not _natural(value.get("last_access")):
        raise ValueError("invalid resident access order")
    return ResidentSection(**value)


def _entry_snapshot(entry: ResidentSection) -> ContextEntrySnapshot:
    return ContextEntrySnapshot(**asdict(entry))


def _clone(entry: ResidentSection) -> ResidentSection:
    return ResidentSection(**asdict(entry))


def _validated_clone(entry: ResidentSection) -> ResidentSection:
    return _decode_entry(asdict(entry))


def _natural(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _state_error(code: str, category: ErrorCategory, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=category, safe_message=message)
