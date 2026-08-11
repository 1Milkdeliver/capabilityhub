"""Optional durable idempotency records with conservative crash recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Protocol, TypeAlias, cast

from capabilityhub.metering import canonical_json
from capabilityhub.models import ExecutionResult, JsonValue

IdempotencySlot: TypeAlias = tuple[str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    arguments_digest: str
    status: str
    result: ExecutionResult | None = None


class IdempotencyStore(Protocol):
    def reserve(self, slot: IdempotencySlot, arguments_digest: str) -> IdempotencyRecord | None: ...

    def complete(self, slot: IdempotencySlot, result: ExecutionResult) -> None: ...

    def uncertain(self, slot: IdempotencySlot) -> None: ...


class SqliteIdempotencyStore:
    """Multi-process SQLite store; provider output persistence is opt-in."""

    def __init__(
        self,
        path: Path,
        *,
        persist_results: bool = False,
        recover_abandoned: bool = False,
    ) -> None:
        self._path = path.resolve()
        self._persist_results = persist_results
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    slot_digest TEXT PRIMARY KEY,
                    arguments_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            # Recovery is an explicit startup action. Running it for every store
            # instance could misclassify work that another process is still doing.
            if recover_abandoned:
                connection.execute(
                    "UPDATE idempotency_records SET status = 'uncertain', updated_at = ? "
                    "WHERE status = 'in_progress'",
                    (time(),),
                )

    @property
    def path(self) -> Path:
        return self._path

    def reserve(self, slot: IdempotencySlot, arguments_digest: str) -> IdempotencyRecord | None:
        key = _slot_digest(slot)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT arguments_digest, status, result_json "
                "FROM idempotency_records WHERE slot_digest = ?",
                (key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO idempotency_records "
                    "(slot_digest, arguments_digest, status, result_json, updated_at) "
                    "VALUES (?, ?, 'in_progress', NULL, ?)",
                    (key, arguments_digest, time()),
                )
                return None
            return IdempotencyRecord(row[0], row[1], _decode_result(row[2]))

    def complete(self, slot: IdempotencySlot, result: ExecutionResult) -> None:
        encoded = _encode_result(result) if self._persist_results else None
        with self._connect() as connection:
            connection.execute(
                "UPDATE idempotency_records SET status = 'complete', result_json = ?, "
                "updated_at = ? WHERE slot_digest = ? AND status = 'in_progress'",
                (encoded, time(), _slot_digest(slot)),
            )

    def uncertain(self, slot: IdempotencySlot) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE idempotency_records SET status = 'uncertain', updated_at = ? "
                "WHERE slot_digest = ? AND status = 'in_progress'",
                (time(), _slot_digest(slot)),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


def _slot_digest(slot: IdempotencySlot) -> str:
    return hashlib.sha256(canonical_json(list(slot)).encode()).hexdigest()


def _encode_result(result: ExecutionResult) -> str:
    return canonical_json(
        {
            "audit_id": result.audit_id,
            "capability_revision": result.capability_revision,
            "operation": result.operation,
            "output": result.output,
            "portable_tokens": result.portable_tokens,
            "provider": result.provider,
        }
    )


def _decode_result(raw: str | None) -> ExecutionResult | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return ExecutionResult(
            capability_revision=data["capability_revision"],
            operation=data["operation"],
            output=cast(JsonValue, data["output"]),
            provider=data["provider"],
            portable_tokens=data["portable_tokens"],
            audit_id=data["audit_id"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
