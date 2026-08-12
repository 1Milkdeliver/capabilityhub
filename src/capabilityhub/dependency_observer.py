"""Live, privacy-bounded policy and provider dependency observations."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import RLock
from time import time

from capabilityhub.degraded import Dependency, DependencyObservation, DependencyStatus
from capabilityhub.resilience import CircuitSnapshot, CircuitState

_MAX_SOURCES = 64
_SEVERITY = {
    DependencyStatus.AVAILABLE: 0,
    DependencyStatus.UNKNOWN: 1,
    DependencyStatus.STALE: 2,
    DependencyStatus.UNAVAILABLE: 3,
}


@dataclass(frozen=True, slots=True)
class ObservationSource:
    """One bounded health signal. ``source_id`` must not contain a raw endpoint."""

    source_id: str
    dependency: Dependency
    ttl_seconds: float
    probe: Callable[[], DependencyStatus]

    def __post_init__(self) -> None:
        if not self.source_id or len(self.source_id) > 64 or any(
            char.isspace() for char in self.source_id
        ):
            raise ValueError("source_id must be a compact non-sensitive identifier")
        if self.dependency not in {Dependency.POLICY, Dependency.PROVIDER}:
            raise ValueError("live sources are limited to policy and provider")
        if not math.isfinite(self.ttl_seconds) or not 0 < self.ttl_seconds <= 300:
            raise ValueError("ttl_seconds must be between zero and 300")


class LiveDependencyObserver:
    """Sample all sources at query time and aggregate conservatively."""

    def __init__(
        self,
        sources: Iterable[ObservationSource],
        *,
        clock: Callable[[], float] = time,
    ) -> None:
        selected = tuple(sorted(sources, key=lambda item: (item.dependency.value, item.source_id)))
        if not selected or len(selected) > _MAX_SOURCES:
            raise ValueError("between one and 64 observation sources are required")
        if len({(item.dependency, item.source_id) for item in selected}) != len(selected):
            raise ValueError("observation source identifiers must be unique per dependency")
        self._sources = selected
        self._clock = clock
        self._lock = RLock()

    def observe(self) -> tuple[DependencyObservation, ...]:
        """Return a fresh snapshot; a broken source is unavailable, never healthy."""

        with self._lock:
            observed_at = self._clock()
            if not math.isfinite(observed_at) or observed_at < 0:
                raise ValueError("clock must return a finite non-negative timestamp")
            grouped: dict[Dependency, list[tuple[DependencyStatus, float]]] = {}
            for source in self._sources:
                try:
                    status = source.probe()
                    if not isinstance(status, DependencyStatus):
                        status = DependencyStatus.UNKNOWN
                except Exception:
                    status = DependencyStatus.UNAVAILABLE
                grouped.setdefault(source.dependency, []).append((status, source.ttl_seconds))
            return tuple(
                DependencyObservation(
                    dependency=dependency,
                    status=max(items, key=lambda item: _SEVERITY[item[0]])[0],
                    observed_at=observed_at,
                    ttl_seconds=min(item[1] for item in items),
                )
                for dependency, items in sorted(grouped.items(), key=lambda item: item[0].value)
            )


def policy_revision_source(
    source_id: str,
    revision: Callable[[], int | None],
    *,
    ttl_seconds: float = 5,
) -> ObservationSource:
    """Treat a readable, non-negative policy revision as current evidence."""

    def probe() -> DependencyStatus:
        value = revision()
        if isinstance(value, bool) or not isinstance(value, int):
            return DependencyStatus.UNKNOWN
        return DependencyStatus.AVAILABLE if value >= 0 else DependencyStatus.UNAVAILABLE

    return ObservationSource(source_id, Dependency.POLICY, ttl_seconds, probe)


def provider_circuit_source(
    source_id: str,
    snapshots: Callable[[], Iterable[CircuitSnapshot | None]],
    *,
    ttl_seconds: float = 2,
) -> ObservationSource:
    """Project live circuit states without exposing provider names or locations."""

    def probe() -> DependencyStatus:
        selected = tuple(snapshots())
        if not selected or any(item is None for item in selected):
            return DependencyStatus.UNKNOWN
        states = tuple(item.state for item in selected if item is not None)
        if any(state is CircuitState.OPEN for state in states):
            return DependencyStatus.UNAVAILABLE
        if any(state is CircuitState.HALF_OPEN for state in states):
            return DependencyStatus.STALE
        return DependencyStatus.AVAILABLE

    return ObservationSource(source_id, Dependency.PROVIDER, ttl_seconds, probe)
