"""Atomic SQLite persistence for privacy-safe reasoning task state."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TypeVar, cast

from .models import JsonValue, ReasoningTier
from .tenancy import SqliteScopedState, TenantScope

_Result = TypeVar("_Result")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS reasoning_tasks (
    task_id TEXT PRIMARY KEY,
    tier TEXT,
    escalations_used INTEGER NOT NULL,
    recommendation_count INTEGER NOT NULL,
    last_recommendation TEXT
);
CREATE TABLE IF NOT EXISTS reasoning_attempts (
    task_id TEXT NOT NULL,
    attempt_digest TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL,
    PRIMARY KEY (task_id, attempt_digest, evidence_digest),
    FOREIGN KEY (task_id) REFERENCES reasoning_tasks(task_id) ON DELETE CASCADE
);
"""


@dataclass(frozen=True, slots=True)
class StoredReasoningState:
    """Persisted counters and digests; never raw attempts or evidence."""

    task_id: str
    tier: ReasoningTier | None = None
    escalations_used: int = 0
    recommendation_count: int = 0
    attempt_counts: Mapping[tuple[str, str], int] = field(default_factory=dict)
    last_recommendation: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempt_counts", MappingProxyType(dict(self.attempt_counts)))
        if self.last_recommendation is not None:
            object.__setattr__(
                self,
                "last_recommendation",
                MappingProxyType(dict(self.last_recommendation)),
            )


class ReasoningStateStore(Protocol):
    def read(self, task_id: str) -> StoredReasoningState: ...

    def transact(
        self,
        task_id: str,
        update: Callable[[StoredReasoningState], tuple[StoredReasoningState, _Result]],
    ) -> _Result: ...

    def reset(self, task_id: str) -> None: ...


