from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.errors import ErrorCategory
from capabilityhub.tenancy import SqliteScopedState, TenantScope, TenantStateError

SCOPE_KEY = b"tenant-scope-test-key-material-32b"


def _scope(tenant: str = "tenant-a", *, task: str = "task-a") -> TenantScope:
    return TenantScope(tenant, "principal-a", "session-a", task)


def test_scope_normalizes_identifiers_and_binds_every_dimension() -> None:
    normalized = TenantScope("  tenant-a  ", "\uff50rincipal", "session", "task")
    canonical = TenantScope("tenant-a", "principal", "session", "task")

    assert normalized == canonical
    assert normalized.digest(SCOPE_KEY) == canonical.digest(SCOPE_KEY)

    baseline = canonical.digest(SCOPE_KEY)
    variants = (
        TenantScope("tenant-b", "principal", "session", "task"),
        TenantScope("tenant-a", "principal-b", "session", "task"),
        TenantScope("tenant-a", "principal", "session-b", "task"),
        TenantScope("tenant-a", "principal", "session", "task-b"),
    )
    assert all(scope.digest(SCOPE_KEY) != baseline for scope in variants)


@pytest.mark.parametrize("value", ["", "  ", "bad\nvalue", "x" * 257])
def test_scope_rejects_invalid_identifiers_without_echoing_them(value: str) -> None:
    with pytest.raises(ValueError) as caught:
        TenantScope(value, "principal", "session", "task")

    if value:
        assert value not in str(caught.value)
    assert str(caught.value) == "tenant is invalid"


def test_kv_reads_writes_deletes_and_enumeration_are_scope_partitioned(tmp_path) -> None:
    repository = SqliteScopedState(tmp_path / "state.sqlite3", scope_key=SCOPE_KEY)
    first = _scope("tenant-a")
    second = _scope("tenant-b")
    repository.set(first, "same-key", {"owner": "first"})
    repository.set(second, "same-key", {"owner": "second"})

    assert repository.get(first, "same-key") == {"owner": "first"}
    assert repository.get(second, "same-key") == {"owner": "second"}
    assert repository.get(_scope("tenant-c"), "same-key") is None
    assert [entry.value for entry in repository.list_entries(first)] == [{"owner": "first"}]

    assert repository.delete(second, "same-key") is True
    assert repository.get(second, "same-key") is None
    assert repository.get(first, "same-key") == {"owner": "first"}


