"""Durable, privacy-safe hierarchical hard-cap budgets."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import suppress
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
from capabilityhub.errors import CapabilityHubError, ErrorCategory

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hierarchical_budget_roots (
    root_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS hierarchical_budget_scopes (
    scope_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,
    parent_scope_id TEXT,
    depth INTEGER NOT NULL CHECK (depth >= 0 AND depth <= 64),
    FOREIGN KEY (root_id) REFERENCES hierarchical_budget_roots(root_id),
    FOREIGN KEY (parent_scope_id) REFERENCES hierarchical_budget_scopes(scope_id)
);
CREATE INDEX IF NOT EXISTS hierarchical_budget_scopes_root
ON hierarchical_budget_scopes(root_id, scope_id);
CREATE INDEX IF NOT EXISTS hierarchical_budget_scopes_parent
ON hierarchical_budget_scopes(root_id, parent_scope_id, scope_id);
CREATE TABLE IF NOT EXISTS hierarchical_budget_limits (
    scope_id TEXT NOT NULL,
    counter TEXT NOT NULL,
    hard_limit INTEGER NOT NULL CHECK (hard_limit >= 0),
    PRIMARY KEY (scope_id, counter),
    FOREIGN KEY (scope_id) REFERENCES hierarchical_budget_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS hierarchical_budget_counters (
    scope_id TEXT NOT NULL,
    counter TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    PRIMARY KEY (scope_id, counter),
    FOREIGN KEY (scope_id) REFERENCES hierarchical_budget_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS hierarchical_budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    amounts_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'reconciled', 'cancelled')),
    FOREIGN KEY (root_id) REFERENCES hierarchical_budget_roots(root_id),
    FOREIGN KEY (scope_id) REFERENCES hierarchical_budget_scopes(scope_id)
);
"""


class HierarchicalBudgetError(CapabilityHubError):
    """Stable failures that never expose caller-provided scope names."""

    def __init__(self, code: str, message: str, *, category: ErrorCategory) -> None:
        super().__init__(code=code, category=category, safe_message=message)