class SQLiteReasoningStore:
    """Serialize per-task reasoning mutations with ``BEGIN IMMEDIATE``."""

    def __init__(self, path: Path | str, *, timeout_seconds: float = 5.0) -> None:
        self.path = Path(path).resolve()
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def read(self, task_id: str) -> StoredReasoningState:
        self._validate_task_id(task_id)
        with self._connect() as connection:
            return self._read(connection, task_id)

    def transact(
        self,
        task_id: str,
        update: Callable[[StoredReasoningState], tuple[StoredReasoningState, _Result]],
    ) -> _Result:
        """Atomically read, transform, and replace one task's state."""

        self._validate_task_id(task_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._read(connection, task_id)
            replacement, result = update(current)
            if replacement.task_id != task_id:
                raise ValueError("replacement task_id must match transaction task_id")
            self._write(connection, replacement)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reset(self, task_id: str) -> None:
        self._validate_task_id(task_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM reasoning_tasks WHERE task_id = ?", (task_id,))
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1_000)}")
        return connection

    @staticmethod
    def _read(connection: sqlite3.Connection, task_id: str) -> StoredReasoningState:
        row = connection.execute(
            "SELECT tier, escalations_used, recommendation_count, last_recommendation "
            "FROM reasoning_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return StoredReasoningState(task_id=task_id)
        attempts = {
            (attempt_digest, evidence_digest): occurrence_count
            for attempt_digest, evidence_digest, occurrence_count in connection.execute(
                "SELECT attempt_digest, evidence_digest, occurrence_count "
                "FROM reasoning_attempts WHERE task_id = ?",
                (task_id,),
            )
        }
        raw_last = json.loads(row[3]) if row[3] is not None else None
        last = cast(dict[str, JsonValue] | None, raw_last)
        return StoredReasoningState(
            task_id=task_id,
            tier=ReasoningTier(row[0]) if row[0] is not None else None,
            escalations_used=int(row[1]),
            recommendation_count=int(row[2]),
            attempt_counts=attempts,
            last_recommendation=last,
        )

    @staticmethod
    def _write(connection: sqlite3.Connection, state: StoredReasoningState) -> None:
        serialized = (
            json.dumps(
                dict(state.last_recommendation),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if state.last_recommendation is not None
            else None
        )
        connection.execute(
            "INSERT INTO reasoning_tasks "
            "(task_id, tier, escalations_used, recommendation_count, last_recommendation) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET tier=excluded.tier, "
            "escalations_used=excluded.escalations_used, "
            "recommendation_count=excluded.recommendation_count, "
            "last_recommendation=excluded.last_recommendation",
            (
                state.task_id,
                state.tier.value if state.tier is not None else None,
                state.escalations_used,
                state.recommendation_count,
                serialized,
            ),
        )
        connection.execute("DELETE FROM reasoning_attempts WHERE task_id = ?", (state.task_id,))
        connection.executemany(
            "INSERT INTO reasoning_attempts "
            "(task_id, attempt_digest, evidence_digest, occurrence_count) VALUES (?, ?, ?, ?)",
            (
                (state.task_id, attempt, evidence, count)
                for (attempt, evidence), count in state.attempt_counts.items()
            ),
        )

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not task_id:
            raise ValueError("task_id must be non-empty")


class ScopedReasoningStore:
    """Reasoning state backed only by the shared opaque TenantScope repository."""

    _NAMESPACE = "reasoning"
    _KEY = "state"

    def __init__(
        self,
        state: SqliteScopedState,
        *,
        scope_provider: Callable[[str], TenantScope],
    ) -> None:
        self._state = state
        self._scope_provider = scope_provider

    def read(self, task_id: str) -> StoredReasoningState:
        SQLiteReasoningStore._validate_task_id(task_id)
        value = self._state.get(
            self._scope_provider(task_id), self._KEY, namespace=self._NAMESPACE
        )
        return _decode_scoped_state(task_id, value)

    def transact(
        self,
        task_id: str,
        update: Callable[[StoredReasoningState], tuple[StoredReasoningState, _Result]],
    ) -> _Result:
        SQLiteReasoningStore._validate_task_id(task_id)

        def transform(value: JsonValue | None) -> tuple[JsonValue, _Result]:
            current = _decode_scoped_state(task_id, value)
            replacement, result = update(current)
            if replacement.task_id != task_id:
                raise ValueError("replacement task_id must match transaction task_id")
            return _encode_scoped_state(replacement), result

        return self._state.transact_entry(
            self._scope_provider(task_id),
            self._KEY,
            transform,
            namespace=self._NAMESPACE,
        )

    def reset(self, task_id: str) -> None:
        SQLiteReasoningStore._validate_task_id(task_id)
        self._state.delete(
            self._scope_provider(task_id), self._KEY, namespace=self._NAMESPACE
        )


def _encode_scoped_state(state: StoredReasoningState) -> dict[str, JsonValue]:
    attempts: list[JsonValue] = [
        [attempt, evidence, count]
        for (attempt, evidence), count in sorted(state.attempt_counts.items())
    ]
    return {
        "attempts": attempts,
        "escalations_used": state.escalations_used,
        "last_recommendation": (
            None
            if state.last_recommendation is None
            else dict(state.last_recommendation)
        ),
        "recommendation_count": state.recommendation_count,
        "tier": state.tier.value if state.tier is not None else None,
    }


def _decode_scoped_state(task_id: str, value: JsonValue | None) -> StoredReasoningState:
    if value is None:
        return StoredReasoningState(task_id)
    if not isinstance(value, dict):
        raise ValueError("scoped reasoning state is invalid")
    try:
        raw_attempts = value["attempts"]
        raw_tier = value["tier"]
        raw_last = value["last_recommendation"]
        if not isinstance(raw_attempts, list):
            raise TypeError
        if raw_tier is not None and not isinstance(raw_tier, str):
            raise TypeError
        attempts: dict[tuple[str, str], int] = {}
        for item in raw_attempts:
            if (
                not isinstance(item, list)
                or len(item) != 3
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
                or not isinstance(item[2], int)
                or isinstance(item[2], bool)
                or item[2] < 0
            ):
                raise TypeError
            attempts[(item[0], item[1])] = item[2]
        if raw_last is not None and not isinstance(raw_last, dict):
            raise TypeError
        escalations = value["escalations_used"]
        count = value["recommendation_count"]
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in (escalations, count)
        ):
            raise TypeError
        return StoredReasoningState(
            task_id,
            tier=None if raw_tier is None else ReasoningTier(raw_tier),
            escalations_used=cast(int, escalations),
            recommendation_count=cast(int, count),
            attempt_counts=attempts,
            last_recommendation=raw_last,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("scoped reasoning state is invalid") from error
