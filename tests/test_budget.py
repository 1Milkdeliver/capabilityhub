from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.budget import BudgetExceeded, BudgetLedger
from capabilityhub.budget_store import SqliteBudgetRepository


def test_reserve_and_reconcile_charge_child_and_parent_atomically() -> None:
    tenant = BudgetLedger("tenant", {"tokens": 100, "executions": 5})
    task = tenant.create_child("task", {"tokens": 40, "executions": 2})

    reservation = task.reserve({"tokens": 30, "executions": 1})
    assert task.snapshot().reserved["tokens"] == 30
    assert tenant.snapshot().reserved["tokens"] == 30

    reservation.reconcile({"tokens": 21, "executions": 1})
    assert task.snapshot().used["tokens"] == 21
    assert task.snapshot().reserved["tokens"] == 0
    assert tenant.snapshot().used["tokens"] == 21


def test_parent_cap_rejects_without_partially_reserving_child() -> None:
    tenant = BudgetLedger("tenant", {"tokens": 10})
    first = tenant.create_child("first", {"tokens": 10})
    second = tenant.create_child("second", {"tokens": 10})
    first.reserve({"tokens": 7})

    with pytest.raises(BudgetExceeded) as raised:
        second.reserve({"tokens": 4})

    assert raised.value.details["scope"] == "tenant"
    assert second.snapshot().reserved.get("tokens", 0) == 0
    assert tenant.snapshot().reserved["tokens"] == 7


def test_failed_reconcile_preserves_reservation_and_all_counters() -> None:
    ledger = BudgetLedger("task", {"tokens": 10})
    reservation = ledger.reserve({"tokens": 5})

    with pytest.raises(BudgetExceeded):
        reservation.reconcile({"tokens": 11})

    assert reservation.active
    assert ledger.snapshot().reserved["tokens"] == 5
    assert ledger.snapshot().used["tokens"] == 0
    reservation.reconcile({"tokens": 8})
    assert ledger.snapshot().used["tokens"] == 8


def test_cancel_releases_capacity_and_reservation_is_single_use() -> None:
    ledger = BudgetLedger("task", {"loads": 1})
    reservation = ledger.reserve({"loads": 1})
    reservation.cancel()

    assert ledger.snapshot().remaining["loads"] == 1
    with pytest.raises(RuntimeError):
        reservation.cancel()


def test_zero_limit_is_a_hard_cap_and_negative_amounts_are_invalid() -> None:
    ledger = BudgetLedger("task", {"retries": 0})
    with pytest.raises(BudgetExceeded):
        ledger.reserve({"retries": 1})
    with pytest.raises(ValueError):
        ledger.reserve({"tokens": -1})


def test_sqlite_budget_survives_repository_restart(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    first = SqliteBudgetRepository(path).ledger("task:one", {"tokens": 10, "loads": 2})
    reservation = first.reserve({"tokens": 6, "loads": 1})
    reservation.reconcile({"tokens": 4, "loads": 1})

    restarted = SqliteBudgetRepository(path).ledger("task:one", {"tokens": 999, "loads": 999})
    snapshot = restarted.snapshot()

    assert snapshot.limits == {"tokens": 10, "loads": 2}
    assert snapshot.used == {"tokens": 4, "loads": 1}
    assert snapshot.remaining == {"tokens": 6, "loads": 1}


def test_sqlite_budget_keeps_unfinished_reservations_after_restart(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    SqliteBudgetRepository(path).ledger("task", {"tokens": 5}).reserve({"tokens": 5})

    restarted = SqliteBudgetRepository(path).ledger("task", {"tokens": 5})

    assert restarted.snapshot().reserved["tokens"] == 5
    with pytest.raises(BudgetExceeded):
        restarted.reserve({"tokens": 1})


def test_sqlite_budget_serializes_concurrent_hard_cap_admission(tmp_path) -> None:
    repository = SqliteBudgetRepository(tmp_path / "state.sqlite3")
    ledger = repository.ledger("task", {"executions": 1})

    def try_reserve(index: int) -> bool:
        try:
            ledger.reserve({"executions": 1}, reservation_id=f"reservation-{index}")
        except BudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(try_reserve, range(8)))

    assert admitted.count(True) == 1
    assert ledger.snapshot().reserved["executions"] == 1


def test_sqlite_budget_rejects_limit_below_persisted_consumption(tmp_path) -> None:
    repository = SqliteBudgetRepository(tmp_path / "state.sqlite3")
    ledger = repository.ledger("task", {"tokens": 10})
    ledger.spend({"tokens": 7})

    with pytest.raises(BudgetExceeded) as raised:
        repository.configure("task", {"tokens": 6})

    assert raised.value.details == {
        "scope": "task",
        "counter": "tokens",
        "limit": 6,
        "requested_total": 7,
    }
    assert ledger.snapshot().limits["tokens"] == 10
