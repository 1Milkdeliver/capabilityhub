from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import ExecutionRequest, OperationSpec, OperationType, SideEffect
from capabilityhub.resilience import (
    CircuitBreaker,
    CircuitState,
    FailureCertainty,
    ResilientProviderExecutor,
    RetryPolicy,
)


@dataclass
class _Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _operation(side_effect: SideEffect = SideEffect.READ) -> OperationSpec:
    return OperationSpec("fetch", OperationType.EXECUTE, side_effect=side_effect)


def _request(idempotency_key: str | None = None) -> ExecutionRequest:
    return ExecutionRequest("execution", "fetch", {}, "task", idempotency_key=idempotency_key)


def _failure(
    category: ErrorCategory = ErrorCategory.PROVIDER,
    *,
    retryable: bool = True,
    secret: str = "",
) -> CapabilityHubError:
    return CapabilityHubError(
        code="upstream_failed",
        category=category,
        safe_message="A safe upstream error." + secret,
        retryable=retryable,
    )


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.INPUT,
        ErrorCategory.REFERENCE,
        ErrorCategory.POLICY,
        ErrorCategory.APPROVAL,
        ErrorCategory.BUDGET,
        ErrorCategory.CONFLICT,
        ErrorCategory.CANCELLED,
        ErrorCategory.INTERNAL,
    ],
)
def test_retry_policy_denies_non_transient_error_categories(category: ErrorCategory) -> None:
    decision = RetryPolicy().decide(
        _failure(category), _operation(), _request(), FailureCertainty.NOT_APPLIED
    )

    assert decision.allowed is False
    assert decision.reason == "error_category_denied"


def test_retry_policy_requires_retryable_typed_error_and_certain_failure() -> None:
    policy = RetryPolicy()

    not_retryable = policy.decide(
        _failure(retryable=False), _operation(), _request(), FailureCertainty.NOT_APPLIED
    )
    uncertain = policy.decide(_failure(), _operation(), _request(), FailureCertainty.UNCERTAIN)

    assert not_retryable.reason == "error_not_retryable"
    assert uncertain.reason == "failure_uncertain"


def test_retry_policy_allows_reads_and_explicitly_idempotent_reversible_writes() -> None:
    policy = RetryPolicy()

    read = policy.decide(
        _failure(), _operation(SideEffect.READ), _request(), FailureCertainty.NOT_APPLIED
    )
    keyed_write = policy.decide(
        _failure(),
        _operation(SideEffect.REVERSIBLE_WRITE),
        _request("idempotency-1"),
        FailureCertainty.NOT_APPLIED,
    )

    assert read.allowed is True
    assert keyed_write.allowed is True


@pytest.mark.parametrize(
    ("side_effect", "idempotency_key"),
    [
        (SideEffect.REVERSIBLE_WRITE, None),
        (SideEffect.REVERSIBLE_WRITE, "  "),
        (SideEffect.IRREVERSIBLE, "idempotency-1"),
    ],
)
def test_retry_policy_denies_unkeyed_or_irreversible_side_effects(
    side_effect: SideEffect, idempotency_key: str | None
) -> None:
    decision = RetryPolicy().decide(
        _failure(),
        _operation(side_effect),
        _request(idempotency_key),
        FailureCertainty.NOT_APPLIED,
    )

    assert decision.allowed is False
    assert decision.reason == "operation_not_safe_to_repeat"


def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(initial_backoff_seconds=0.5, multiplier=3, max_backoff_seconds=2)

    assert [policy.backoff_seconds(index) for index in range(1, 5)] == [0.5, 1.5, 2, 2]


def test_circuit_opens_and_allows_only_one_half_open_probe() -> None:
    clock = _Clock()
    circuit = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=5, clock=clock)
    circuit.record_failure(circuit.acquire("provider"))
    circuit.record_failure(circuit.acquire("provider"))

    assert circuit.snapshot("provider").state is CircuitState.OPEN  # type: ignore[union-attr]
    with pytest.raises(CapabilityHubError, match="Provider access is paused") as caught:
        circuit.acquire("provider")
    assert caught.value.code == "provider_circuit_open"

    clock.now = 5
    probe = circuit.acquire("provider")
    assert probe.half_open_probe is True
    with pytest.raises(CapabilityHubError) as concurrent:
        circuit.acquire("provider")
    assert concurrent.value.code == "provider_circuit_open"

    circuit.record_success(probe)
    assert circuit.snapshot("provider").state is CircuitState.CLOSED  # type: ignore[union-attr]


def test_failed_half_open_probe_reopens_circuit() -> None:
    clock = _Clock()
    circuit = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=1, clock=clock)
    circuit.record_failure(circuit.acquire("provider"))
    clock.now = 1

    circuit.record_failure(circuit.acquire("provider"), countable=False)

    assert circuit.snapshot("provider").state is CircuitState.OPEN  # type: ignore[union-attr]


