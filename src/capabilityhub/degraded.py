"""Deterministic dependency freshness and safe-degradation decisions."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum


class Dependency(StrEnum):
    REGISTRY = "registry"
    INDEX = "index"
    POLICY = "policy"
    PROVIDER = "provider"


class DependencyStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    UNKNOWN = "unknown"


class Operation(StrEnum):
    SEARCH = "search"
    LOAD = "load"
    EXECUTE = "execute"
    LIFECYCLE = "lifecycle"


class DecisionOutcome(StrEnum):
    ALLOW = "allow"
    DEGRADED = "degraded"
    DENY = "deny"


_REQUIRED: dict[Operation, tuple[Dependency, ...]] = {
    Operation.SEARCH: (Dependency.REGISTRY, Dependency.INDEX),
    Operation.LOAD: (Dependency.REGISTRY, Dependency.POLICY, Dependency.PROVIDER),
    Operation.EXECUTE: (Dependency.REGISTRY, Dependency.POLICY, Dependency.PROVIDER),
    Operation.LIFECYCLE: (Dependency.REGISTRY, Dependency.POLICY, Dependency.PROVIDER),
}


@dataclass(frozen=True, slots=True)
class DependencyObservation:
    dependency: Dependency
    status: DependencyStatus
    observed_at: float
    ttl_seconds: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.observed_at) or self.observed_at < 0:
            raise ValueError("observed_at must be a finite non-negative timestamp")
        if not math.isfinite(self.ttl_seconds) or self.ttl_seconds < 0:
            raise ValueError("ttl_seconds must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class DependencyAssessment:
    dependency: Dependency
    declared_status: DependencyStatus
    effective_status: DependencyStatus
    observed_at: float | None
    ttl_seconds: float | None
    age_seconds: float | None


@dataclass(frozen=True, slots=True)
class SafeFallback:
    """An explicit, reviewed fallback; absence always means fail closed."""

    operation: Operation
    dependency: Dependency
    name: str
    statuses: tuple[DependencyStatus, ...] = (
        DependencyStatus.UNAVAILABLE,
        DependencyStatus.STALE,
    )
    max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 64 or any(char.isspace() for char in self.name):
            raise ValueError("fallback name must be a non-empty identifier")
        if not self.statuses or len(set(self.statuses)) != len(self.statuses):
            raise ValueError("fallback statuses must be non-empty and unique")
        if DependencyStatus.AVAILABLE in self.statuses:
            raise ValueError("available dependencies do not need a fallback")
        if self.max_age_seconds is not None and (
            not math.isfinite(self.max_age_seconds) or self.max_age_seconds < 0
        ):
            raise ValueError("max_age_seconds must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class DegradedDecision:
    operation: Operation
    outcome: DecisionOutcome
    reasons: tuple[str, ...]
    dependencies: tuple[DependencyAssessment, ...]
    fallbacks_used: tuple[str, ...]


class DegradedModePolicy:
    """Evaluate dependency health without probing or revealing dependency locations."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock

    def decide(
        self,
        operation: Operation,
        observations: Iterable[DependencyObservation],
        *,
        safe_fallbacks: Iterable[SafeFallback] = (),
    ) -> DegradedDecision:
        now = self._clock()
        if not math.isfinite(now) or now < 0:
            raise ValueError("clock must return a finite non-negative timestamp")
        observed = _observation_map(observations)
        assessed = tuple(
            _assess(dependency, observed.get(dependency), now) for dependency in Dependency
        )
        by_dependency = {item.dependency: item for item in assessed}
        fallbacks = _fallback_map(safe_fallbacks, operation)
        reasons: list[str] = []
        used: list[str] = []
        denied = False
        degraded = False
        for dependency in _REQUIRED[operation]:
            assessment = by_dependency[dependency]
            if assessment.effective_status is DependencyStatus.AVAILABLE:
                continue
            reasons.append(f"dependency.{dependency.value}.{assessment.effective_status.value}")
            fallback = fallbacks.get(dependency)
            if fallback is not None and _fallback_applies(fallback, assessment):
                degraded = True
                used.append(fallback.name)
                reasons.append(f"fallback.{dependency.value}.{fallback.name}")
            else:
                denied = True
                reasons.append(f"fallback.{dependency.value}.missing")
        outcome = (
            DecisionOutcome.DENY
            if denied
            else DecisionOutcome.DEGRADED
            if degraded
            else DecisionOutcome.ALLOW
        )
        return DegradedDecision(
            operation=operation,
            outcome=outcome,
            reasons=tuple(reasons) if reasons else ("dependencies.fresh",),
            dependencies=assessed,
            fallbacks_used=tuple(used),
        )


def _observation_map(
    observations: Iterable[DependencyObservation],
) -> dict[Dependency, DependencyObservation]:
    result: dict[Dependency, DependencyObservation] = {}
    for observation in observations:
        if observation.dependency in result:
            raise ValueError(f"duplicate observation for {observation.dependency.value}")
        result[observation.dependency] = observation
    return result


def _fallback_map(
    fallbacks: Iterable[SafeFallback], operation: Operation
) -> dict[Dependency, SafeFallback]:
    result: dict[Dependency, SafeFallback] = {}
    for fallback in fallbacks:
        if fallback.operation is not operation:
            continue
        if fallback.dependency in result:
            raise ValueError(f"duplicate fallback for {fallback.dependency.value}")
        result[fallback.dependency] = fallback
    return result


def _assess(
    dependency: Dependency,
    observation: DependencyObservation | None,
    now: float,
) -> DependencyAssessment:
    if observation is None:
        return DependencyAssessment(
            dependency=dependency,
            declared_status=DependencyStatus.UNKNOWN,
            effective_status=DependencyStatus.UNKNOWN,
            observed_at=None,
            ttl_seconds=None,
            age_seconds=None,
        )
    age = now - observation.observed_at
    if age < 0:
        effective = DependencyStatus.UNKNOWN
    elif observation.status is DependencyStatus.AVAILABLE and age > observation.ttl_seconds:
        effective = DependencyStatus.STALE
    else:
        effective = observation.status
    return DependencyAssessment(
        dependency=dependency,
        declared_status=observation.status,
        effective_status=effective,
        observed_at=observation.observed_at,
        ttl_seconds=observation.ttl_seconds,
        age_seconds=max(age, 0.0),
    )


def _fallback_applies(fallback: SafeFallback, assessment: DependencyAssessment) -> bool:
    if assessment.effective_status not in fallback.statuses:
        return False
    if fallback.max_age_seconds is None:
        return True
    return assessment.age_seconds is not None and assessment.age_seconds <= fallback.max_age_seconds
