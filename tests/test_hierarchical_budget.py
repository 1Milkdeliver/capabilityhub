from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.budget import BudgetExceeded
from capabilityhub.errors import CapabilityHubError
from capabilityhub.hierarchical_budget import (
    DurableHierarchicalBudgetProvider,
    SQLiteHierarchicalBudgetStore,
    load_or_create_hmac_key,
)

_KEY = b"hierarchical-budget-test-key-32-bytes-minimum"


def test_scope_names_are_hmac_identifiers_and_never_persisted(tmp_path) -> None:
    path = tmp_path / "budgets.sqlite3"
    store = SQLiteHierarchicalBudgetStore(path, hmac_key=_KEY)
    root = store.root("tenant-private-name", {"tokens": 100})
    child = root.create_child("raw-task-private-name", {"tokens": 40})

    assert len(root.opaque_root) == len(root.scope_id) == len(child.scope_id) == 64
    with sqlite3.connect(path) as connection:
        dump = " ".join(connection.iterdump())
    assert "tenant-private-name" not in dump
    assert "raw-task-private-name" not in dump


def test_children_cannot_allocate_more_than_parent_quota(tmp_path) -> None:
    store = SQLiteHierarchicalBudgetStore(tmp_path / "budgets.sqlite3", hmac_key=_KEY)
    root = store.root("tenant", {"tokens": 10})
    root.create_child("first", {"tokens": 7})

    with pytest.raises(BudgetExceeded) as caught:
        root.create_child("second", {"tokens": 4})

    assert caught.value.details["scope"] == root.scope_id
    assert caught.value.details["requested_total"] == 11
    assert len(store.snapshots(root.opaque_root)) == 2


def test_create_is_idempotent_but_different_limits_fail_closed(tmp_path) -> None:
    store = SQLiteHierarchicalBudgetStore(tmp_path / "budgets.sqlite3", hmac_key=_KEY)
    root = store.root("tenant", {"tokens": 10})
    first = root.create_child("task", {"tokens": 5})
    reopened = root.create_child("task", {"tokens": 5})

    assert reopened.scope_id == first.scope_id
    with pytest.raises(CapabilityHubError) as caught:
        root.create_child("task", {"tokens": 4})
    assert caught.value.code == "budget_scope_exists"


def test_limit_update_requires_cas_and_preserves_parent_and_child_allocations(tmp_path) -> None:
    store = SQLiteHierarchicalBudgetStore(tmp_path / "budgets.sqlite3", hmac_key=_KEY)
    root = store.root("tenant", {"tokens": 10})
    child = root.create_child("task", {"tokens": 5})
    child.create_child("attempt", {"tokens": 4})

    with pytest.raises(BudgetExceeded):
        child.configure({"tokens": 3}, expected_limits={"tokens": 5})
    with pytest.raises(CapabilityHubError) as caught:
        child.configure({"tokens": 6}, expected_limits={"tokens": 4})
    assert caught.value.code == "budget_limits_changed"

    child.configure({"tokens": 6}, expected_limits={"tokens": 5})
    assert child.snapshot().limits == {"tokens": 6}


def test_opaque_root_prevents_cross_tenant_scope_open(tmp_path) -> None:
    store = SQLiteHierarchicalBudgetStore(tmp_path / "budgets.sqlite3", hmac_key=_KEY)
    first = store.root("tenant-a", {"tokens": 10})
    second = store.root("tenant-b", {"tokens": 10})
    child = first.create_child("same-task", {"tokens": 5})

    with pytest.raises(CapabilityHubError) as caught:
        store.open_scope(second.opaque_root, child.scope_id)
    assert caught.value.code == "unknown_budget_scope"


def test_restart_and_bounded_deterministic_snapshot_list(tmp_path) -> None:
    path = tmp_path / "budgets.sqlite3"
    first_store = SQLiteHierarchicalBudgetStore(path, hmac_key=_KEY)
    root = first_store.root("tenant", {"tokens": 20})
    child = root.create_child("task", {"tokens": 10})

    restarted = SQLiteHierarchicalBudgetStore(path, hmac_key=_KEY)
    reopened_root = restarted.root("tenant", {"tokens": 20})
    reopened_child = restarted.open_scope(reopened_root.opaque_root, child.scope_id)

    assert reopened_child.snapshot().limits == {"tokens": 10}
    snapshots = restarted.snapshots(root.opaque_root, limit=2)
    assert [snapshot.scope for snapshot in snapshots] == sorted([root.scope_id, child.scope_id])
    with pytest.raises(ValueError):
        restarted.snapshots(root.opaque_root, limit=restarted.MAX_SCOPES + 1)


def test_reserve_reconcile_and_cancel_update_every_ancestor_atomically(tmp_path) -> None:
    store = SQLiteHierarchicalBudgetStore(tmp_path / "budgets.sqlite3", hmac_key=_KEY)
    root = store.root("tenant", {"tokens": 100})
    child = root.create_child("task", {"tokens": 40})
    leaf = child.create_child("attempt", {"tokens": 20})

    reservation = leaf.reserve({"tokens": 15})
    assert [scope.snapshot().reserved["tokens"] for scope in (root, child, leaf)] == [15, 15, 15]

    reservation.reconcile({"tokens": 12})
    assert [scope.snapshot().used["tokens"] for scope in (root, child, leaf)] == [12, 12, 12]
    assert all(scope.snapshot().reserved["tokens"] == 0 for scope in (root, child, leaf))

    cancelled = leaf.reserve({"tokens": 5})
    cancelled.cancel()
    assert all(scope.snapshot().reserved["tokens"] == 0 for scope in (root, child, leaf))
    with pytest.raises(CapabilityHubError) as caught:
        cancelled.cancel()
    assert caught.value.code == "budget_reservation_inactive"


