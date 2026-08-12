from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.degraded import Dependency, DependencyStatus
from capabilityhub.dependency_observer import (
    LiveDependencyObserver,
    ObservationSource,
    policy_revision_source,
    provider_circuit_source,
)
from capabilityhub.resilience import CircuitSnapshot, CircuitState


def test_observer_resamples_live_policy_and_provider_state() -> None:
    state = {"revision": 3, "circuit": CircuitState.CLOSED}
    observer = LiveDependencyObserver(
        (
            policy_revision_source("policy-store", lambda: state["revision"]),
            provider_circuit_source(
                "provider-circuits",
                lambda: (CircuitSnapshot(state["circuit"], 0),),
            ),
        ),
        clock=lambda: 50,
    )

    first = {item.dependency: item for item in observer.observe()}
    state["revision"] = -1
    state["circuit"] = CircuitState.OPEN
    second = {item.dependency: item for item in observer.observe()}

    assert first[Dependency.POLICY].status is DependencyStatus.AVAILABLE
    assert first[Dependency.PROVIDER].status is DependencyStatus.AVAILABLE
    assert second[Dependency.POLICY].status is DependencyStatus.UNAVAILABLE
    assert second[Dependency.PROVIDER].status is DependencyStatus.UNAVAILABLE


def test_observer_fails_closed_and_is_concurrency_safe() -> None:
    def broken() -> DependencyStatus:
        raise RuntimeError("contains private endpoint details")

    observer = LiveDependencyObserver(
        (ObservationSource("policy", Dependency.POLICY, 1, broken),), clock=lambda: 10
    )
    with ThreadPoolExecutor(max_workers=16) as executor:
        results = tuple(executor.map(lambda _item: observer.observe(), range(64)))

    assert all(item[0].status is DependencyStatus.UNAVAILABLE for item in results)
    assert "private endpoint" not in repr(results)


def test_observer_rejects_unbounded_or_sensitive_source_ids() -> None:
    with pytest.raises(ValueError):
        ObservationSource(
            "raw https://private.example",
            Dependency.POLICY,
            1,
            lambda: DependencyStatus.AVAILABLE,
        )
    with pytest.raises(ValueError):
        ObservationSource("registry", Dependency.REGISTRY, 1, lambda: DependencyStatus.AVAILABLE)
