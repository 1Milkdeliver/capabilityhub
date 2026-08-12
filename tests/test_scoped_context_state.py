from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.residency import ResidentSection
from capabilityhub.scoped_context_state import ScopedContextState
from capabilityhub.tenancy import SqliteScopedState, TenantScope

_SCOPE_KEY = b"scoped-context-state-test-hmac-key"


def _scope(tenant: str, session: str = "same-session") -> TenantScope:
    return TenantScope(tenant, "same-principal", session, "same-task")


def _section(key: str, tokens: int, *, pinned: bool = False) -> ResidentSection:
    return ResidentSection(
        key,
        f"demo/{key}@1#sha256:abc",
        "contract",
        tokens,
        pinned=pinned,
    )


def _state(
    path,
    scope: TenantScope,
    *,
    event_limit: int = 100,
    repository: SqliteScopedState | None = None,
) -> ScopedContextState:
    return ScopedContextState(
        repository or SqliteScopedState(path, scope_key=_SCOPE_KEY),
        scope,
        max_portable_tokens=4,
        event_limit=event_limit,
    )


def test_same_keys_are_isolated_by_tenant_and_session_and_restore(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    scopes = (
        _scope("TENANT-CANARY-A"),
        _scope("TENANT-CANARY-B"),
        _scope("TENANT-CANARY-A", "second-session"),
    )
    repository = SqliteScopedState(path, scope_key=_SCOPE_KEY)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(
            pool.map(
                lambda pair: _state(path, pair[0], repository=repository).add(
                    _section("same-key", pair[1])
                ),
                zip(scopes, (1, 2, 3), strict=True),
            )
        )

    assert [
        _state(path, scope).snapshot().used_portable_tokens for scope in scopes
    ] == [1, 2, 3]
    assert _state(path, _scope("TENANT-CANARY-C")).snapshot().entries == ()
    raw = path.read_bytes()
    assert b"TENANT-CANARY" not in raw
    assert b"same-principal" not in raw
    assert b"same-session" not in raw
    assert b"same-task" not in raw


def test_eviction_and_control_events_are_atomic_bounded_and_observable(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    state = _state(path, _scope("tenant"), event_limit=2)
    state.add(_section("first", 2))
    state.add(_section("second", 2))
    evictions = state.add(_section("third", 2))

    assert [eviction.key for eviction in evictions] == ["first"]
    restored = _state(path, _scope("tenant"), event_limit=2)
    assert [entry.key for entry in restored.snapshot().entries] == ["second", "third"]
    assert [(event.sequence, event.action) for event in restored.events] == [
        (2, "add"),
        (3, "add"),
    ]
    assert restored.events[-1].evictions == evictions

    restored.pin("second")
    restored.access("third")
    removed = restored.remove("third")
    assert removed.reason == "explicit"
    assert [(event.sequence, event.action) for event in restored.events] == [
        (5, "access"),
        (6, "remove"),
    ]


def test_failed_pinned_add_rolls_back_and_missing_error_is_redacted(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    state = _state(path, _scope("tenant"))
    state.add(_section("required", 4, pinned=True))
    before = state.snapshot()

    with pytest.raises(CapabilityHubError) as exhausted:
        state.add(_section("also-required", 1, pinned=True))
    assert exhausted.value.code == "context_budget_exhausted"
    assert state.snapshot() == before

    with pytest.raises(CapabilityHubError) as missing:
        state.access("SECRET-MISSING-KEY")
    assert missing.value.code == "context_entry_not_found"
    assert "SECRET" not in str(missing.value.as_dict())


def test_scope_limit_mismatch_fails_closed_without_cross_scope_effect(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    state = _state(path, _scope("tenant-a"))
    state.add(_section("entry", 2))

    with pytest.raises(CapabilityHubError) as mismatch:
        ScopedContextState(
            SqliteScopedState(path, scope_key=_SCOPE_KEY),
            _scope("tenant-a"),
            max_portable_tokens=5,
        )
    assert mismatch.value.code == "scoped_context_state_invalid"
    assert _state(path, _scope("tenant-b")).snapshot().entries == ()


def test_same_scope_concurrent_updates_are_atomic(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteScopedState(path, scope_key=_SCOPE_KEY)
    scope = _scope("tenant")

    def add(index: int) -> None:
        _state(path, scope, repository=repository).add(_section(f"zero-{index}", 0))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(40)))

    restored = _state(path, scope)
    assert restored.snapshot().generation == 40
    assert len(restored.snapshot().entries) == 40
    assert [event.sequence for event in restored.events] == list(range(1, 41))
