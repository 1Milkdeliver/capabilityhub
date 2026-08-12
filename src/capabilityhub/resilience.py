"""Conservative retry and circuit-breaker primitives for provider calls."""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from time import monotonic, sleep
from typing import Generic, TypeVar

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import ExecutionRequest, OperationSpec, SideEffect

T = TypeVar("T")


class FailureCertainty(StrEnum):
    """Whether the caller knows that repeating a failed call is safe."""

    NOT_APPLIED = "not_applied"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class RetryDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Explicit retry allowlist with exponential backoff."""

    max_attempts: int = 3
    initial_backoff_seconds: float = 0.1
    multiplier: float = 2.0
    max_backoff_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        values = (
            self.initial_backoff_seconds,
            self.multiplier,
            self.max_backoff_seconds,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("retry timing values must be finite")
        if self.initial_backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("retry backoff values must be non-negative")
        if self.multiplier < 1:
            raise ValueError("retry multiplier must be at least one")

    def decide(
        self,
        error: CapabilityHubError,
        operation: OperationSpec,
        request: ExecutionRequest,
        certainty: FailureCertainty,
    ) -> RetryDecision:
        if certainty is not FailureCertainty.NOT_APPLIED:
            return RetryDecision(False, "failure_uncertain")
        if error.category not in {
            ErrorCategory.PROVIDER,
            ErrorCategory.DEPENDENCY,
            ErrorCategory.TIMEOUT,
        }:
            return RetryDecision(False, "error_category_denied")
        if not error.retryable:
            return RetryDecision(False, "error_not_retryable")
        if operation.side_effect in {SideEffect.NONE, SideEffect.READ}:
            return RetryDecision(True, "safe_operation")
        if (
            operation.side_effect is SideEffect.REVERSIBLE_WRITE
            and request.idempotency_key is not None
            and bool(request.idempotency_key.strip())
        ):
            return RetryDecision(True, "idempotency_key_present")
        return RetryDecision(False, "operation_not_safe_to_repeat")

    def backoff_seconds(self, retry_number: int) -> float:
        """Return delay before a one-based retry number."""

        if retry_number < 1:
            raise ValueError("retry_number must be positive")
        try:
            delay = self.initial_backoff_seconds * self.multiplier ** (retry_number - 1)
        except OverflowError:
            return self.max_backoff_seconds
        return min(delay, self.max_backoff_seconds)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitPermit:
    provider_key: str
    generation: int
    half_open_probe: bool


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    state: CircuitState
    consecutive_failures: int


@dataclass(slots=True)
class _CircuitEntry:
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False
    in_flight: int = 0
    generation: int = 0


class CircuitBreaker:
    """Thread-safe, bounded per-provider circuit breaker."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        max_entries: int = 128,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if not math.isfinite(recovery_timeout_seconds) or recovery_timeout_seconds < 0:
            raise ValueError("recovery_timeout_seconds must be finite and non-negative")
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._failure_threshold = failure_threshold
        self._recovery_timeout_seconds = recovery_timeout_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _CircuitEntry] = OrderedDict()
        self._lock = RLock()

    def acquire(self, provider_key: str) -> CircuitPermit:
        if not provider_key:
            raise ValueError("provider_key must not be empty")
        with self._lock:
            entry = self._entry(provider_key)
            now = self._clock()
            if entry.state is CircuitState.OPEN:
                opened_at = entry.opened_at if entry.opened_at is not None else now
                if now - opened_at < self._recovery_timeout_seconds:
                    raise _circuit_error("provider_circuit_open", "Provider access is paused.")
                entry.state = CircuitState.HALF_OPEN
                entry.probe_in_flight = False
                entry.generation += 1
            if entry.state is CircuitState.HALF_OPEN:
                if entry.probe_in_flight:
                    raise _circuit_error("provider_circuit_open", "Provider access is paused.")
                entry.probe_in_flight = True
                return CircuitPermit(provider_key, entry.generation, True)
            entry.in_flight += 1
            return CircuitPermit(provider_key, entry.generation, False)

    def record_success(self, permit: CircuitPermit) -> None:
        with self._lock:
            entry = self._entries.get(permit.provider_key)
            if entry is None or entry.generation != permit.generation:
                return
            transitioned = entry.state is not CircuitState.CLOSED
            if not permit.half_open_probe:
                entry.in_flight = max(0, entry.in_flight - 1)
            entry.state = CircuitState.CLOSED
            entry.failures = 0
            entry.opened_at = None
            entry.probe_in_flight = False
            if transitioned:
                entry.generation += 1
            self._entries.move_to_end(permit.provider_key)

    def record_failure(self, permit: CircuitPermit, *, countable: bool = True) -> None:
        with self._lock:
            entry = self._entries.get(permit.provider_key)
            if entry is None or entry.generation != permit.generation:
                return
            if permit.half_open_probe:
                self._open(entry)
            else:
                entry.in_flight = max(0, entry.in_flight - 1)
                if countable:
                    entry.failures += 1
                    if entry.failures >= self._failure_threshold:
                        self._open(entry)
            self._entries.move_to_end(permit.provider_key)

    def snapshot(self, provider_key: str) -> CircuitSnapshot | None:
        with self._lock:
            entry = self._entries.get(provider_key)
            if entry is None:
                return None
            return CircuitSnapshot(entry.state, entry.failures)

    def _entry(self, provider_key: str) -> _CircuitEntry:
        entry = self._entries.get(provider_key)
        if entry is not None:
            self._entries.move_to_end(provider_key)
            return entry
        if len(self._entries) >= self._max_entries:
            evictable = next(
                (
                    key
                    for key, item in self._entries.items()
                    if item.state is CircuitState.CLOSED and item.in_flight == 0
                ),
                None,
            )
            if evictable is None:
                raise _circuit_error(
                    "provider_circuit_capacity",
                    "Provider protection capacity is temporarily unavailable.",
                )
            del self._entries[evictable]
        entry = _CircuitEntry()
        self._entries[provider_key] = entry
        return entry

    def _open(self, entry: _CircuitEntry) -> None:
        entry.state = CircuitState.OPEN
        entry.opened_at = self._clock()
        entry.probe_in_flight = False
        entry.in_flight = 0
        entry.generation += 1


