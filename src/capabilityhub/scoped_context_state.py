"""Tenant-scoped, observable residency metadata backed by shared local state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import TypeVar, cast

from capabilityhub.context_state import (
    ContextEntrySnapshot,
    ContextStateEvent,
    ContextStateSnapshot,
)
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import JsonValue
from capabilityhub.residency import ContextInventory, Eviction, ResidentSection
from capabilityhub.tenancy import SqliteScopedState, TenantScope

_SCHEMA_VERSION = 1
_NAMESPACE = "context-residency"
_KEY = "inventory"
_Result = TypeVar("_Result")


class ScopedContextState:
    """Track disclosed-section metadata within exactly one opaque tenant scope.

    This controls CapabilityHub's residency bookkeeping only. It cannot remove text
    already retained by a model client or another external conversation system.
    """

    def __init__(
        self,
        state: SqliteScopedState,
        scope: TenantScope,
        *,
        max_portable_tokens: int,
        event_limit: int = 100,
    ) -> None:
        if max_portable_tokens <= 0:
            raise ValueError("max_portable_tokens must be positive")
        if not 1 <= event_limit <= 10_000:
            raise ValueError("event_limit must be from 1 to 10000")
        self._state = state
        self._scope = scope
        self._limit = max_portable_tokens
        self._event_limit = event_limit
        self.snapshot()  # Fail closed on incompatible persisted state.

    def snapshot(self) -> ContextStateSnapshot:
        generation, inventory, _events = self._read()
        return ContextStateSnapshot(
            generation,
            self._limit,
            inventory.used_portable_tokens,
            tuple(_snapshot(entry) for entry in inventory.entries),
        )

    @property
    def events(self) -> tuple[ContextStateEvent, ...]:
        return self._read()[2]

    def add(self, entry: ResidentSection) -> tuple[Eviction, ...]:
        selected = _decode_entry(cast(dict[str, JsonValue], asdict(entry)))

        def mutate(inventory: ContextInventory) -> tuple[Eviction, ...]:
            return inventory.add(selected)

        return self._mutate("add", selected.key, mutate)

    def access(self, key: str) -> ContextEntrySnapshot:
        def mutate(inventory: ContextInventory) -> ContextEntrySnapshot:
            try:
                return _snapshot(inventory.access(key))
            except KeyError as error:
                raise _missing() from error

        return self._mutate("access", key, mutate)

    def pin(self, key: str, value: bool = True) -> None:
        if not isinstance(value, bool):
            raise TypeError("pin value must be a boolean")

        def mutate(inventory: ContextInventory) -> None:
            try:
                inventory.pin(key, value)
            except KeyError as error:
                raise _missing() from error

        self._mutate("pin" if value else "unpin", key, mutate)

    def remove(self, key: str) -> Eviction:
        def mutate(inventory: ContextInventory) -> Eviction:
            try:
                return inventory.remove(key)
            except KeyError as error:
                raise _missing() from error

        return self._mutate("remove", key, mutate)

    def _read(self) -> tuple[int, ContextInventory, tuple[ContextStateEvent, ...]]:
        raw = self._state.get(self._scope, _KEY, namespace=_NAMESPACE)
        return _decode(raw, expected_limit=self._limit)

    def _mutate(
        self,
        action: str,
        key: str,
        operation: Callable[[ContextInventory], _Result],
    ) -> _Result:
        def update(raw: JsonValue | None) -> tuple[JsonValue, _Result]:
            generation, inventory, events = _decode(raw, expected_limit=self._limit)
            result = operation(inventory)
            next_generation = generation + 1
            evictions = (
                result
                if action == "add" and isinstance(result, tuple)
                else (result,) if action == "remove" and isinstance(result, Eviction) else ()
            )
            event = ContextStateEvent(
                next_generation,
                action,
                key,
                inventory.used_portable_tokens,
                cast(tuple[Eviction, ...], evictions),
            )
            retained = (*events, event)[-self._event_limit :]
            return _encode(next_generation, inventory, retained), result

        return self._state.transact_entry(
            self._scope, _KEY, update, namespace=_NAMESPACE
        )


def _encode(
    generation: int,
    inventory: ContextInventory,
    events: tuple[ContextStateEvent, ...],
) -> dict[str, JsonValue]:
    return {
        "entries": [cast(JsonValue, asdict(entry)) for entry in inventory.entries],
        "events": [_encode_event(event) for event in events],
        "generation": generation,
        "max_portable_tokens": inventory.max_portable_tokens,
        "schema_version": _SCHEMA_VERSION,
    }


def _decode(
    raw: JsonValue | None, *, expected_limit: int
) -> tuple[int, ContextInventory, tuple[ContextStateEvent, ...]]:
    if raw is None:
        return 0, ContextInventory(expected_limit), ()
    try:
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            raise TypeError
        generation = _natural(raw.get("generation"))
        stored_limit = _positive(raw.get("max_portable_tokens"))
        raw_entries = raw.get("entries")
        raw_events = raw.get("events")
        if stored_limit != expected_limit or not isinstance(raw_entries, list):
            raise TypeError
        if not isinstance(raw_events, list):
            raise TypeError
        entries = tuple(_decode_entry(item) for item in raw_entries)
        if len({entry.key for entry in entries}) != len(entries):
            raise TypeError
        inventory = ContextInventory(expected_limit)
        for entry in sorted(entries, key=lambda item: item.last_access):
            inventory.add(entry)
        events = tuple(_decode_event(item) for item in raw_events)
        return generation, inventory, events
    except (KeyError, TypeError, ValueError) as error:
        raise CapabilityHubError(
            code="scoped_context_state_invalid",
            category=ErrorCategory.INTERNAL,
            safe_message="The scoped context residency state is invalid.",
        ) from error


def _decode_entry(value: JsonValue) -> ResidentSection:
    if not isinstance(value, dict):
        raise TypeError
    key, revision, section = (value.get(name) for name in ("key", "revision", "section"))
    if any(not isinstance(item, str) or not item for item in (key, revision, section)):
        raise TypeError
    portable_tokens = _natural(value.get("portable_tokens"))
    reload_cost = _positive(value.get("reload_cost"))
    reuse_score = _positive(value.get("reuse_score"))
    last_access = _natural(value.get("last_access"))
    sensitive = value.get("sensitive")
    pinned = value.get("pinned")
    if not isinstance(sensitive, bool) or not isinstance(pinned, bool):
        raise TypeError
    return ResidentSection(
        cast(str, key),
        cast(str, revision),
        cast(str, section),
        portable_tokens,
        reload_cost,
        reuse_score,
        sensitive,
        pinned,
        last_access,
    )


def _decode_event(value: JsonValue) -> ContextStateEvent:
    if not isinstance(value, dict):
        raise TypeError
    sequence = _natural(value.get("sequence"))
    action = value.get("action")
    key = value.get("key")
    used = _natural(value.get("used_portable_tokens"))
    raw_evictions = value.get("evictions")
    if not isinstance(action, str) or not isinstance(key, str):
        raise TypeError
    if not isinstance(raw_evictions, list):
        raise TypeError
    evictions: list[Eviction] = []
    for raw in raw_evictions:
        if not isinstance(raw, dict):
            raise TypeError
        victim = raw.get("key")
        tokens = _natural(raw.get("portable_tokens"))
        reason = raw.get("reason")
        if not isinstance(victim, str) or not isinstance(reason, str):
            raise TypeError
        evictions.append(Eviction(victim, tokens, reason))
    return ContextStateEvent(sequence, action, key, used, tuple(evictions))


def _encode_event(event: ContextStateEvent) -> dict[str, JsonValue]:
    return {
        "action": event.action,
        "evictions": [
            {
                "key": eviction.key,
                "portable_tokens": eviction.portable_tokens,
                "reason": eviction.reason,
            }
            for eviction in event.evictions
        ],
        "key": event.key,
        "sequence": event.sequence,
        "used_portable_tokens": event.used_portable_tokens,
    }


def _snapshot(entry: ResidentSection) -> ContextEntrySnapshot:
    return ContextEntrySnapshot(**asdict(entry))


def _natural(value: JsonValue | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError
    return value


def _positive(value: JsonValue | None) -> int:
    selected = _natural(value)
    if selected == 0:
        raise TypeError
    return selected


def _missing() -> CapabilityHubError:
    return CapabilityHubError(
        code="context_entry_not_found",
        category=ErrorCategory.REFERENCE,
        safe_message="The scoped context entry does not exist.",
    )
