"""Thread-safe, non-blocking lifecycle draining for capability revisions."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from time import monotonic

from capabilityhub.errors import CapabilityHubError, ErrorCategory

_REASON = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")


class LifecycleState(StrEnum):
    ACCEPTING = "accepting"
    DRAINING = "draining"
    RETIRED = "retired"


class DrainOutcome(StrEnum):
    WAITING = "waiting"
    CANCEL_REQUESTED = "cancel_requested"
    BLOCKED = "blocked"
    RETIRED = "retired"


class ForceRetirePolicy(StrEnum):
    DENY = "deny"
    ALLOW_IN_FLIGHT = "allow_in_flight"


@dataclass(frozen=True, slots=True)
class AdmissionPin:
    pin_id: str
    coordinate: str
    revision: str
    cancellable: bool
    cancel_requested: bool = False


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    coordinate: str
    revision: str | None
    state: LifecycleState
    in_flight: int
    cancellable: int
    cancellation_requested: int


@dataclass(frozen=True, slots=True)
class DrainProgress:
    outcome: DrainOutcome
    snapshot: LifecycleSnapshot
    cancellation_requests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    sequence: int
    action: str
    target_digest: str
    in_flight: int
    reason: str | None = None
    count: int = 0


@dataclass(slots=True)
class _Revision:
    state: LifecycleState = LifecycleState.ACCEPTING


@dataclass(slots=True)
class _Coordinate:
    state: LifecycleState = LifecycleState.ACCEPTING


class LifecycleError(CapabilityHubError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(
            code=code,
            category=ErrorCategory.CONFLICT,
            safe_message=message,
            retryable=False,
        )


class DrainController:
    """Atomically admit executions and drain revisions without sleeping."""

    def __init__(
        self,
        *,
        max_coordinates: int = 1_000,
        max_revisions: int = 10_000,
        max_in_flight: int = 100_000,
        event_limit: int = 1_000,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        for value, label in (
            (max_coordinates, "max_coordinates"),
            (max_revisions, "max_revisions"),
            (max_in_flight, "max_in_flight"),
            (event_limit, "event_limit"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be positive")
        self._max_coordinates = max_coordinates
        self._max_revisions = max_revisions
        self._max_in_flight = max_in_flight
        self._clock = clock
        self._coordinates: dict[str, _Coordinate] = {}
        self._revisions: dict[tuple[str, str], _Revision] = {}
        self._pins: dict[str, AdmissionPin] = {}
        self._events: deque[LifecycleEvent] = deque(maxlen=event_limit)
        self._sequence = 0
        self._lock = RLock()

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def register(self, coordinate: str, revision: str) -> LifecycleSnapshot:
        selected_coordinate = _identifier(coordinate, "coordinate")
        selected_revision = _identifier(revision, "revision")
        key = (selected_coordinate, selected_revision)
        with self._lock:
            existing = self._revisions.get(key)
            if existing is not None:
                return self._revision_snapshot(key, existing)
            coordinate_state = self._coordinates.get(selected_coordinate)
            if (
                coordinate_state is not None
                and coordinate_state.state is not LifecycleState.ACCEPTING
            ):
                raise _error("lifecycle_coordinate_not_accepting")
            if len(self._revisions) >= self._max_revisions:
                raise _error("lifecycle_state_capacity")
            if coordinate_state is None:
                if len(self._coordinates) >= self._max_coordinates:
                    raise _error("lifecycle_state_capacity")
                coordinate_state = _Coordinate()
                self._coordinates[selected_coordinate] = coordinate_state
            revision_state = _Revision()
            self._revisions[key] = revision_state
            self._record("registered", key, 0)
            return self._revision_snapshot(key, revision_state)

    def admit(
        self,
        coordinate: str,
        revision: str,
        pin_id: str,
        *,
        cancellable: bool = False,
    ) -> AdmissionPin:
        key = (_identifier(coordinate, "coordinate"), _identifier(revision, "revision"))
        selected_pin = _identifier(pin_id, "pin_id")
        if not isinstance(cancellable, bool):
            raise TypeError("cancellable must be a boolean")
        with self._lock:
            revision_state = self._revisions.get(key)
            coordinate_state = self._coordinates.get(key[0])
            if revision_state is None or coordinate_state is None:
                raise _error("lifecycle_revision_not_found")
            if (
                coordinate_state.state is not LifecycleState.ACCEPTING
                or revision_state.state is not LifecycleState.ACCEPTING
            ):
                raise _error("lifecycle_not_accepting")
            existing = self._pins.get(selected_pin)
            if existing is not None:
                if (
                    existing.coordinate == key[0]
                    and existing.revision == key[1]
                    and existing.cancellable is cancellable
                ):
                    return existing
                raise _error("lifecycle_pin_conflict")
            if len(self._pins) >= self._max_in_flight:
                raise _error("lifecycle_pin_capacity")
            pin = AdmissionPin(selected_pin, key[0], key[1], cancellable)
            self._pins[selected_pin] = pin
            return pin

    def release(self, pin_id: str) -> bool:
        selected_pin = _identifier(pin_id, "pin_id")
        with self._lock:
            pin = self._pins.pop(selected_pin, None)
            if pin is None:
                return False
            key = (pin.coordinate, pin.revision)
            revision_state = self._revisions[key]
            if revision_state.state is LifecycleState.DRAINING and self._pin_count(key) == 0:
                self._retire_revision(key, reason="drained")
            self._maybe_retire_coordinate(pin.coordinate)
            return True

    def begin_drain(
        self,
        coordinate: str,
        revision: str | None = None,
        *,
        reason: str = "lifecycle_update",
    ) -> LifecycleSnapshot:
        selected_coordinate = _identifier(coordinate, "coordinate")
        selected_revision = _identifier(revision, "revision") if revision is not None else None
        selected_reason = _reason(reason)
        with self._lock:
            coordinate_state = self._coordinates.get(selected_coordinate)
            if coordinate_state is None:
                raise _error("lifecycle_coordinate_not_found")
            if selected_revision is None:
                if coordinate_state.state is LifecycleState.ACCEPTING:
                    coordinate_state.state = LifecycleState.DRAINING
                    self._record(
                        "coordinate_draining",
                        (selected_coordinate, None),
                        0,
                        selected_reason,
                    )
                for key, state in self._coordinate_revisions(selected_coordinate):
                    self._begin_revision_drain(key, state, selected_reason)
                self._maybe_retire_coordinate(selected_coordinate)
                return self._coordinate_snapshot(selected_coordinate, coordinate_state)
            key = (selected_coordinate, selected_revision)
            revision_state = self._revisions.get(key)
            if revision_state is None:
                raise _error("lifecycle_revision_not_found")
            self._begin_revision_drain(key, revision_state, selected_reason)
            return self._revision_snapshot(key, revision_state)

    def advance(
        self,
        coordinate: str,
        revision: str | None = None,
        *,
        deadline: float,
        now: float | None = None,
    ) -> DrainProgress:
        selected_coordinate = _identifier(coordinate, "coordinate")
        selected_revision = _identifier(revision, "revision") if revision is not None else None
        selected_deadline = _time(deadline, "deadline")
        current = _time(self._clock() if now is None else now, "now")
        with self._lock:
            snapshot = self._snapshot(selected_coordinate, selected_revision)
            if snapshot.state is LifecycleState.RETIRED:
                return DrainProgress(DrainOutcome.RETIRED, snapshot)
            if snapshot.state is not LifecycleState.DRAINING:
                raise _error("lifecycle_not_draining")
            if snapshot.in_flight == 0:
                self._retire_target(selected_coordinate, selected_revision, "drained")
                return DrainProgress(
                    DrainOutcome.RETIRED,
                    self._snapshot(selected_coordinate, selected_revision),
                )
            if current < selected_deadline:
                return DrainProgress(DrainOutcome.WAITING, snapshot)
            target_pins = self._target_pins(selected_coordinate, selected_revision)
            requests: list[str] = []
            for pin in target_pins:
                if pin.cancellable and not pin.cancel_requested:
                    replacement = AdmissionPin(
                        pin.pin_id,
                        pin.coordinate,
                        pin.revision,
                        pin.cancellable,
                        True,
                    )
                    self._pins[pin.pin_id] = replacement
                    requests.append(pin.pin_id)
            if requests:
                self._record(
                    "cancellation_requested",
                    (selected_coordinate, selected_revision),
                    snapshot.in_flight,
                    count=len(requests),
                )
                return DrainProgress(
                    DrainOutcome.CANCEL_REQUESTED,
                    self._snapshot(selected_coordinate, selected_revision),
                    tuple(requests),
                )
            return DrainProgress(DrainOutcome.BLOCKED, snapshot)

    def force_retire(
        self,
        coordinate: str,
        revision: str | None = None,
        *,
        policy: ForceRetirePolicy,
        reason: str,
    ) -> LifecycleSnapshot:
        selected_coordinate = _identifier(coordinate, "coordinate")
        selected_revision = _identifier(revision, "revision") if revision is not None else None
        selected_reason = _reason(reason)
        if policy is not ForceRetirePolicy.ALLOW_IN_FLIGHT:
            raise _error("lifecycle_force_retire_denied")
        with self._lock:
            snapshot = self._snapshot(selected_coordinate, selected_revision)
            if snapshot.state is not LifecycleState.DRAINING:
                raise _error("lifecycle_not_draining")
            self._retire_target(
                selected_coordinate,
                selected_revision,
                selected_reason,
                forced=True,
            )
            return self._snapshot(selected_coordinate, selected_revision)

    def snapshot(self, coordinate: str, revision: str | None = None) -> LifecycleSnapshot:
        selected_coordinate = _identifier(coordinate, "coordinate")
        selected_revision = _identifier(revision, "revision") if revision is not None else None
        with self._lock:
            return self._snapshot(selected_coordinate, selected_revision)

    def _snapshot(self, coordinate: str, revision: str | None) -> LifecycleSnapshot:
        coordinate_state = self._coordinates.get(coordinate)
        if coordinate_state is None:
            raise _error("lifecycle_coordinate_not_found")
        if revision is None:
            return self._coordinate_snapshot(coordinate, coordinate_state)
        key = (coordinate, revision)
        revision_state = self._revisions.get(key)
        if revision_state is None:
            raise _error("lifecycle_revision_not_found")
        return self._revision_snapshot(key, revision_state)

    def _coordinate_snapshot(self, coordinate: str, state: _Coordinate) -> LifecycleSnapshot:
        pins = self._target_pins(coordinate, None)
        return _make_snapshot(coordinate, None, state.state, pins)

    def _revision_snapshot(self, key: tuple[str, str], state: _Revision) -> LifecycleSnapshot:
        pins = self._target_pins(*key)
        return _make_snapshot(key[0], key[1], state.state, pins)

    def _begin_revision_drain(self, key: tuple[str, str], state: _Revision, reason: str) -> None:
        if state.state is LifecycleState.ACCEPTING:
            state.state = LifecycleState.DRAINING
            count = self._pin_count(key)
            self._record("revision_draining", key, count, reason)
            if count == 0:
                self._retire_revision(key, reason="drained")

    def _retire_target(
        self,
        coordinate: str,
        revision: str | None,
        reason: str,
        *,
        forced: bool = False,
    ) -> None:
        if revision is None:
            for key, state in self._coordinate_revisions(coordinate):
                if state.state is LifecycleState.DRAINING:
                    self._retire_revision(key, reason=reason, forced=forced)
            coordinate_state = self._coordinates[coordinate]
            coordinate_state.state = LifecycleState.RETIRED
            self._record(
                "coordinate_force_retired" if forced else "coordinate_retired",
                (coordinate, None),
                self._pin_count((coordinate, None)),
                reason,
            )
        else:
            self._retire_revision((coordinate, revision), reason=reason, forced=forced)
            self._maybe_retire_coordinate(coordinate)

    def _retire_revision(self, key: tuple[str, str], *, reason: str, forced: bool = False) -> None:
        state = self._revisions[key]
        if state.state is LifecycleState.RETIRED:
            return
        state.state = LifecycleState.RETIRED
        self._record(
            "revision_force_retired" if forced else "revision_retired",
            key,
            self._pin_count(key),
            reason,
        )

    def _maybe_retire_coordinate(self, coordinate: str) -> None:
        state = self._coordinates[coordinate]
        revisions = self._coordinate_revisions(coordinate)
        if state.state is LifecycleState.DRAINING and all(
            item.state is LifecycleState.RETIRED for _, item in revisions
        ):
            state.state = LifecycleState.RETIRED
            self._record(
                "coordinate_retired",
                (coordinate, None),
                self._pin_count((coordinate, None)),
                "drained",
            )

    def _coordinate_revisions(self, coordinate: str) -> list[tuple[tuple[str, str], _Revision]]:
        return [(key, state) for key, state in self._revisions.items() if key[0] == coordinate]

    def _target_pins(self, coordinate: str, revision: str | None) -> tuple[AdmissionPin, ...]:
        return tuple(
            pin
            for pin in self._pins.values()
            if pin.coordinate == coordinate and (revision is None or pin.revision == revision)
        )

    def _pin_count(self, key: tuple[str, str | None]) -> int:
        return len(self._target_pins(*key))

    def _record(
        self,
        action: str,
        key: tuple[str, str | None],
        in_flight: int,
        reason: str | None = None,
        *,
        count: int = 0,
    ) -> None:
        self._sequence += 1
        target = f"{key[0]}\0{key[1] or '*'}".encode()
        self._events.append(
            LifecycleEvent(
                self._sequence,
                action,
                hashlib.sha256(target).hexdigest()[:16],
                in_flight,
                reason,
                count,
            )
        )


def _make_snapshot(
    coordinate: str,
    revision: str | None,
    state: LifecycleState,
    pins: tuple[AdmissionPin, ...],
) -> LifecycleSnapshot:
    return LifecycleSnapshot(
        coordinate,
        revision,
        state,
        len(pins),
        sum(pin.cancellable for pin in pins),
        sum(pin.cancel_requested for pin in pins),
    )


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    selected = unicodedata.normalize("NFC", value).strip()
    if (
        not selected
        or len(selected) > 512
        or any(unicodedata.category(character).startswith("C") for character in selected)
    ):
        raise ValueError(f"{label} is invalid")
    return selected


def _reason(value: object) -> str:
    if not isinstance(value, str) or _REASON.fullmatch(value) is None:
        raise ValueError("reason must be a stable lowercase reason code")
    return value


def _time(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a non-negative finite number")
    selected = float(value)
    if not math.isfinite(selected) or selected < 0:
        raise ValueError(f"{label} must be a non-negative finite number")
    return selected


def _error(code: str) -> LifecycleError:
    messages = {
        "lifecycle_coordinate_not_accepting": "The capability is no longer accepting revisions.",
        "lifecycle_coordinate_not_found": "The capability lifecycle was not found.",
        "lifecycle_force_retire_denied": "Forced retirement requires an explicit policy.",
        "lifecycle_not_accepting": "The capability revision is not accepting new executions.",
        "lifecycle_not_draining": "The capability target is not draining.",
        "lifecycle_pin_capacity": "The in-flight execution limit was reached.",
        "lifecycle_pin_conflict": "The execution pin identifier is already in use.",
        "lifecycle_revision_not_found": "The capability revision lifecycle was not found.",
        "lifecycle_state_capacity": "The lifecycle state limit was reached.",
    }
    return LifecycleError(code, messages[code])