class ResilientProviderExecutor(Generic[T]):
    """Apply deadline-aware retries and circuit protection to a provider callable."""

    def __init__(
        self,
        *,
        retry_policy: RetryPolicy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._retry_policy = retry_policy or RetryPolicy()
        self._circuit_breaker = circuit_breaker or CircuitBreaker(clock=clock)
        self._clock = clock
        self._sleeper = sleeper

    def snapshot(self, provider_key: str) -> CircuitSnapshot | None:
        """Return the bounded, non-sensitive circuit state for one provider."""

        return self._circuit_breaker.snapshot(provider_key)

    def execute(
        self,
        provider_key: str,
        call: Callable[[], T],
        *,
        operation: OperationSpec,
        request: ExecutionRequest,
        deadline_seconds: float,
        classify_certainty: Callable[[CapabilityHubError], FailureCertainty] | None = None,
    ) -> T:
        if not math.isfinite(deadline_seconds) or deadline_seconds < 0:
            raise ValueError("deadline_seconds must be finite and non-negative")
        deadline = self._clock() + deadline_seconds
        attempts = 0
        classifier = classify_certainty or (lambda _error: FailureCertainty.UNCERTAIN)
        while True:
            if self._clock() >= deadline:
                raise _deadline_error()
            permit = self._circuit_breaker.acquire(provider_key)
            attempts += 1
            try:
                result = call()
            except CapabilityHubError as error:
                countable = error.category in {
                    ErrorCategory.PROVIDER,
                    ErrorCategory.DEPENDENCY,
                    ErrorCategory.TIMEOUT,
                    ErrorCategory.INTERNAL,
                }
                self._circuit_breaker.record_failure(permit, countable=countable)
                try:
                    certainty = classifier(error)
                except Exception:
                    certainty = FailureCertainty.UNCERTAIN
                decision = self._retry_policy.decide(error, operation, request, certainty)
                if not decision.allowed:
                    raise
                if attempts >= self._retry_policy.max_attempts:
                    raise _exhausted_error(attempts) from error
                delay = self._retry_policy.backoff_seconds(attempts)
                if self._clock() + delay >= deadline:
                    raise _deadline_error() from error
                self._sleeper(delay)
            except Exception as error:
                self._circuit_breaker.record_failure(permit)
                raise CapabilityHubError(
                    code="provider_resilience_unhandled",
                    category=ErrorCategory.PROVIDER,
                    safe_message="The provider failed without a safe structured error.",
                ) from error
            else:
                self._circuit_breaker.record_success(permit)
                return result


_NOT_APPLIED_PROVIDER_ERRORS = frozenset(
    {
        "cli_start_failed",
        "provider_worker_start_failed",
    }
)


def classify_adapter_failure(error: CapabilityHubError) -> FailureCertainty:
    """Classify only failures proven to occur before adapter application.

    Timeouts, crashes, protocol failures, and remote transport failures remain
    uncertain because the adapter may have applied a write before failing.
    """

    if error.code in _NOT_APPLIED_PROVIDER_ERRORS:
        return FailureCertainty.NOT_APPLIED
    return FailureCertainty.UNCERTAIN


def _circuit_error(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.PROVIDER,
        safe_message=message,
    )


def _deadline_error() -> CapabilityHubError:
    return CapabilityHubError(
        code="provider_retry_deadline_exhausted",
        category=ErrorCategory.TIMEOUT,
        safe_message="The provider retry deadline was exhausted.",
    )


def _exhausted_error(attempts: int) -> CapabilityHubError:
    return CapabilityHubError(
        code="provider_retry_exhausted",
        category=ErrorCategory.PROVIDER,
        safe_message="The provider did not recover within the retry policy.",
        details={"attempts": attempts},
    )
