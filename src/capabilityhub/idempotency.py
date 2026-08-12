"""Optional durable idempotency records with conservative crash recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep, time
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

    def wait(
        self, slot: IdempotencySlot, arguments_digest: str, timeout_seconds: float
    ) -> IdempotencyRecord: ...


class SqliteIdempotencyStore:
    """Multi-process SQLite store; provider output persistence is opt-in."""

    def __init__(
        self,
        path: Path,
        *,
        persist_results: bool = False,
        recover_abandoned: bool = False,
        result_ttl_seconds: int = 300,
        max_result_bytes: int = 1_000_000,
        clock: Callable[[], float] = time,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if not 1 <= result_ttl_seconds <= 86_400:
            raise ValueError("result_ttl_seconds must be from 1 to 86400")
        if not 1_024 <= max_result_bytes <= 4_000_000:
            raise ValueError("max_result_bytes must be from 1024 to 4000000")
        self._path = path.resolve()
        self._persist_results = persist_results
        self._result_ttl_seconds = result_ttl_seconds
        self._max_result_bytes = max_result_bytes
        self._clock = clock
        self._monotonic = monotonic_clock
        self._sleep = sleeper
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
                    (self._clock(),),
                )

    @property
    def path(self) -> Path:
        return self._path

    def reserve(self, slot: IdempotencySlot, arguments_digest: str) -> IdempotencyRecord | None:
        key = _slot_digest(slot)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT arguments_digest, status, result_json, updated_at "
                "FROM idempotency_records WHERE slot_digest = ?",
                (key,),
            ).fetchone()
            if (
                row is not None
                and row[1] == "complete"
                and self._clock() - float(row[3]) >= self._result_ttl_seconds
            ):
                connection.execute(
                    "DELETE FROM idempotency_records WHERE slot_digest = ?", (key,)
                )
                row = None
            if row is None:
                connection.execute(
                    "INSERT INTO idempotency_records "
                    "(slot_digest, arguments_digest, status, result_json, updated_at) "
                    "VALUES (?, ?, 'in_progress', NULL, ?)",
                    (key, arguments_digest, self._clock()),
                )
                return None
            return IdempotencyRecord(row[0], row[1], _decode_result(row[2]))

    def complete(self, slot: IdempotencySlot, result: ExecutionResult) -> None:
        encoded = _encode_result(result) if self._persist_results else None
        if encoded is not None and len(encoded.encode("utf-8")) > self._max_result_bytes:
            encoded = None
        with self._connect() as connection:
            connection.execute(
                "UPDATE idempotency_records SET status = 'complete', result_json = ?, "
                "updated_at = ? WHERE slot_digest = ? AND status = 'in_progress'",
                (encoded, self._clock(), _slot_digest(slot)),
            )

    def uncertain(self, slot: IdempotencySlot) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE idempotency_records SET status = 'uncertain', updated_at = ? "
                "WHERE slot_digest = ? AND status = 'in_progress'",
                (self._clock(), _slot_digest(slot)),
            )

    def wait(
        self, slot: IdempotencySlot, arguments_digest: str, timeout_seconds: float
    ) -> IdempotencyRecord:
        deadline = self._monotonic() + max(0.0, timeout_seconds)
        while True:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT arguments_digest, status, result_json "
                    "FROM idempotency_records WHERE slot_digest = ?",
                    (_slot_digest(slot),),
                ).fetchone()
            if row is None or row[0] != arguments_digest:
                return IdempotencyRecord("", "conflict")
            record = IdempotencyRecord(row[0], row[1], _decode_result(row[2]))
            if record.status != "in_progress":
                return record
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return record
            self._sleep(min(0.01, remaining))

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