def test_failed_reconcile_rolls_back_and_keeps_reservation_active(tmp_path) -> None:
    store = SQLiteHierarchicalBudgetStore(tmp_path / "budgets.sqlite3", hmac_key=_KEY)
    root = store.root("tenant", {"tokens": 10})
    child = root.create_child("task", {"tokens": 10})
    reservation = child.reserve({"tokens": 5})

    with pytest.raises(BudgetExceeded):
        reservation.reconcile({"tokens": 11})

    assert reservation.active
    assert root.snapshot().reserved["tokens"] == 5
    assert child.snapshot().reserved["tokens"] == 5
    assert root.snapshot().used["tokens"] == 0


def test_active_reservation_can_be_reopened_after_restart(tmp_path) -> None:
    path = tmp_path / "budgets.sqlite3"
    first_store = SQLiteHierarchicalBudgetStore(path, hmac_key=_KEY)
    root = first_store.root("tenant", {"tokens": 10})
    child = root.create_child("task", {"tokens": 10})
    reservation = child.reserve({"tokens": 6})

    restarted = SQLiteHierarchicalBudgetStore(path, hmac_key=_KEY)
    reopened = restarted.open_scope(root.opaque_root, child.scope_id)
    recovered = reopened.reservation(reservation.reservation_id)
    recovered.reconcile({"tokens": 4})

    assert restarted.open_scope(root.opaque_root, root.scope_id).snapshot().used["tokens"] == 4
    assert reopened.snapshot().used["tokens"] == 4


def test_concurrent_children_cannot_reserve_beyond_parent_hard_cap(tmp_path) -> None:
    store = SQLiteHierarchicalBudgetStore(tmp_path / "budgets.sqlite3", hmac_key=_KEY)
    root = store.root("tenant", {"executions": 1, "tokens": 20})
    children = [root.create_child(f"task-{index}", {"tokens": 5}) for index in range(4)]

    def attempt(index: int) -> bool:
        try:
            children[index].reserve({"executions": 1})
        except BudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=4) as pool:
        admitted = list(pool.map(attempt, range(4)))

    assert admitted.count(True) == 1
    assert root.snapshot().reserved["executions"] == 1
    assert sum(child.snapshot().reserved.get("executions", 0) for child in children) == 1


def test_reservation_is_bound_to_opaque_root_and_scope(tmp_path) -> None:
    store = SQLiteHierarchicalBudgetStore(tmp_path / "budgets.sqlite3", hmac_key=_KEY)
    first = store.root("tenant-a", {"tokens": 10})
    second = store.root("tenant-b", {"tokens": 10})
    reservation = first.reserve({"tokens": 1})

    with pytest.raises(CapabilityHubError) as caught:
        second.reservation(reservation.reservation_id)
    assert caught.value.code == "unknown_budget_reservation"


def test_concurrent_child_creation_cannot_overallocate_parent_quota(tmp_path) -> None:
    store = SQLiteHierarchicalBudgetStore(tmp_path / "budgets.sqlite3", hmac_key=_KEY)
    root = store.root("tenant", {"tokens": 10})

    def create(index: int) -> bool:
        try:
            root.create_child(f"task-{index}", {"tokens": 3})
        except BudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(create, range(8)))

    assert created.count(True) == 3
    child_limits = sum(
        snapshot.limits.get("tokens", 0)
        for snapshot in store.snapshots(root.opaque_root)
        if snapshot.scope != root.scope_id
    )
    assert child_limits == 9


def test_corrupt_cycle_fails_closed_before_creating_a_child(tmp_path) -> None:
    path = tmp_path / "budgets.sqlite3"
    store = SQLiteHierarchicalBudgetStore(path, hmac_key=_KEY)
    root = store.root("tenant", {"tokens": 10})
    child = root.create_child("task", {"tokens": 5})
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "UPDATE hierarchical_budget_scopes SET parent_scope_id = ? WHERE scope_id = ?",
            (child.scope_id, root.scope_id),
        )

    with pytest.raises(CapabilityHubError) as caught:
        child.create_child("attempt", {"tokens": 1})
    assert caught.value.code == "budget_state_invalid"


def test_local_hmac_key_creation_is_atomic_and_restart_stable(tmp_path) -> None:
    path = tmp_path / "private" / "budget.key"
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: load_or_create_hmac_key(path), range(16)))

    assert len(set(keys)) == 1
    assert len(keys[0]) == 32
    assert load_or_create_hmac_key(path) == keys[0]


def test_durable_provider_derives_four_private_levels_and_reopens_tasks(tmp_path) -> None:
    path = tmp_path / "budgets.sqlite3"
    store = SQLiteHierarchicalBudgetStore(path, hmac_key=_KEY)
    provider = DurableHierarchicalBudgetProvider(
        store,
        tenant_scope="private-tenant",
        principal_scope="private-principal",
        session_scope="private-session",
        aggregate_limits={"tokens": 100},
        task_limits={"tokens": 10},
    )
    first = provider("private-task")
    first.spend({"tokens": 3})

    restarted = DurableHierarchicalBudgetProvider(
        SQLiteHierarchicalBudgetStore(path, hmac_key=_KEY),
        tenant_scope="private-tenant",
        principal_scope="private-principal",
        session_scope="private-session",
        aggregate_limits={"tokens": 100},
        task_limits={"tokens": 10},
    )
    reopened = restarted("private-task")

    assert reopened.scope_id == first.scope_id
    assert reopened.snapshot().used["tokens"] == 3
    assert len(store.snapshots(provider.opaque_root)) == 4
    with sqlite3.connect(path) as connection:
        dump = " ".join(connection.iterdump())
    for private_value in (
        "private-tenant",
        "private-principal",
        "private-session",
        "private-task",
    ):
        assert private_value not in dump