def test_namespace_and_key_are_opaque_and_isolated(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteScopedState(path, scope_key=SCOPE_KEY)
    scope = _scope()
    repository.set(scope, "PRIVATE-LOGICAL-KEY", "one", namespace="cache.one")
    repository.set(scope, "PRIVATE-LOGICAL-KEY", "two", namespace="cache.two")

    assert repository.get(scope, "PRIVATE-LOGICAL-KEY", namespace="cache.one") == "one"
    assert repository.get(scope, "PRIVATE-LOGICAL-KEY", namespace="cache.two") == "two"
    entries = repository.list_entries(scope, namespace="cache.one")
    assert len(entries) == 1
    assert len(entries[0].key_digest) == 64

    persisted = path.read_bytes()
    for forbidden in (
        b"tenant-a",
        b"principal-a",
        b"session-a",
        b"task-a",
        b"PRIVATE-LOGICAL-KEY",
    ):
        assert forbidden not in persisted


def test_cache_ttl_is_inclusive_and_cleanup_is_bounded_to_one_scope(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteScopedState(path, scope_key=SCOPE_KEY)
    first = _scope("tenant-a")
    second = _scope("tenant-b")
    repository.set_cache(first, "one", 1, ttl_seconds=5, now=10)
    repository.set_cache(first, "two", 2, ttl_seconds=5, now=10)
    repository.set_cache(second, "one", 3, ttl_seconds=5, now=10)

    assert repository.get_cache(first, "one", now=14.999) == 1
    assert repository.get_cache(first, "one", now=15) is None

    first_cleanup = repository.cleanup_expired(first, now=15, limit=1)
    assert first_cleanup.entries_deleted == 1
    second_cleanup = repository.cleanup_expired(first, now=15, limit=1)
    assert second_cleanup.entries_deleted == 1

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT scope_digest, COUNT(*) FROM scoped_entries GROUP BY scope_digest"
        ).fetchall()
    assert rows == [(second.digest(SCOPE_KEY), 1)]


def test_events_are_scope_partitioned_ordered_and_ttl_cleaned_locally(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteScopedState(path, scope_key=SCOPE_KEY)
    first = _scope("tenant-a")
    second = _scope("tenant-b")
    event_one = repository.append_event(first, {"event": 1}, stream="audit", now=1)
    event_two = repository.append_event(first, {"event": 2}, stream="audit", ttl_seconds=2, now=1)
    other = repository.append_event(
        second, {"event": "other"}, stream="audit", ttl_seconds=2, now=1
    )

    assert (event_one.sequence, event_two.sequence, other.sequence) == (1, 2, 1)
    assert [event.value for event in repository.list_events(first, stream="audit", now=2)] == [
        {"event": 1},
        {"event": 2},
    ]
    assert [
        event.value
        for event in repository.list_events(first, stream="audit", after_sequence=1, now=2)
    ] == [{"event": 2}]
    assert repository.list_events(second, stream="audit", now=3) == ()

    cleanup = repository.cleanup_expired(first, now=3)
    assert cleanup.events_deleted == 1
    with sqlite3.connect(path) as connection:
        second_count = connection.execute(
            "SELECT COUNT(*) FROM scoped_events WHERE scope_digest = ?",
            (second.digest(SCOPE_KEY),),
        ).fetchone()[0]
    assert second_count == 1


def test_concurrent_kv_and_event_operations_remain_isolated(tmp_path) -> None:
    repository = SqliteScopedState(
        tmp_path / "state.sqlite3", scope_key=SCOPE_KEY, timeout_seconds=10
    )
    first = _scope("tenant-a")
    second = _scope("tenant-b")

    def write(index: int) -> None:
        scope = first if index % 2 == 0 else second
        repository.set(scope, f"key-{index}", index)
        repository.append_event(scope, index, stream="events")

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, range(100)))

    assert len(repository.list_entries(first, limit=100)) == 50
    assert len(repository.list_entries(second, limit=100)) == 50
    first_events = repository.list_events(first, stream="events", limit=100)
    second_events = repository.list_events(second, stream="events", limit=100)
    assert [event.sequence for event in first_events] == list(range(1, 51))
    assert [event.sequence for event in second_events] == list(range(1, 51))
    assert all(isinstance(event.value, int) and event.value % 2 == 0 for event in first_events)
    assert all(isinstance(event.value, int) and event.value % 2 == 1 for event in second_events)


def test_corrupt_state_fails_closed_with_stable_redacted_error(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    repository = SqliteScopedState(path, scope_key=SCOPE_KEY)
    scope = _scope()
    repository.set(scope, "key", "value")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE scoped_entries SET value_json = ?", ("SECRET-CANARY{",))

    with pytest.raises(TenantStateError) as caught:
        repository.get(scope, "key")

    assert caught.value.code == "tenant_state_corrupt"
    assert caught.value.category is ErrorCategory.INTERNAL
    assert "SECRET-CANARY" not in str(caught.value.as_dict())


def test_invalid_cache_values_and_scope_keys_fail_closed(tmp_path) -> None:
    with pytest.raises(ValueError):
        SqliteScopedState(tmp_path / "short.sqlite3", scope_key=b"short")

    repository = SqliteScopedState(tmp_path / "state.sqlite3", scope_key=SCOPE_KEY)
    with pytest.raises(ValueError, match="finite JSON"):
        repository.set(_scope(), "key", float("nan"))
    with pytest.raises(ValueError, match="ttl_seconds"):
        repository.set_cache(_scope(), "key", "value", ttl_seconds=0)
