"""SQLite state for staged capability revision activation and rollback."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import CapabilityHubError, ErrorCategory

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_updates (
    coordinate TEXT PRIMARY KEY,
    active_revision TEXT,
    previous_revision TEXT,
    staged_revision TEXT,
    health_status TEXT
);
CREATE TABLE IF NOT EXISTS revision_pins (
    pin_id TEXT PRIMARY KEY,
    coordinate TEXT NOT NULL,
    revision TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS revision_pins_coordinate
ON revision_pins(coordinate, revision, pin_id);
"""


@dataclass(frozen=True, slots=True)
class UpdateState:
    coordinate: str
    active_revision: str | None = None
    previous_revision: str | None = None
    staged_revision: str | None = None
    health_status: str | None = None


@dataclass(frozen=True, slots=True)
class RevisionPin:
    pin_id: str
    coordinate: str
    revision: str


class SQLiteUpdateStore:
    """Maintain active pointers with process-safe SQLite transactions."""

    MAX_STATE_ROWS = 1_000

    def __init__(self, path: Path | str, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.path = Path(path).resolve()
        self.timeout_seconds = timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def state(self, coordinate: str) -> UpdateState:
        self._require("coordinate", coordinate)
        with self._connect() as connection:
            return self._read_state(connection, coordinate)

    def states(self, *, limit: int = MAX_STATE_ROWS) -> tuple[UpdateState, ...]:
        """List update rows in stable coordinate order with a hard upper bound."""

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.MAX_STATE_ROWS
        ):
            raise ValueError(f"states limit must be from 1 to {self.MAX_STATE_ROWS}")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT coordinate, active_revision, previous_revision, "
                "staged_revision, health_status FROM capability_updates "
                "ORDER BY coordinate LIMIT ?",
                (limit,),
            )
            return tuple(UpdateState(*row) for row in rows)

    def active_pointers(self) -> dict[str, str]:
        with self._connect() as connection:
            return {
                coordinate: revision
                for coordinate, revision in connection.execute(
                    "SELECT coordinate, active_revision FROM capability_updates "
                    "WHERE active_revision IS NOT NULL ORDER BY coordinate"
                )
            }

    def bootstrap_active(self, coordinate: str, revision: str) -> UpdateState:
        """Record the catalog's current pointer once without overwriting managed state."""

        self._require("coordinate", coordinate)
        self._require("revision", revision)
        connection = self._begin()
        try:
            current = self._read_state(connection, coordinate)
            if current.active_revision is None and current.staged_revision is None:
                current = UpdateState(coordinate=coordinate, active_revision=revision)
                self._write_state(connection, current)
            elif current.active_revision != revision:
                raise _conflict(
                    "active_revision_changed",
                    "Managed active revision differs from the catalog bootstrap revision.",
                    coordinate=coordinate,
                    expected=revision,
                    actual=current.active_revision,
                )
            connection.commit()
            return current
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def stage(
        self,
        coordinate: str,
        revision: str,
        *,
        expected_active_revision: str | None,
    ) -> UpdateState:
        self._require("coordinate", coordinate)
        self._require("revision", revision)
        connection = self._begin()
        try:
            current = self._read_state(connection, coordinate)
            self._expect_active(current, expected_active_revision)
            if current.staged_revision not in (None, revision):
                raise _conflict(
                    "stage_in_progress",
                    "Another revision is already staged for this capability.",
                    coordinate=coordinate,
                    staged_revision=current.staged_revision,
                )
            replacement = UpdateState(
                coordinate=coordinate,
                active_revision=current.active_revision,
                previous_revision=current.previous_revision,
                staged_revision=revision,
                health_status=(
                    current.health_status if current.staged_revision == revision else "pending"
                ),
            )
            self._write_state(connection, replacement)
            connection.commit()
            return replacement
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_health(self, coordinate: str, revision: str, *, passed: bool) -> UpdateState:
        self._require("coordinate", coordinate)
        self._require("revision", revision)
        connection = self._begin()
        try:
            current = self._read_state(connection, coordinate)
            if current.staged_revision != revision:
                raise _conflict(
                    "staged_revision_changed",
                    "The staged revision changed before its health result was recorded.",
                    coordinate=coordinate,
                )
            replacement = UpdateState(
                coordinate=coordinate,
                active_revision=current.active_revision,
                previous_revision=current.previous_revision,
                staged_revision=revision,
                health_status="passed" if passed else "failed",
            )
            self._write_state(connection, replacement)
            connection.commit()
            return replacement
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def activate(
        self,
        coordinate: str,
        revision: str,
        *,
        expected_active_revision: str | None,
        validate: Callable[[Mapping[str, str]], None],
    ) -> UpdateState:
        connection = self._begin()
        try:
            current = self._read_state(connection, coordinate)
            self._expect_active(current, expected_active_revision)
            if current.staged_revision != revision or current.health_status != "passed":
                raise _conflict(
                    "staged_revision_not_healthy",
                    "Only the current health-passed staged revision can be activated.",
                    coordinate=coordinate,
                )
            pointers = self._active_pointers(connection)
            pointers[coordinate] = revision
            validate(pointers)
            replacement = UpdateState(
                coordinate=coordinate,
                active_revision=revision,
                previous_revision=current.active_revision,
            )
            self._write_state(connection, replacement)
            connection.commit()
            return replacement
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def rollback(
        self,
        coordinate: str,
        *,
        expected_active_revision: str,
        validate: Callable[[Mapping[str, str]], None],
    ) -> UpdateState:
        connection = self._begin()
        try:
            current = self._read_state(connection, coordinate)
            self._expect_active(current, expected_active_revision)
            if current.previous_revision is None:
                raise _conflict(
                    "no_rollback_revision",
                    "No previous revision is available for rollback.",
                    coordinate=coordinate,
                )
            pointers = self._active_pointers(connection)
            pointers[coordinate] = current.previous_revision
            validate(pointers)
            replacement = UpdateState(
                coordinate=coordinate,
                active_revision=current.previous_revision,
                previous_revision=current.active_revision,
            )
            self._write_state(connection, replacement)
            connection.commit()
            return replacement
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def pin_active(self, coordinate: str, pin_id: str) -> RevisionPin:
        self._require("coordinate", coordinate)
        self._require("pin_id", pin_id)
        connection = self._begin()
        try:
            current = self._read_state(connection, coordinate)
            if current.active_revision is None:
                raise _conflict(
                    "inactive_capability",
                    "Capability has no active revision to pin.",
                    coordinate=coordinate,
                )
            existing = connection.execute(
                "SELECT coordinate, revision FROM revision_pins WHERE pin_id = ?", (pin_id,)
            ).fetchone()
            if existing is not None and existing != (coordinate, current.active_revision):
                raise _conflict("pin_id_conflict", "Pin identifier is already in use.")
            connection.execute(
                "INSERT OR IGNORE INTO revision_pins"
                "(pin_id, coordinate, revision) VALUES (?, ?, ?)",
                (pin_id, coordinate, current.active_revision),
            )
            connection.commit()
            return RevisionPin(pin_id, coordinate, current.active_revision)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def pins(self, coordinate: str | None = None) -> tuple[RevisionPin, ...]:
        with self._connect() as connection:
            if coordinate is None:
                rows = connection.execute(
                    "SELECT pin_id, coordinate, revision FROM revision_pins "
                    "ORDER BY coordinate, revision, pin_id"
                )
            else:
                self._require("coordinate", coordinate)
                rows = connection.execute(
                    "SELECT pin_id, coordinate, revision FROM revision_pins "
                    "WHERE coordinate = ? ORDER BY revision, pin_id",
                    (coordinate,),
                )
            return tuple(RevisionPin(*row) for row in rows)

    def release_pin(self, pin_id: str) -> bool:
        self._require("pin_id", pin_id)
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM revision_pins WHERE pin_id = ?", (pin_id,))
            connection.commit()
            return cursor.rowcount == 1

    def _begin(self) -> sqlite3.Connection:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1_000)}")
        return connection

    @staticmethod
    def _read_state(connection: sqlite3.Connection, coordinate: str) -> UpdateState:
        row = connection.execute(
            "SELECT active_revision, previous_revision, staged_revision, health_status "
            "FROM capability_updates WHERE coordinate = ?",
            (coordinate,),
        ).fetchone()
        return UpdateState(coordinate) if row is None else UpdateState(coordinate, *row)

    @staticmethod
    def _write_state(connection: sqlite3.Connection, state: UpdateState) -> None:
        connection.execute(
            "INSERT INTO capability_updates "
            "(coordinate, active_revision, previous_revision, staged_revision, health_status) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(coordinate) DO UPDATE SET "
            "active_revision=excluded.active_revision, "
            "previous_revision=excluded.previous_revision, "
            "staged_revision=excluded.staged_revision, health_status=excluded.health_status",
            (
                state.coordinate,
                state.active_revision,
                state.previous_revision,
                state.staged_revision,
                state.health_status,
            ),
        )

    @staticmethod
    def _active_pointers(connection: sqlite3.Connection) -> dict[str, str]:
        return {
            coordinate: revision
            for coordinate, revision in connection.execute(
                "SELECT coordinate, active_revision FROM capability_updates "
                "WHERE active_revision IS NOT NULL"
            )
        }

    @staticmethod
    def _expect_active(state: UpdateState, expected: str | None) -> None:
        if state.active_revision != expected:
            raise _conflict(
                "active_revision_changed",
                "Active revision changed; retry from fresh state.",
                coordinate=state.coordinate,
                expected=expected,
                actual=state.active_revision,
            )

    @staticmethod
    def _require(label: str, value: str) -> None:
        if not value:
            raise ValueError(f"{label} must be non-empty")


def _conflict(code: str, message: str, **details: object) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.CONFLICT,
        safe_message=message,
        retryable=False,
        details=details,
    )
