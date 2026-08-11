from __future__ import annotations

import pytest

from capabilityhub.budget import BudgetExceeded, BudgetLedger


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
