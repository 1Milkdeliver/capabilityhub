"""Durable SQLite-backed budget accounting for local and multi-process runtimes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from capabilityhub.budget import (
    BudgetExceeded,
    BudgetLedger,
    BudgetReservation,
    BudgetSnapshot,
    _validate_amounts,
)


class SqliteBudgetRepository:
    """Own durable budget scopes and serialize admission with ``BEGIN IMMEDIATE``."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS budget_scopes (
                    scope TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS budget_limits (
                    scope TEXT NOT NULL,
                    counter TEXT NOT NULL,
                    hard_limit INTEGER NOT NULL CHECK (hard_limit >= 0),
                    PRIMARY KEY (scope, counter),
                    FOREIGN KEY (scope) REFERENCES budget_scopes(scope)
                );
                CREATE TABLE IF NOT EXISTS budget_counters (
                    scope TEXT NOT NULL,
                    counter TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
                    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
                    PRIMARY KEY (scope, counter),
                    FOREIGN KEY (scope) REFERENCES budget_scopes(scope)
                );
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    amounts_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'reconciled', 'cancelled')),
                    FOREIGN KEY (scope) REFERENCES budget_scopes(scope)
                );
                """
            )

    @property
    def path(self) -> Path:
        return self._path

    def ledger(self, scope: str, limits: Mapping[str, int]) -> SqliteBudgetLedger:
        if not scope:
            raise ValueError("scope must be non-empty")
        normalized = _validate_amounts(limits, label="limits", drop_zero=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            created = connection.execute(
                "INSERT OR IGNORE INTO budget_scopes(scope) VALUES (?)", (scope,)
            ).rowcount
            if created:
                connection.executemany(
                    "INSERT INTO budget_limits(scope, counter, hard_limit) VALUES (?, ?, ?)",
                    ((scope, counter, value) for counter, value in normalized.items()),
                )
        return SqliteBudgetLedger(self, scope)

    def configure(self, scope: str, limits: Mapping[str, int]) -> SqliteBudgetLedger:
        """Merge hard limits after proving current usage and reservations still fit."""

        normalized = _validate_amounts(limits, label="limits", drop_zero=False)
        if not normalized:
            raise ValueError("at least one limit is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_scope(connection, scope)
            for counter, limit in normalized.items():
                used, reserved = self._counter(connection, scope, counter)
                requested_total = used + reserved
                if requested_total > limit:
                    raise BudgetExceeded(
                        scope=scope,
                        counter=counter,
                        limit=limit,
                        requested_total=requested_total,
                    )
                connection.execute(
                    "INSERT INTO budget_limits(scope, counter, hard_limit) VALUES (?, ?, ?) "
                    "ON CONFLICT(scope, counter) DO UPDATE SET hard_limit = excluded.hard_limit",
                    (scope, counter, limit),
                )
        return SqliteBudgetLedger(self, scope)

    def _reserve(self, scope: str, amounts: Mapping[str, int], reservation_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_scope(connection, scope)
            self._check_capacity(connection, scope, amounts)
            try:
                connection.execute(
                    "INSERT INTO budget_reservations VALUES (?, ?, ?, 'active')",
                    (reservation_id, scope, _encode_amounts(amounts)),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("reservation_id must be unique") from error
            for counter, amount in amounts.items():
                self._add_counter(connection, scope, counter, reserved=amount)

    def _finish(
        self,
        scope: str,
        reservation_id: str,
        actual: Mapping[str, int] | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT amounts_json, status FROM budget_reservations "
                "WHERE reservation_id = ? AND scope = ?",
                (reservation_id, scope),
            ).fetchone()
            if row is None or row[1] != "active":
                raise RuntimeError("reservation is no longer active")
            reserved = _decode_amounts(row[0])
            charged = actual or {}
            self._check_capacity(connection, scope, charged, replacing=reserved)
            for counter in set(reserved) | set(charged):
                used, held = self._counter(connection, scope, counter)
                replacement = held - reserved.get(counter, 0)
                if replacement < 0:
                    raise RuntimeError(f"negative reserved budget for {counter!r}")
                connection.execute(
                    "INSERT INTO budget_counters(scope, counter, used, reserved) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(scope, counter) DO UPDATE SET "
                    "used = excluded.used, reserved = excluded.reserved",
                    (scope, counter, used + charged.get(counter, 0), replacement),
                )
            connection.execute(
                "UPDATE budget_reservations SET status = ? WHERE reservation_id = ?",
                ("reconciled" if actual is not None else "cancelled", reservation_id),
            )

    def _snapshot(self, scope: str) -> BudgetSnapshot:
        with self._connect() as connection:
            self._require_scope(connection, scope)
            limits = dict(
                connection.execute(
                    "SELECT counter, hard_limit FROM budget_limits WHERE scope = ?", (scope,)
                )
            )
            rows = connection.execute(
                "SELECT counter, used, reserved FROM budget_counters WHERE scope = ?", (scope,)
            )
            counters = {counter: (used, reserved) for counter, used, reserved in rows}
        names = set(limits) | set(counters)
        used = {counter: counters.get(counter, (0, 0))[0] for counter in names}
        reserved = {counter: counters.get(counter, (0, 0))[1] for counter in names}
        remaining = {
            counter: max(0, limit - used.get(counter, 0) - reserved.get(counter, 0))
            for counter, limit in limits.items()
        }
        return BudgetSnapshot(
            scope,
            MappingProxyType(limits),
            MappingProxyType(used),
            MappingProxyType(reserved),
            MappingProxyType(remaining),
        )

    def _check_capacity(
        self,
        connection: sqlite3.Connection,
        scope: str,
        amounts: Mapping[str, int],
        *,
        replacing: Mapping[str, int] | None = None,
    ) -> None:
        replaced = replacing or {}
        limits = dict(
            connection.execute(
                "SELECT counter, hard_limit FROM budget_limits WHERE scope = ?", (scope,)
            )
        )
        for counter, amount in amounts.items():
            limit = limits.get(counter)
            if limit is None:
                continue
            used, reserved = self._counter(connection, scope, counter)
            requested_total = used + reserved - replaced.get(counter, 0) + amount
            if requested_total > limit:
                raise BudgetExceeded(
                    scope=scope,
                    counter=counter,
                    limit=limit,
                    requested_total=requested_total,
                )

    @staticmethod
    def _counter(connection: sqlite3.Connection, scope: str, counter: str) -> tuple[int, int]:
        row = connection.execute(
            "SELECT used, reserved FROM budget_counters WHERE scope = ? AND counter = ?",
            (scope, counter),
        ).fetchone()
        return (0, 0) if row is None else (int(row[0]), int(row[1]))

    @staticmethod
    def _add_counter(
        connection: sqlite3.Connection,
        scope: str,
        counter: str,
        *,
        reserved: int,
    ) -> None:
        connection.execute(
            "INSERT INTO budget_counters(scope, counter, reserved) VALUES (?, ?, ?) "
            "ON CONFLICT(scope, counter) DO UPDATE SET reserved = reserved + excluded.reserved",
            (scope, counter, reserved),
        )

    @staticmethod
    def _require_scope(connection: sqlite3.Connection, scope: str) -> None:
        if (
            connection.execute("SELECT 1 FROM budget_scopes WHERE scope = ?", (scope,)).fetchone()
            is None
        ):
            raise KeyError(f"unknown budget scope: {scope}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


class SqliteBudgetLedger(BudgetLedger):
    """A ``BudgetLedger``-compatible durable scope."""

    def __init__(self, repository: SqliteBudgetRepository, scope: str) -> None:
        self.scope = scope
        self.parent = None
        self._repository = repository

    @property
    def limits(self) -> Mapping[str, int]:
        return self.snapshot().limits

    def create_child(self, scope: str, limits: Mapping[str, int]) -> BudgetLedger:
        raise NotImplementedError("persistent hierarchical scopes are not implemented")

    def reserve(
        self,
        amounts: Mapping[str, int],
        *,
        reservation_id: str | None = None,
    ) -> BudgetReservation:
        requested = _validate_amounts(amounts, label="amounts")
        identifier = reservation_id or uuid4().hex
        self._repository._reserve(self.scope, requested, identifier)
        return SqliteBudgetReservation(self, identifier, requested)

    def spend(self, amounts: Mapping[str, int]) -> None:
        reservation = self.reserve(amounts)
        reservation.reconcile(amounts)

    def snapshot(self) -> BudgetSnapshot:
        return self._repository._snapshot(self.scope)


class SqliteBudgetReservation(BudgetReservation):
    ledger: SqliteBudgetLedger

    def __init__(
        self, ledger: SqliteBudgetLedger, reservation_id: str, amounts: Mapping[str, int]
    ) -> None:
        self.ledger = ledger
        self.reservation_id = reservation_id
        self.amounts = MappingProxyType(dict(amounts))
        self._active = True

    def reconcile(self, actual: Mapping[str, int]) -> None:
        charged = _validate_amounts(actual, label="actual")
        if not self._active:
            raise RuntimeError("reservation is no longer active")
        self.ledger._repository._finish(self.ledger.scope, self.reservation_id, charged)
        self._active = False

    def cancel(self) -> None:
        if not self._active:
            raise RuntimeError("reservation is no longer active")
        self.ledger._repository._finish(self.ledger.scope, self.reservation_id, None)
        self._active = False

    release = cancel


def _encode_amounts(amounts: Mapping[str, int]) -> str:
    return json.dumps(dict(amounts), sort_keys=True, separators=(",", ":"))


def _decode_amounts(value: str) -> dict[str, int]:
    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise RuntimeError("stored budget reservation is invalid")
    return _validate_amounts(raw, label="stored amounts")
