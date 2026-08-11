from __future__ import annotations

from dataclasses import replace

import pytest

from capabilityhub.degraded import (
    DecisionOutcome,
    DegradedModePolicy,
    Dependency,
    DependencyObservation,
    DependencyStatus,
    Operation,
    SafeFallback,
)

NOW = 10_000.0


def _observations(
    *,
    status: DependencyStatus = DependencyStatus.AVAILABLE,
    observed_at: float = NOW,
    ttl: float = 60.0,
) -> list[DependencyObservation]:
    return [
        DependencyObservation(dependency, status, observed_at, ttl) for dependency in Dependency
    ]


@pytest.mark.parametrize("operation", list(Operation))
def test_fresh_dependencies_allow_every_operation(operation: Operation) -> None:
    decision = DegradedModePolicy(clock=lambda: NOW).decide(operation, _observations())

    assert decision.outcome is DecisionOutcome.ALLOW
    assert decision.reasons == ("dependencies.fresh",)
    assert decision.fallbacks_used == ()


def test_available_observation_becomes_stale_after_ttl() -> None:
    observations = _observations()
    observations[1] = replace(observations[1], observed_at=NOW - 61)

    decision = DegradedModePolicy(clock=lambda: NOW).decide(Operation.SEARCH, observations)

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.reasons == (
        "dependency.index.stale",
        "fallback.index.missing",
    )


def test_stale_cached_index_is_degraded_only_with_explicit_safe_fallback() -> None:
    observations = _observations()
    observations[1] = replace(observations[1], observed_at=NOW - 30, ttl_seconds=10)
    fallback = SafeFallback(
        Operation.SEARCH,
        Dependency.INDEX,
        "cached_index",
        max_age_seconds=45,
    )

    decision = DegradedModePolicy(clock=lambda: NOW).decide(
        Operation.SEARCH, observations, safe_fallbacks=(fallback,)
    )

    assert decision.outcome is DecisionOutcome.DEGRADED
    assert decision.fallbacks_used == ("cached_index",)
    assert decision.reasons[-1] == "fallback.index.cached_index"


def test_expired_cache_fallback_denies() -> None:
    observations = _observations()
    observations[1] = replace(observations[1], observed_at=NOW - 100, ttl_seconds=10)
    fallback = SafeFallback(
        Operation.SEARCH,
        Dependency.INDEX,
        "cached_index",
        max_age_seconds=90,
    )

    decision = DegradedModePolicy(clock=lambda: NOW).decide(
        Operation.SEARCH, observations, safe_fallbacks=(fallback,)
    )

    assert decision.outcome is DecisionOutcome.DENY
    assert decision.fallbacks_used == ()


@pytest.mark.parametrize("dependency", [Dependency.POLICY, Dependency.PROVIDER])
def test_execute_unknown_policy_or_provider_denies_by_default(
    dependency: Dependency,
) -> None:
    observations = [
        observation for observation in _observations() if observation.dependency is not dependency
    ]

    decision = DegradedModePolicy(clock=lambda: NOW).decide(Operation.EXECUTE, observations)

    assert decision.outcome is DecisionOutcome.DENY
    assert f"dependency.{dependency.value}.unknown" in decision.reasons
    assert f"fallback.{dependency.value}.missing" in decision.reasons


def test_unknown_is_degraded_only_when_explicitly_listed_as_safe() -> None:
    observations = [
        observation
        for observation in _observations()
        if observation.dependency is not Dependency.PROVIDER
    ]
    explicit = SafeFallback(
        Operation.EXECUTE,
        Dependency.PROVIDER,
        "offline_provider",
        statuses=(DependencyStatus.UNKNOWN,),
    )

    decision = DegradedModePolicy(clock=lambda: NOW).decide(
        Operation.EXECUTE, observations, safe_fallbacks=(explicit,)
    )

    assert decision.outcome is DecisionOutcome.DEGRADED
    assert decision.fallbacks_used == ("offline_provider",)


def test_load_can_use_explicit_cached_provider_fallback() -> None:
    observations = _observations()
    observations[3] = replace(observations[3], status=DependencyStatus.UNAVAILABLE)

    decision = DegradedModePolicy(clock=lambda: NOW).decide(
        Operation.LOAD,
        observations,
        safe_fallbacks=(SafeFallback(Operation.LOAD, Dependency.PROVIDER, "verified_cache"),),
    )

    assert decision.outcome is DecisionOutcome.DEGRADED


def test_lifecycle_has_no_implicit_fallback() -> None:
    observations = _observations(status=DependencyStatus.AVAILABLE)
    observations[0] = replace(observations[0], status=DependencyStatus.UNAVAILABLE)

    decision = DegradedModePolicy(clock=lambda: NOW).decide(Operation.LIFECYCLE, observations)

    assert decision.outcome is DecisionOutcome.DENY


def test_irrelevant_stale_index_does_not_block_execute() -> None:
    observations = _observations()
    observations[1] = replace(observations[1], status=DependencyStatus.STALE)

    decision = DegradedModePolicy(clock=lambda: NOW).decide(Operation.EXECUTE, observations)

    assert decision.outcome is DecisionOutcome.ALLOW


def test_future_observation_is_unknown_and_fails_closed() -> None:
    observations = _observations()
    observations[0] = replace(observations[0], observed_at=NOW + 1)

    decision = DegradedModePolicy(clock=lambda: NOW).decide(Operation.SEARCH, observations)

    registry = next(
        item for item in decision.dependencies if item.dependency is Dependency.REGISTRY
    )
    assert registry.effective_status is DependencyStatus.UNKNOWN
    assert decision.outcome is DecisionOutcome.DENY


def test_decision_surface_contains_no_location_fields() -> None:
    decision = DegradedModePolicy(clock=lambda: NOW).decide(Operation.SEARCH, _observations())

    assert all(not hasattr(item, "endpoint") for item in decision.dependencies)
    assert all(not hasattr(item, "path") for item in decision.dependencies)


def test_duplicate_observations_and_fallbacks_are_rejected() -> None:
    duplicate = DependencyObservation(Dependency.REGISTRY, DependencyStatus.AVAILABLE, NOW, 10)
    with pytest.raises(ValueError, match="duplicate observation"):
        DegradedModePolicy(clock=lambda: NOW).decide(Operation.SEARCH, (duplicate, duplicate))
    observations = _observations()
    fallback = SafeFallback(Operation.SEARCH, Dependency.INDEX, "cache")
    with pytest.raises(ValueError, match="duplicate fallback"):
        DegradedModePolicy(clock=lambda: NOW).decide(
            Operation.SEARCH,
            observations,
            safe_fallbacks=(fallback, fallback),
        )
