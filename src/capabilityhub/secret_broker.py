"""In-memory scoped secret handles for trusted local consumers.

This broker resolves environment aliases only at consumption time. It is not an OS
keychain and intentionally provides no plaintext lookup API or persistent storage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType

from .errors import CapabilityHubError, ErrorCategory

_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class SecretBrokerError(CapabilityHubError):
    """A stable rejection that never includes aliases, values, or raw handles."""

    def __init__(self, code: str) -> None:
        super().__init__(
            code=code,
            category=ErrorCategory.POLICY,
            safe_message="Secret handle is unavailable for this operation.",
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class SecretScope:
    tenant: str
    principal: str
    session: str
    task: str
    provider: str
    operation: str
    policy_revision: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.tenant,
                self.principal,
                self.session,
                self.task,
                self.provider,
                self.operation,
                self.policy_revision,
            )
        ):
            raise ValueError("all secret scope bindings must be non-empty strings")


@dataclass(frozen=True, slots=True)
class SecretHandle:
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("secret handle token must be non-empty")


@dataclass(frozen=True, slots=True)
class SecretConsumerContext:
    scope: SecretScope
    handle_digest: str
    uses_remaining: int


@dataclass(frozen=True, slots=True)
class SecretUseReceipt:
    handle_digest: str
    scope_digest: str
    uses_remaining: int
    consumed_at: int


@dataclass(frozen=True, slots=True)
class SecretAuditEvent:
    event: str
    outcome: str
    handle_digest: str
    scope_digest: str
    timestamp: int
    error_code: str | None = None


SecretConsumer = Callable[[str, SecretConsumerContext], None]
EnvironmentResolver = Callable[[str], str | None]
AuditSink = Callable[[SecretAuditEvent], None]


@dataclass(slots=True)
class _Lease:
    alias: str = field(repr=False)
    scope: SecretScope
    expires_at: int
    uses_remaining: int


class ScopedSecretBroker:
    """Issue and atomically consume short-lived handles without returning secrets."""

    MAX_TTL_SECONDS = 3_600
    MAX_USES = 32

    def __init__(
        self,
        trusted_consumers: Mapping[str, SecretConsumer],
        *,
        environment: EnvironmentResolver = os.environ.get,
        clock: Callable[[], float] = time.time,
        audit_sink: AuditSink | None = None,
    ) -> None:
        consumers = dict(trusted_consumers)
        if not consumers or any(
            not name or not callable(value) for name, value in consumers.items()
        ):
            raise ValueError("trusted consumers must map provider names to callbacks")
        self._consumers = MappingProxyType(consumers)
        self._environment = environment
        self._clock = clock
        self._audit_sink = audit_sink
        self._leases: dict[str, _Lease] = {}
        self._lock = RLock()

    def issue(
        self,
        alias: str,
        *,
        scope: SecretScope,
        ttl_seconds: int,
        max_uses: int = 1,
        now: int | None = None,
    ) -> SecretHandle:
        """Issue an opaque bearer handle for one validated environment alias."""

        if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
            raise ValueError("secret alias must be a valid environment variable name")
        if scope.provider not in self._consumers:
            raise SecretBrokerError("untrusted_secret_consumer")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= self.MAX_TTL_SECONDS
        ):
            raise ValueError(f"ttl_seconds must be from 1 to {self.MAX_TTL_SECONDS}")
        if (
            isinstance(max_uses, bool)
            or not isinstance(max_uses, int)
            or not 1 <= max_uses <= self.MAX_USES
        ):
            raise ValueError(f"max_uses must be from 1 to {self.MAX_USES}")
        timestamp = self._timestamp(now)
        with self._lock:
            token = secrets.token_urlsafe(32)
            while token in self._leases:
                token = secrets.token_urlsafe(32)
            self._leases[token] = _Lease(
                alias=alias,
                scope=scope,
                expires_at=timestamp + ttl_seconds,
                uses_remaining=max_uses,
            )
        handle = SecretHandle(token)
        self._emit("issue", "success", handle, scope, timestamp)
        return handle

    def consume(
        self,
        handle: SecretHandle,
        *,
        scope: SecretScope,
        now: int | None = None,
    ) -> SecretUseReceipt:
        """Atomically spend one use, then resolve only inside the trusted callback."""

        if not isinstance(handle, SecretHandle):
            raise SecretBrokerError("invalid_secret_handle")
        timestamp = self._timestamp(now)
        try:
            alias, consumer, remaining = self._admit(handle, scope, timestamp)
            try:
                value = self._environment(alias)
            except Exception as error:
                raise SecretBrokerError("secret_alias_unavailable") from error
            if not isinstance(value, str) or not value:
                raise SecretBrokerError("secret_alias_unavailable")
            context = SecretConsumerContext(
                scope=scope,
                handle_digest=_digest(handle.token),
                uses_remaining=remaining,
            )
            try:
                consumer(value, context)
            except Exception as error:
                raise SecretBrokerError("trusted_secret_consumer_failed") from error
        except SecretBrokerError as error:
            self._emit("consume", "failure", handle, scope, timestamp, error.code)
            raise
        receipt = SecretUseReceipt(
            handle_digest=_digest(handle.token),
            scope_digest=_scope_digest(scope),
            uses_remaining=remaining,
            consumed_at=timestamp,
        )
        self._emit("consume", "success", handle, scope, timestamp)
        return receipt

    def revoke(self, handle: SecretHandle, *, now: int | None = None) -> bool:
        if not isinstance(handle, SecretHandle):
            raise SecretBrokerError("invalid_secret_handle")
        timestamp = self._timestamp(now)
        with self._lock:
            removed = self._leases.pop(handle.token, None)
        self._emit(
            "revoke",
            "success" if removed is not None else "not_found",
            handle,
            removed.scope if removed is not None else None,
            timestamp,
        )
        return removed is not None

    def active_count(self) -> int:
        """Expose aggregate state only; aliases and bindings remain private."""

        with self._lock:
            return len(self._leases)

    def _admit(
        self,
        handle: SecretHandle,
        scope: SecretScope,
        timestamp: int,
    ) -> tuple[str, SecretConsumer, int]:
        with self._lock:
            lease = self._leases.get(handle.token)
            if lease is None:
                raise SecretBrokerError("secret_handle_consumed")
            if timestamp >= lease.expires_at:
                self._leases.pop(handle.token, None)
                raise SecretBrokerError("secret_handle_expired")
            if lease.scope != scope:
                raise SecretBrokerError("secret_scope_mismatch")
            consumer = self._consumers.get(scope.provider)
            if consumer is None:
                raise SecretBrokerError("untrusted_secret_consumer")
            lease.uses_remaining -= 1
            remaining = lease.uses_remaining
            if remaining == 0:
                self._leases.pop(handle.token, None)
            return lease.alias, consumer, remaining

    def _emit(
        self,
        event: str,
        outcome: str,
        handle: SecretHandle,
        scope: SecretScope | None,
        timestamp: int,
        error_code: str | None = None,
    ) -> None:
        if self._audit_sink is None:
            return
        audit = SecretAuditEvent(
            event=event,
            outcome=outcome,
            handle_digest=_digest(handle.token),
            scope_digest=_scope_digest(scope) if scope is not None else "unavailable",
            timestamp=timestamp,
            error_code=error_code,
        )
        try:
            self._audit_sink(audit)
        except Exception:
            # Audit observers cannot restore a spent bearer handle or expose secrets.
            return

    def _timestamp(self, now: int | None) -> int:
        value = int(self._clock()) if now is None else now
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("now must be an integer timestamp")
        return value


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope_digest(scope: SecretScope) -> str:
    encoded = json.dumps(
        {
            "operation": scope.operation,
            "policy_revision": scope.policy_revision,
            "principal": scope.principal,
            "provider": scope.provider,
            "session": scope.session,
            "task": scope.task,
            "tenant": scope.tenant,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _digest(encoded)