class SQLiteHierarchicalBudgetStore:
    """Persist a forest of opaque-root budget trees using SQLite transactions."""

    MAX_SCOPES = 1_000
    MAX_COUNTERS = 100
    MAX_DEPTH = 64

    def __init__(
        self,
        path: str | Path,
        *,
        hmac_key: bytes,
        timeout_seconds: float = 5.0,
    ) -> None:
        if len(hmac_key) < 32:
            raise ValueError("hmac_key must contain at least 32 bytes")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.path = Path(path).resolve()
        self._key = bytes(hmac_key)
        self._timeout = timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def root(self, scope_name: str, limits: Mapping[str, int]) -> HierarchicalBudgetScope:
        """Create or reopen a root; the clear-text name is never persisted."""

        name = _scope_name(scope_name)
        normalized = self._limits(limits)
        root_id = self._digest(b"root", name.encode("utf-8"))
        connection = self._begin()
        try:
            row = connection.execute(
                "SELECT root_id, parent_scope_id, depth FROM hierarchical_budget_scopes "
                "WHERE scope_id = ?",
                (root_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO hierarchical_budget_roots(root_id) VALUES (?)", (root_id,)
                )
                connection.execute(
                    "INSERT INTO hierarchical_budget_scopes "
                    "(scope_id, root_id, parent_scope_id, depth) VALUES (?, ?, NULL, 0)",
                    (root_id, root_id),
                )
                self._write_limits(connection, root_id, normalized)
            elif row != (root_id, None, 0) or self._read_limits(connection, root_id) != normalized:
                raise _conflict(
                    "budget_scope_exists",
                    "The opaque budget scope already exists with different limits.",
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return HierarchicalBudgetScope(self, root_id, root_id, None, 0)

    def open_scope(self, opaque_root: str, scope_id: str) -> HierarchicalBudgetScope:
        """Reopen a scope only when it belongs to the supplied opaque root."""

        _opaque_id(opaque_root)
        _opaque_id(scope_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT parent_scope_id, depth FROM hierarchical_budget_scopes "
                "WHERE scope_id = ? AND root_id = ?",
                (scope_id, opaque_root),
            ).fetchone()
        if row is None:
            raise _unknown_scope()
        return HierarchicalBudgetScope(self, opaque_root, scope_id, row[0], int(row[1]))

    def snapshots(
        self,
        opaque_root: str,
        *,
        limit: int = 100,
    ) -> tuple[BudgetSnapshot, ...]:
        """Return a deterministic, bounded list containing only opaque scope IDs."""

        _opaque_id(opaque_root)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.MAX_SCOPES
        ):
            raise ValueError(f"limit must be from 1 to {self.MAX_SCOPES}")
        with self._connect() as connection:
            self._require_root(connection, opaque_root)
            scope_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT scope_id FROM hierarchical_budget_scopes "
                    "WHERE root_id = ? ORDER BY scope_id LIMIT ?",
                    (opaque_root, limit),
                )
            ]
            return tuple(self._snapshot(connection, scope_id) for scope_id in scope_ids)

    def _create_child(
        self,
        parent: HierarchicalBudgetScope,
        scope_name: str,
        limits: Mapping[str, int],
    ) -> HierarchicalBudgetScope:
        name = _scope_name(scope_name)
        normalized = self._limits(limits)
        if parent.depth >= self.MAX_DEPTH:
            raise _conflict("budget_depth_exceeded", "The budget hierarchy is too deep.")
        scope_id = self._digest(
            b"scope",
            parent.opaque_root.encode("ascii"),
            parent.scope_id.encode("ascii"),
            name.encode("utf-8"),
        )
        connection = self._begin()
        try:
            parent_scope_id, parent_depth = self._validated_metadata(connection, parent)
            if parent_scope_id != parent.parent_scope_id:
                raise _corrupt()
            parent_chain = self._chain(connection, parent.opaque_root, parent.scope_id)
            if len(parent_chain) - 1 != parent_depth:
                raise _corrupt()
            if parent_depth >= self.MAX_DEPTH:
                raise _conflict("budget_depth_exceeded", "The budget hierarchy is too deep.")
            existing = connection.execute(
                "SELECT root_id, parent_scope_id, depth FROM hierarchical_budget_scopes "
                "WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing != (parent.opaque_root, parent.scope_id, parent_depth + 1)
                    or self._read_limits(connection, scope_id) != normalized
                ):
                    raise _conflict(
                        "budget_scope_exists",
                        "The opaque budget scope already exists with different limits.",
                    )
            else:
                self._check_parent_allocation(
                    connection,
                    parent.scope_id,
                    normalized,
                    excluding_scope=None,
                )
                connection.execute(
                    "INSERT INTO hierarchical_budget_scopes "
                    "(scope_id, root_id, parent_scope_id, depth) VALUES (?, ?, ?, ?)",
                    (scope_id, parent.opaque_root, parent.scope_id, parent_depth + 1),
                )
                self._write_limits(connection, scope_id, normalized)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return HierarchicalBudgetScope(
            self,
            parent.opaque_root,
            scope_id,
            parent.scope_id,
            parent_depth + 1,
        )

    def _configure(
        self,
        scope: HierarchicalBudgetScope,
        limits: Mapping[str, int],
        *,
        expected_limits: Mapping[str, int],
    ) -> None:
        changes = self._limits(limits)
        expected = self._limits(expected_limits)
        connection = self._begin()
        try:
            parent_scope_id, depth = self._validated_metadata(connection, scope)
            if parent_scope_id != scope.parent_scope_id or depth != scope.depth:
                raise _corrupt()
            chain = self._chain(connection, scope.opaque_root, scope.scope_id)
            if len(chain) - 1 != depth:
                raise _corrupt()
            current = self._read_limits(connection, scope.scope_id)
            if current != expected:
                raise _conflict(
                    "budget_limits_changed",
                    "The budget limits changed before this update.",
                )
            replacement = {**current, **changes}
            if len(replacement) > self.MAX_COUNTERS:
                raise ValueError(f"at most {self.MAX_COUNTERS} counters are allowed")
            for counter, limit in changes.items():
                used, reserved = self._counter(connection, scope.scope_id, counter)
                if used + reserved > limit:
                    raise BudgetExceeded(
                        scope=scope.scope_id,
                        counter=counter,
                        limit=limit,
                        requested_total=used + reserved,
                    )
                child_total = self._child_allocation(connection, scope.scope_id, counter)
                if child_total > limit:
                    raise BudgetExceeded(
                        scope=scope.scope_id,
                        counter=counter,
                        limit=limit,
                        requested_total=child_total,
                    )
            if parent_scope_id is not None:
                self._check_parent_allocation(
                    connection,
                    parent_scope_id,
                    replacement,
                    excluding_scope=scope.scope_id,
                )
            self._write_limits(connection, scope.scope_id, changes)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validated_metadata(
        connection: sqlite3.Connection,
        scope: HierarchicalBudgetScope,
    ) -> tuple[str | None, int]:
        row = connection.execute(
            "SELECT parent_scope_id, depth FROM hierarchical_budget_scopes "
            "WHERE root_id = ? AND scope_id = ?",
            (scope.opaque_root, scope.scope_id),
        ).fetchone()
        if row is None:
            raise _unknown_scope()
        return row[0], int(row[1])

    def _reserve(
        self,
        scope: HierarchicalBudgetScope,
        amounts: Mapping[str, int],
    ) -> PersistentBudgetReservation:
        requested = self._amounts(amounts, "amounts")
        reservation_id = uuid4().hex
        connection = self._begin()
        try:
            chain = self._chain(connection, scope.opaque_root, scope.scope_id)
            self._ensure_counter_bounds(connection, chain, requested)
            self._check_capacity(connection, chain, requested)
            connection.execute(
                "INSERT INTO hierarchical_budget_reservations "
                "(reservation_id, root_id, scope_id, amounts_json, status) "
                "VALUES (?, ?, ?, ?, 'active')",
                (
                    reservation_id,
                    scope.opaque_root,
                    scope.scope_id,
                    _encode_amounts(requested),
                ),
            )
            for scope_id in chain:
                self._add_reserved(connection, scope_id, requested)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return PersistentBudgetReservation(scope, reservation_id, requested)

    def _open_reservation(
        self,
        scope: HierarchicalBudgetScope,
        reservation_id: str,
    ) -> PersistentBudgetReservation:
        _reservation_id(reservation_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT amounts_json FROM hierarchical_budget_reservations "
                "WHERE reservation_id = ? AND root_id = ? AND scope_id = ?",
                (reservation_id, scope.opaque_root, scope.scope_id),
            ).fetchone()
        if row is None:
            raise _unknown_reservation()
        return PersistentBudgetReservation(scope, reservation_id, _decode_amounts(row[0]))

    def _finish_reservation(
        self,
        scope: HierarchicalBudgetScope,
        reservation_id: str,
        actual: Mapping[str, int] | None,
    ) -> None:
        charged = None if actual is None else self._amounts(actual, "actual")
        connection = self._begin()
        try:
            row = connection.execute(
                "SELECT amounts_json, status FROM hierarchical_budget_reservations "
                "WHERE reservation_id = ? AND root_id = ? AND scope_id = ?",
                (reservation_id, scope.opaque_root, scope.scope_id),
            ).fetchone()
            if row is None:
                raise _unknown_reservation()
            if row[1] != "active":
                raise _inactive_reservation()
            reserved = _decode_amounts(row[0])
            chain = self._chain(connection, scope.opaque_root, scope.scope_id)
            final = charged or {}
            self._ensure_counter_bounds(connection, chain, final)
            self._check_capacity(connection, chain, final, replacing=reserved)
            for scope_id in chain:
                self._replace_reserved(connection, scope_id, reserved, final)
            connection.execute(
                "UPDATE hierarchical_budget_reservations SET status = ? "
                "WHERE reservation_id = ? AND status = 'active'",
                ("reconciled" if charged is not None else "cancelled", reservation_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _reservation_active(
        self,
        scope: HierarchicalBudgetScope,
        reservation_id: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM hierarchical_budget_reservations "
                "WHERE reservation_id = ? AND root_id = ? AND scope_id = ?",
                (reservation_id, scope.opaque_root, scope.scope_id),
            ).fetchone()
        if row is None:
            raise _unknown_reservation()
        return str(row[0]) == "active"

    def _chain(
        self,
        connection: sqlite3.Connection,
        opaque_root: str,
        leaf_scope_id: str,
    ) -> tuple[str, ...]:
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = leaf_scope_id
        while current is not None:
            if current in seen or len(chain) > self.MAX_DEPTH:
                raise _corrupt()
            seen.add(current)
            row = connection.execute(
                "SELECT parent_scope_id FROM hierarchical_budget_scopes "
                "WHERE root_id = ? AND scope_id = ?",
                (opaque_root, current),
            ).fetchone()
            if row is None:
                raise _unknown_scope()
            chain.append(current)
            current = row[0]
        if not chain or chain[-1] != opaque_root:
            raise _corrupt()
        chain.reverse()
        return tuple(chain)

    def _check_capacity(
        self,
        connection: sqlite3.Connection,
        chain: tuple[str, ...],
        amounts: Mapping[str, int],
        *,
        replacing: Mapping[str, int] | None = None,
    ) -> None:
        replaced = replacing or {}
        for scope_id in chain:
            limits = self._read_limits(connection, scope_id)
            for counter, amount in amounts.items():
                limit = limits.get(counter)
                if limit is None:
                    continue
                used, reserved = self._counter(connection, scope_id, counter)
                requested_total = used + reserved - replaced.get(counter, 0) + amount
                if requested_total > limit:
                    raise BudgetExceeded(
                        scope=scope_id,
                        counter=counter,
                        limit=limit,
                        requested_total=requested_total,
                    )

    def _ensure_counter_bounds(
        self,
        connection: sqlite3.Connection,
        chain: tuple[str, ...],
        amounts: Mapping[str, int],
    ) -> None:
        for scope_id in chain:
            rows = connection.execute(
                "SELECT counter FROM hierarchical_budget_limits WHERE scope_id = ? "
                "UNION SELECT counter FROM hierarchical_budget_counters WHERE scope_id = ?",
                (scope_id, scope_id),
            )
            names = {str(row[0]) for row in rows}
            if len(names | set(amounts)) > self.MAX_COUNTERS:
                raise HierarchicalBudgetError(
                    "budget_counter_limit",
                    "The budget scope contains too many counters.",
                    category=ErrorCategory.BUDGET,
                )

    @staticmethod
    def _add_reserved(
        connection: sqlite3.Connection,
        scope_id: str,
        amounts: Mapping[str, int],
    ) -> None:
        connection.executemany(
            "INSERT INTO hierarchical_budget_counters(scope_id, counter, reserved) "
            "VALUES (?, ?, ?) ON CONFLICT(scope_id, counter) "
            "DO UPDATE SET reserved = reserved + excluded.reserved",
            ((scope_id, counter, amount) for counter, amount in amounts.items()),
        )

    def _replace_reserved(
        self,
        connection: sqlite3.Connection,
        scope_id: str,
        reserved_amounts: Mapping[str, int],
        charged: Mapping[str, int],
    ) -> None:
        for counter in set(reserved_amounts) | set(charged):
            used, reserved = self._counter(connection, scope_id, counter)
            remaining = reserved - reserved_amounts.get(counter, 0)
            if remaining < 0:
                raise _corrupt()
            connection.execute(
                "INSERT INTO hierarchical_budget_counters "
                "(scope_id, counter, used, reserved) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope_id, counter) DO UPDATE SET "
                "used = excluded.used, reserved = excluded.reserved",
                (scope_id, counter, used + charged.get(counter, 0), remaining),
            )

    def _amounts(self, amounts: Mapping[str, int], label: str) -> dict[str, int]:
        normalized = _validate_amounts(amounts, label=label)
        if len(normalized) > self.MAX_COUNTERS:
            raise ValueError(f"at most {self.MAX_COUNTERS} counters are allowed")
        return normalized

    def _snapshot(self, connection: sqlite3.Connection, scope_id: str) -> BudgetSnapshot:
        limits = self._bounded_pairs(
            connection,
            "SELECT counter, hard_limit FROM hierarchical_budget_limits "
            "WHERE scope_id = ? ORDER BY counter LIMIT ?",
            scope_id,
        )
        rows = list(
            connection.execute(
                "SELECT counter, used, reserved FROM hierarchical_budget_counters "
                "WHERE scope_id = ? ORDER BY counter LIMIT ?",
                (scope_id, self.MAX_COUNTERS + 1),
            )
        )
        if len(rows) > self.MAX_COUNTERS:
            raise _corrupt()
        counters = {str(counter): (int(used), int(reserved)) for counter, used, reserved in rows}
        names = set(limits) | set(counters)
        used = {counter: counters.get(counter, (0, 0))[0] for counter in names}
        reserved = {counter: counters.get(counter, (0, 0))[1] for counter in names}
        remaining = {
            counter: max(0, limit - used.get(counter, 0) - reserved.get(counter, 0))
            for counter, limit in limits.items()
        }
        return BudgetSnapshot(
            scope=scope_id,
            limits=MappingProxyType(limits),
            used=MappingProxyType(used),
            reserved=MappingProxyType(reserved),
            remaining=MappingProxyType(remaining),
        )

    def _limits(self, limits: Mapping[str, int]) -> dict[str, int]:
        normalized = _validate_amounts(limits, label="limits", drop_zero=False)
        if not normalized:
            raise ValueError("at least one hard limit is required")
        if len(normalized) > self.MAX_COUNTERS:
            raise ValueError(f"at most {self.MAX_COUNTERS} counters are allowed")
        return normalized

    def _check_parent_allocation(
        self,
        connection: sqlite3.Connection,
        parent_scope_id: str,
        child_limits: Mapping[str, int],
        *,
        excluding_scope: str | None,
    ) -> None:
        parent_limits = self._read_limits(connection, parent_scope_id)
        for counter, requested in child_limits.items():
            parent_limit = parent_limits.get(counter)
            if parent_limit is None:
                continue
            allocated = self._child_allocation(
                connection,
                parent_scope_id,
                counter,
                excluding_scope=excluding_scope,
            )
            if allocated + requested > parent_limit:
                raise BudgetExceeded(
                    scope=parent_scope_id,
                    counter=counter,
                    limit=parent_limit,
                    requested_total=allocated + requested,
                )

    @staticmethod
    def _child_allocation(
        connection: sqlite3.Connection,
        parent_scope_id: str,
        counter: str,
        *,
        excluding_scope: str | None = None,
    ) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(l.hard_limit), 0) "
            "FROM hierarchical_budget_scopes s "
            "JOIN hierarchical_budget_limits l ON l.scope_id = s.scope_id "
            "WHERE s.parent_scope_id = ? AND l.counter = ? "
            "AND (? IS NULL OR s.scope_id != ?)",
            (parent_scope_id, counter, excluding_scope, excluding_scope),
        ).fetchone()
        return int(row[0])

    def _read_limits(self, connection: sqlite3.Connection, scope_id: str) -> dict[str, int]:
        return self._bounded_pairs(
            connection,
            "SELECT counter, hard_limit FROM hierarchical_budget_limits "
            "WHERE scope_id = ? ORDER BY counter LIMIT ?",
            scope_id,
        )

    def _bounded_pairs(
        self,
        connection: sqlite3.Connection,
        query: str,
        scope_id: str,
    ) -> dict[str, int]:
        rows = list(connection.execute(query, (scope_id, self.MAX_COUNTERS + 1)))
        if len(rows) > self.MAX_COUNTERS:
            raise _corrupt()
        return {str(counter): int(value) for counter, value in rows}

    @staticmethod
    def _write_limits(
        connection: sqlite3.Connection,
        scope_id: str,
        limits: Mapping[str, int],
    ) -> None:
        connection.executemany(
            "INSERT INTO hierarchical_budget_limits(scope_id, counter, hard_limit) "
            "VALUES (?, ?, ?) ON CONFLICT(scope_id, counter) "
            "DO UPDATE SET hard_limit = excluded.hard_limit",
            ((scope_id, counter, limit) for counter, limit in limits.items()),
        )

    @staticmethod
    def _counter(
        connection: sqlite3.Connection,
        scope_id: str,
        counter: str,
    ) -> tuple[int, int]:
        row = connection.execute(
            "SELECT used, reserved FROM hierarchical_budget_counters "
            "WHERE scope_id = ? AND counter = ?",
            (scope_id, counter),
        ).fetchone()
        return (0, 0) if row is None else (int(row[0]), int(row[1]))

    @staticmethod
    def _require_root(connection: sqlite3.Connection, opaque_root: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM hierarchical_budget_roots WHERE root_id = ?", (opaque_root,)
            ).fetchone()
            is None
        ):
            raise _unknown_scope()

    @staticmethod
    def _require_scope(
        connection: sqlite3.Connection,
        opaque_root: str,
        scope_id: str,
    ) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM hierarchical_budget_scopes WHERE root_id = ? AND scope_id = ?",
                (opaque_root, scope_id),
            ).fetchone()
            is None
        ):
            raise _unknown_scope()

    def _digest(self, label: bytes, *parts: bytes) -> str:
        digest = hmac.new(self._key, digestmod=hashlib.sha256)
        digest.update(label)
        for part in parts:
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
        return digest.hexdigest()

    def _begin(self) -> sqlite3.Connection:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self._timeout)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA busy_timeout={int(self._timeout * 1_000)}")
        return connection