def test_circuit_state_is_bounded_and_evicts_only_closed_lru_entries() -> None:
    circuit = CircuitBreaker(failure_threshold=1, max_entries=2)
    first = circuit.acquire("first")
    circuit.record_success(first)
    circuit.record_success(circuit.acquire("second"))
    circuit.record_success(circuit.acquire("first"))  # first is now most recently used
    circuit.acquire("third")

    assert circuit.snapshot("second") is None
    assert circuit.snapshot("first") is not None
    assert circuit.snapshot("third") is not None

    protected = CircuitBreaker(failure_threshold=1, max_entries=1)
    protected.record_failure(protected.acquire("open"))
    with pytest.raises(CapabilityHubError) as caught:
        protected.acquire("SECRET-PROVIDER")
    assert caught.value.code == "provider_circuit_capacity"
    assert "SECRET-PROVIDER" not in str(caught.value.as_dict())


def test_circuit_never_evicts_an_in_flight_call() -> None:
    circuit = CircuitBreaker(max_entries=1)
    permit = circuit.acquire("active")

    with pytest.raises(CapabilityHubError) as caught:
        circuit.acquire("other")
    assert caught.value.code == "provider_circuit_capacity"

    circuit.record_success(permit)
    circuit.acquire("other")


def test_circuit_updates_are_thread_safe() -> None:
    circuit = CircuitBreaker(failure_threshold=10_000)

    def fail_once(_: int) -> None:
        circuit.record_failure(circuit.acquire("provider"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fail_once, range(200)))

    snapshot = circuit.snapshot("provider")
    assert snapshot is not None
    assert snapshot.consecutive_failures == 200


def test_executor_retries_with_injected_backoff_then_returns() -> None:
    clock = _Clock()
    sleeps: list[float] = []
    attempts = 0

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock.sleep(seconds)

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _failure(ErrorCategory.TIMEOUT)
        return "ok"

    executor = ResilientProviderExecutor[str](
        retry_policy=RetryPolicy(initial_backoff_seconds=1, max_backoff_seconds=10),
        clock=clock,
        sleeper=sleeper,
    )
    result = executor.execute(
        "provider",
        call,
        operation=_operation(),
        request=_request(),
        deadline_seconds=10,
        classify_certainty=lambda _error: FailureCertainty.NOT_APPLIED,
    )

    assert result == "ok"
    assert attempts == 3
    assert sleeps == [1, 2]


def test_executor_default_uncertain_classification_never_retries() -> None:
    attempts = 0

    def call() -> str:
        nonlocal attempts
        attempts += 1
        raise _failure()

    with pytest.raises(CapabilityHubError) as caught:
        ResilientProviderExecutor[str]().execute(
            "provider",
            call,
            operation=_operation(),
            request=_request(),
            deadline_seconds=10,
        )

    assert caught.value.code == "upstream_failed"
    assert attempts == 1


def test_executor_fails_closed_if_certainty_classifier_fails() -> None:
    attempts = 0

    def call() -> str:
        nonlocal attempts
        attempts += 1
        raise _failure()

    def broken_classifier(_error: CapabilityHubError) -> FailureCertainty:
        raise RuntimeError("SECRET-CANARY")

    with pytest.raises(CapabilityHubError) as caught:
        ResilientProviderExecutor[str]().execute(
            "provider",
            call,
            operation=_operation(),
            request=_request(),
            deadline_seconds=10,
            classify_certainty=broken_classifier,
        )

    assert caught.value.code == "upstream_failed"
    assert attempts == 1


def test_executor_honors_deadline_before_sleeping() -> None:
    clock = _Clock()
    slept: list[float] = []
    executor = ResilientProviderExecutor[str](
        retry_policy=RetryPolicy(initial_backoff_seconds=2),
        clock=clock,
        sleeper=slept.append,
    )

    with pytest.raises(CapabilityHubError) as caught:
        executor.execute(
            "provider",
            lambda: (_ for _ in ()).throw(_failure()),
            operation=_operation(),
            request=_request(),
            deadline_seconds=1,
            classify_certainty=lambda _error: FailureCertainty.NOT_APPLIED,
        )

    assert caught.value.code == "provider_retry_deadline_exhausted"
    assert slept == []


def test_executor_exhaustion_and_unknown_errors_are_stable_and_redacted() -> None:
    clock = _Clock()
    executor = ResilientProviderExecutor[str](retry_policy=RetryPolicy(max_attempts=1), clock=clock)

    with pytest.raises(CapabilityHubError) as exhausted:
        executor.execute(
            "provider",
            lambda: (_ for _ in ()).throw(_failure(secret=" SECRET-CANARY")),
            operation=_operation(),
            request=_request(),
            deadline_seconds=1,
            classify_certainty=lambda _error: FailureCertainty.NOT_APPLIED,
        )
    assert exhausted.value.code == "provider_retry_exhausted"
    assert "SECRET-CANARY" not in str(exhausted.value.as_dict())

    with pytest.raises(CapabilityHubError) as unknown:
        ResilientProviderExecutor[str]().execute(
            "provider",
            lambda: (_ for _ in ()).throw(RuntimeError("SECRET-CANARY")),
            operation=_operation(),
            request=_request(),
            deadline_seconds=1,
        )
    assert unknown.value.code == "provider_resilience_unhandled"
    assert unknown.value.retryable is False
    assert "SECRET-CANARY" not in str(unknown.value.as_dict())
