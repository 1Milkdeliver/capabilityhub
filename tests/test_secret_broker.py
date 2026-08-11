from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Lock

import pytest

from capabilityhub.secret_broker import (
    ScopedSecretBroker,
    SecretAuditEvent,
    SecretBrokerError,
    SecretConsumerContext,
    SecretScope,
)


def _scope() -> SecretScope:
    return SecretScope(
        tenant="tenant-1",
        principal="principal-1",
        session="session-1",
        task="task-1",
        provider="http-api",
        operation="create",
        policy_revision="policy-7",
    )


def test_environment_alias_is_resolved_only_inside_trusted_consumer() -> None:
    environment: dict[str, str] = {}
    observed: list[str] = []
    contexts: list[SecretConsumerContext] = []

    def consumer(secret: str, context: SecretConsumerContext) -> None:
        observed.append(secret)
        contexts.append(context)

    broker = ScopedSecretBroker(
        {"http-api": consumer},
        environment=environment.get,
        clock=lambda: 100,
    )
    handle = broker.issue("SERVICE_TOKEN", scope=_scope(), ttl_seconds=60)
    environment["SERVICE_TOKEN"] = "plaintext-secret"

    receipt = broker.consume(handle, scope=_scope())

    assert observed == ["plaintext-secret"]
    assert contexts[0].scope == _scope()
    assert receipt.uses_remaining == 0
    assert "plaintext-secret" not in repr(receipt)
    assert "SERVICE_TOKEN" not in repr(receipt)
    assert "SERVICE_TOKEN" not in repr(handle)


@pytest.mark.parametrize(
    "scope",
    [
        replace(_scope(), tenant="other"),
        replace(_scope(), principal="other"),
        replace(_scope(), session="other"),
        replace(_scope(), task="other"),
        replace(_scope(), provider="other"),
        replace(_scope(), operation="other"),
        replace(_scope(), policy_revision="other"),
    ],
)
def test_handle_is_bound_to_every_scope_dimension(scope: SecretScope) -> None:
    broker = ScopedSecretBroker(
        {"http-api": lambda _secret, _context: None},
        environment=lambda _alias: "secret",
    )
    handle = broker.issue("TOKEN", scope=_scope(), ttl_seconds=60, now=100)

    with pytest.raises(SecretBrokerError) as raised:
        broker.consume(handle, scope=scope, now=101)

    assert raised.value.code == "secret_scope_mismatch"
    assert broker.active_count() == 1


def test_expiry_and_replay_fail_closed() -> None:
    calls: list[str] = []
    broker = ScopedSecretBroker(
        {"http-api": lambda secret, _context: calls.append(secret)},
        environment=lambda _alias: "secret",
    )
    expired = broker.issue("TOKEN", scope=_scope(), ttl_seconds=1, now=100)
    with pytest.raises(SecretBrokerError) as raised:
        broker.consume(expired, scope=_scope(), now=101)
    assert raised.value.code == "secret_handle_expired"

    once = broker.issue("TOKEN", scope=_scope(), ttl_seconds=60, now=100)
    broker.consume(once, scope=_scope(), now=101)
    with pytest.raises(SecretBrokerError) as replayed:
        broker.consume(once, scope=_scope(), now=102)
    assert replayed.value.code == "secret_handle_consumed"
    assert calls == ["secret"]


def test_bounded_use_handle_is_atomic_under_concurrency() -> None:
    calls = 0
    lock = Lock()

    def consumer(_secret: str, _context: SecretConsumerContext) -> None:
        nonlocal calls
        with lock:
            calls += 1

    broker = ScopedSecretBroker(
        {"http-api": consumer},
        environment=lambda _alias: "secret",
    )
    handle = broker.issue("TOKEN", scope=_scope(), ttl_seconds=60, max_uses=3, now=100)

    def consume(_: int) -> str:
        try:
            broker.consume(handle, scope=_scope(), now=101)
        except SecretBrokerError as error:
            return error.code
        return "success"

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(consume, range(24)))

    assert outcomes.count("success") == 3
    assert outcomes.count("secret_handle_consumed") == 21
    assert calls == 3


def test_missing_alias_and_consumer_failure_are_sanitized_and_spend_use() -> None:
    events: list[SecretAuditEvent] = []
    alias = "VERY_PRIVATE_ALIAS"
    secret = "VERY_PRIVATE_VALUE"
    broker = ScopedSecretBroker(
        {"http-api": lambda _secret, _context: (_ for _ in ()).throw(RuntimeError(secret))},
        environment=lambda name: secret if name == alias else None,
        audit_sink=events.append,
    )
    handle = broker.issue(alias, scope=_scope(), ttl_seconds=60, now=100)

    with pytest.raises(SecretBrokerError) as raised:
        broker.consume(handle, scope=_scope(), now=101)

    assert raised.value.code == "trusted_secret_consumer_failed"
    serialized = repr([raised.value.as_dict(), *events])
    assert alias not in serialized
    assert secret not in serialized
    assert handle.token not in serialized
    assert raised.value.details == {}
    with pytest.raises(SecretBrokerError, match="unavailable"):
        broker.consume(handle, scope=_scope(), now=102)


def test_alias_syntax_and_hard_bounds_are_enforced() -> None:
    broker = ScopedSecretBroker({"http-api": lambda _secret, _context: None})
    for alias in ("", "plaintext secret", "NAME=VALUE", "A" * 129):
        with pytest.raises(ValueError, match="alias"):
            broker.issue(alias, scope=_scope(), ttl_seconds=1)
    with pytest.raises(ValueError, match="ttl_seconds"):
        broker.issue("TOKEN", scope=_scope(), ttl_seconds=broker.MAX_TTL_SECONDS + 1)
    with pytest.raises(ValueError, match="max_uses"):
        broker.issue("TOKEN", scope=_scope(), ttl_seconds=1, max_uses=broker.MAX_USES + 1)


def test_environment_resolver_exception_is_sanitized() -> None:
    alias = "PRIVATE_ALIAS"

    def resolver(_alias: str) -> str:
        raise RuntimeError(f"failed to read {alias}")

    broker = ScopedSecretBroker(
        {"http-api": lambda _secret, _context: None},
        environment=resolver,
    )
    handle = broker.issue(alias, scope=_scope(), ttl_seconds=60, now=100)

    with pytest.raises(SecretBrokerError) as raised:
        broker.consume(handle, scope=_scope(), now=101)

    assert raised.value.code == "secret_alias_unavailable"
    assert alias not in repr(raised.value.as_dict())