class DurableHierarchicalBudgetProvider:
    """Resolve task ledgers below one opaque tenant/principal/session chain."""

    def __init__(
        self,
        store: SQLiteHierarchicalBudgetStore,
        *,
        tenant_scope: str,
        principal_scope: str,
        session_scope: str,
        aggregate_limits: Mapping[str, int],
        task_limits: Mapping[str, int],
    ) -> None:
        root = store.root(f"tenant:{_scope_name(tenant_scope)}", aggregate_limits)
        principal = root.create_child(
            f"principal:{_scope_name(principal_scope)}",
            aggregate_limits,
        )
        self._session = principal.create_child(
            f"session:{_scope_name(session_scope)}",
            aggregate_limits,
        )
        self._task_limits = MappingProxyType(store._limits(task_limits))

    @property
    def opaque_root(self) -> str:
        return self._session.opaque_root

    @property
    def session_scope_id(self) -> str:
        return self._session.scope_id

    def __call__(self, task_scope: str) -> HierarchicalBudgetScope:
        """Create or reopen one HMAC-addressed task budget."""

        return self._session.create_child(
            f"task:{_scope_name(task_scope)}",
            self._task_limits,
        )


class HierarchicalBudgetScope(BudgetLedger):
    """A durable budget node addressed only by opaque HMAC identifiers."""

    def __init__(
        self,
        store: SQLiteHierarchicalBudgetStore,
        opaque_root: str,
        scope_id: str,
        parent_scope_id: str | None,
        depth: int,
    ) -> None:
        self._store = store
        self.opaque_root = opaque_root
        self.scope = scope_id
        self.scope_id = scope_id
        self.parent_scope_id = parent_scope_id
        self.depth = depth
        self.parent = None

    @property
    def limits(self) -> Mapping[str, int]:
        return self.snapshot().limits

    def create_child(
        self,
        scope: str,
        limits: Mapping[str, int],
    ) -> HierarchicalBudgetScope:
        return self._store._create_child(self, scope, limits)

    def configure(
        self,
        limits: Mapping[str, int],
        *,
        expected_limits: Mapping[str, int],
    ) -> None:
        """Merge limits with a required compare-and-swap precondition."""

        self._store._configure(self, limits, expected_limits=expected_limits)

    def reserve(
        self,
        amounts: Mapping[str, int],
        *,
        reservation_id: str | None = None,
    ) -> PersistentBudgetReservation:
        if reservation_id is not None:
            raise ValueError("caller-provided reservation IDs are not supported")
        return self._store._reserve(self, amounts)

    def spend(self, amounts: Mapping[str, int]) -> None:
        reservation = self.reserve(amounts)
        reservation.reconcile(amounts)

    def reservation(self, reservation_id: str) -> PersistentBudgetReservation:
        """Reopen a durable reservation without exposing its scope name."""

        return self._store._open_reservation(self, reservation_id)

    def snapshot(self) -> BudgetSnapshot:
        with self._store._connect() as connection:
            self._store._require_scope(connection, self.opaque_root, self.scope_id)
            return self._store._snapshot(connection, self.scope_id)


class PersistentBudgetReservation(BudgetReservation):
    """A restart-safe, single-use reservation over an entire persisted chain."""

    ledger: HierarchicalBudgetScope

    def __init__(
        self,
        ledger: HierarchicalBudgetScope,
        reservation_id: str,
        amounts: Mapping[str, int],
    ) -> None:
        self.ledger = ledger
        self.reservation_id = reservation_id
        self.amounts = MappingProxyType(dict(amounts))

    @property
    def active(self) -> bool:
        return self.ledger._store._reservation_active(self.ledger, self.reservation_id)

    def reconcile(self, actual: Mapping[str, int]) -> None:
        self.ledger._store._finish_reservation(self.ledger, self.reservation_id, actual)

    def cancel(self) -> None:
        self.ledger._store._finish_reservation(self.ledger, self.reservation_id, None)

    release = cancel


def _scope_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise ValueError("scope name must be non-empty and at most 4096 characters")
    return value


def load_or_create_hmac_key(path: str | Path) -> bytes:
    """Load a private 256-bit key, creating it with an atomic no-replace link."""

    key_path = Path(path).resolve()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _read_hmac_key(key_path)
    except FileNotFoundError:
        pass

    candidate = os.urandom(32)
    temporary = key_path.with_name(f".{key_path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
        with suppress(FileExistsError):
            os.link(temporary, key_path)
    finally:
        temporary.unlink(missing_ok=True)
    return _read_hmac_key(key_path)


def _read_hmac_key(path: Path) -> bytes:
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise HierarchicalBudgetError(
            "budget_hmac_key_permissions",
            "The local budget key file permissions are unsafe.",
            category=ErrorCategory.POLICY,
        )
    value = path.read_bytes()
    if len(value) != 32:
        raise HierarchicalBudgetError(
            "budget_hmac_key_invalid",
            "The local budget key file is invalid.",
            category=ErrorCategory.INTERNAL,
        )
    return value


def _opaque_id(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise _unknown_scope()


def _reservation_id(value: str) -> None:
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise _unknown_reservation()


def _encode_amounts(amounts: Mapping[str, int]) -> str:
    return json.dumps(dict(amounts), sort_keys=True, separators=(",", ":"))


def _decode_amounts(value: str) -> dict[str, int]:
    try:
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise TypeError
        return _validate_amounts(raw, label="stored amounts")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _corrupt() from exc


def _unknown_scope() -> HierarchicalBudgetError:
    return HierarchicalBudgetError(
        "unknown_budget_scope",
        "The opaque budget scope is unknown or belongs to another root.",
        category=ErrorCategory.REFERENCE,
    )


def _unknown_reservation() -> HierarchicalBudgetError:
    return HierarchicalBudgetError(
        "unknown_budget_reservation",
        "The opaque budget reservation is unknown for this scope.",
        category=ErrorCategory.REFERENCE,
    )


def _inactive_reservation() -> HierarchicalBudgetError:
    return HierarchicalBudgetError(
        "budget_reservation_inactive",
        "The budget reservation is no longer active.",
        category=ErrorCategory.BUDGET,
    )


def _conflict(code: str, message: str) -> HierarchicalBudgetError:
    return HierarchicalBudgetError(code, message, category=ErrorCategory.CONFLICT)


def _corrupt() -> HierarchicalBudgetError:
    return HierarchicalBudgetError(
        "budget_state_invalid",
        "The persisted budget state is invalid.",
        category=ErrorCategory.INTERNAL,
    )
