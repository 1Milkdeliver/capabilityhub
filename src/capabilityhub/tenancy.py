"""Opaque tenant-scoped local state primitives."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import TypeVar, cast
from uuid import uuid4

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import canonical_json
from capabilityhub.models import JsonValue

_SCOPE_DOMAIN = b"capabilityhub-tenant-scope-v1\0"
_KEY_DOMAIN = b"capabilityhub-scoped-key-v1\0"
_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class TenantScope:
    """Canonical in-memory binding for one tenant task session."""

    tenant: str
    principal: str
    session: str
    task: str

    def __post_init__(self) -> None:
        for name in ("tenant", "principal", "session", "task"):
            object.__setattr__(self, name, _normalize_identifier(getattr(self, name), name))

    def digest(self, scope_key: bytes) -> str:
        key = _validate_scope_key(scope_key)
        payload = canonical_json(
            {
                "principal": self.principal,
                "session": self.session,
                "task": self.task,
                "tenant": self.tenant,
            }
        ).encode("utf-8")
        return hmac.new(key, _SCOPE_DOMAIN + payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class ScopedEntry:
    key_digest: str
    value: JsonValue
    updated_at: float
    expires_at: float | None


@dataclass(frozen=True, slots=True)
class ScopedEvent:
    event_id: str
    sequence: int
    value: JsonValue
    created_at: float
    expires_at: float | None


@dataclass(frozen=True, slots=True)
class CleanupResult:
    entries_deleted: int
    events_deleted: int


class TenantStateError(CapabilityHubError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(
            code=code,
            category=ErrorCategory.INTERNAL,
            safe_message=message,
            retryable=False,
        )


class SqliteScopedState:
    """Generic KV, cache, and event state partitioned by opaque scope digest."""

    def __init__(self, path: str | Path, *, scope_key: bytes, timeout_seconds: float = 5) -> None:
        self._path = Path(path).resolve()
        self._scope_key = _validate_scope_key(scope_key)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self._timeout_seconds = timeout_seconds
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS scoped_entries (
                        scope_digest TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        key_digest TEXT NOT NULL,
                        value_json TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL,
                        PRIMARY KEY (scope_digest, namespace, key_digest)
                    );
                    CREATE INDEX IF NOT EXISTS scoped_entries_expiry
                        ON scoped_entries(scope_digest, expires_at);
                    CREATE TABLE IF NOT EXISTS scoped_events (
                        scope_digest TEXT NOT NULL,
                        stream TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        event_id TEXT NOT NULL,
                        value_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL,
                        PRIMARY KEY (scope_digest, stream, sequence),
                        UNIQUE (scope_digest, event_id)
                    );
                    CREATE INDEX IF NOT EXISTS scoped_events_expiry
                        ON scoped_events(scope_digest, expires_at);
                    """
                )
        except sqlite3.Error as error:
            raise _store_error("tenant_state_open_failed") from error

    @property
    def path(self) -> Path:
        return self._path

    def set(
        self,
        scope: TenantScope,
        key: str,
        value: JsonValue,
        *,
        namespace: str = "default",
        now: float | None = None,
    ) -> None:
        self._set(scope, key, value, namespace=namespace, expires_at=None, now=now)

    def set_cache(
        self,
        scope: TenantScope,
        key: str,
        value: JsonValue,
        *,
        ttl_seconds: float,
        namespace: str = "default",
        now: float | None = None,
    ) -> None:
        current = _timestamp(now)
        expires_at = current + _ttl(ttl_seconds)
        self._set(
            scope,
            key,
            value,
            namespace=namespace,
            expires_at=expires_at,
            now=current,
        )

    def get(
        self,
        scope: TenantScope,
        key: str,
        *,
        namespace: str = "default",
        now: float | None = None,
    ) -> JsonValue | None:
        digest = self._scope_digest(scope)
        selected_namespace = _name(namespace, "namespace")
        key_digest = self._key_digest(digest, selected_namespace, key)
        current = _timestamp(now)
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT value_json, expires_at FROM scoped_entries "
                    "WHERE scope_digest = ? AND namespace = ? AND key_digest = ?",
                    (digest, selected_namespace, key_digest),
                ).fetchone()
        except sqlite3.Error as error:
            raise _store_error("tenant_state_read_failed") from error
        if row is None or (row[1] is not None and _stored_time(row[1]) <= current):
            return None
        return _decode(row[0])

    get_cache = get

    def transact_entry(
        self,
        scope: TenantScope,
        key: str,
        update: Callable[[JsonValue | None], tuple[JsonValue | None, _Result]],
        *,
        namespace: str = "default",
        now: float | None = None,
    ) -> _Result:
        """Atomically transform one opaque scoped entry without exposing its scope."""

        digest = self._scope_digest(scope)
        selected_namespace = _name(namespace, "namespace")
        key_digest = self._key_digest(digest, selected_namespace, key)
        current = _timestamp(now)
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT value_json, expires_at FROM scoped_entries "
                    "WHERE scope_digest = ? AND namespace = ? AND key_digest = ?",
                    (digest, selected_namespace, key_digest),
                ).fetchone()
                value = (
                    None
                    if row is None
                    or (row[1] is not None and _stored_time(row[1]) <= current)
                    else _decode(row[0])
                )
                replacement, result = update(value)
                if replacement is None:
                    connection.execute(
                        "DELETE FROM scoped_entries WHERE scope_digest = ? "
                        "AND namespace = ? AND key_digest = ?",
                        (digest, selected_namespace, key_digest),
                    )
                else:
                    connection.execute(
                        "INSERT INTO scoped_entries "
                        "(scope_digest, namespace, key_digest, value_json, updated_at, "
                        "expires_at) VALUES (?, ?, ?, ?, ?, NULL) "
                        "ON CONFLICT(scope_digest, namespace, key_digest) DO UPDATE SET "
                        "value_json=excluded.value_json, updated_at=excluded.updated_at, "
                        "expires_at=NULL",
                        (
                            digest,
                            selected_namespace,
                            key_digest,
                            _encode(replacement),
                            current,
                        ),
                    )
                return result
        except TenantStateError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise _store_error("tenant_state_write_failed") from error

    def delete(self, scope: TenantScope, key: str, *, namespace: str = "default") -> bool:
        digest = self._scope_digest(scope)
        selected_namespace = _name(namespace, "namespace")
        key_digest = self._key_digest(digest, selected_namespace, key)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM scoped_entries "
                    "WHERE scope_digest = ? AND namespace = ? AND key_digest = ?",
                    (digest, selected_namespace, key_digest),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as error:
            raise _store_error("tenant_state_write_failed") from error

    def list_entries(
        self,
        scope: TenantScope,
        *,
        namespace: str = "default",
        limit: int = 100,
        now: float | None = None,
    ) -> tuple[ScopedEntry, ...]:
        digest = self._scope_digest(scope)
        selected_namespace = _name(namespace, "namespace")
        selected_limit = _limit(limit)
        current = _timestamp(now)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT key_digest, value_json, updated_at, expires_at "
                    "FROM scoped_entries WHERE scope_digest = ? AND namespace = ? "
                    "AND (expires_at IS NULL OR expires_at > ?) "
                    "ORDER BY updated_at DESC, key_digest LIMIT ?",
                    (digest, selected_namespace, current, selected_limit),
                ).fetchall()
        except sqlite3.Error as error:
            raise _store_error("tenant_state_read_failed") from error
        return tuple(
            ScopedEntry(
                key_digest=str(row[0]),
                value=_decode(row[1]),
                updated_at=_stored_time(row[2]),
                expires_at=_stored_time(row[3]) if row[3] is not None else None,
            )
            for row in rows
        )

    def append_event(
        self,
        scope: TenantScope,
        value: JsonValue,
        *,
        stream: str = "default",
        ttl_seconds: float | None = None,
        now: float | None = None,
    ) -> ScopedEvent:
        digest = self._scope_digest(scope)
        selected_stream = _name(stream, "stream")
        current = _timestamp(now)
        expires_at = current + _ttl(ttl_seconds) if ttl_seconds is not None else None
        serialized = _encode(value)
        event_id = hmac.new(
            self._scope_key,
            _KEY_DOMAIN + digest.encode("ascii") + uuid4().bytes,
            hashlib.sha256,
        ).hexdigest()
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM scoped_events "
                    "WHERE scope_digest = ? AND stream = ?",
                    (digest, selected_stream),
                ).fetchone()
                sequence = int(row[0])
                connection.execute(
                    "INSERT INTO scoped_events "
                    "(scope_digest, stream, sequence, event_id, value_json, "
                    "created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        digest,
                        selected_stream,
                        sequence,
                        event_id,
                        serialized,
                        current,
                        expires_at,
                    ),
                )
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise _store_error("tenant_state_write_failed") from error
        return ScopedEvent(event_id, sequence, value, current, expires_at)

    def list_events(
        self,
        scope: TenantScope,
        *,
        stream: str = "default",
        after_sequence: int = 0,
        limit: int = 100,
        now: float | None = None,
    ) -> tuple[ScopedEvent, ...]:
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")
        digest = self._scope_digest(scope)
        selected_stream = _name(stream, "stream")
        current = _timestamp(now)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT event_id, sequence, value_json, created_at, expires_at "
                    "FROM scoped_events WHERE scope_digest = ? AND stream = ? "
                    "AND sequence > ? AND (expires_at IS NULL OR expires_at > ?) "
                    "ORDER BY sequence LIMIT ?",
                    (digest, selected_stream, after_sequence, current, _limit(limit)),
                ).fetchall()
        except sqlite3.Error as error:
            raise _store_error("tenant_state_read_failed") from error
        return tuple(
            ScopedEvent(
                event_id=str(row[0]),
                sequence=int(row[1]),
                value=_decode(row[2]),
                created_at=_stored_time(row[3]),
                expires_at=_stored_time(row[4]) if row[4] is not None else None,
            )
            for row in rows
        )

    def cleanup_expired(
        self,
        scope: TenantScope,
        *,
        now: float | None = None,
        limit: int = 1_000,
    ) -> CleanupResult:
        """Delete at most ``limit`` expired rows of each kind in exactly one scope."""

        digest = self._scope_digest(scope)
        current = _timestamp(now)
        selected_limit = _limit(limit, maximum=10_000)
        try:
            with self._transaction() as connection:
                entries = connection.execute(
                    "DELETE FROM scoped_entries WHERE rowid IN ("
                    "SELECT rowid FROM scoped_entries WHERE scope_digest = ? "
                    "AND expires_at IS NOT NULL AND expires_at <= ? LIMIT ?)",
                    (digest, current, selected_limit),
                ).rowcount
                events = connection.execute(
                    "DELETE FROM scoped_events WHERE rowid IN ("
                    "SELECT rowid FROM scoped_events WHERE scope_digest = ? "
                    "AND expires_at IS NOT NULL AND expires_at <= ? LIMIT ?)",
                    (digest, current, selected_limit),
                ).rowcount
        except sqlite3.Error as error:
            raise _store_error("tenant_state_cleanup_failed") from error
        return CleanupResult(entries, events)

    def _set(
        self,
        scope: TenantScope,
        key: str,
        value: JsonValue,
        *,
        namespace: str,
        expires_at: float | None,
        now: float | None,
    ) -> None:
        digest = self._scope_digest(scope)
        selected_namespace = _name(namespace, "namespace")
        key_digest = self._key_digest(digest, selected_namespace, key)
        current = _timestamp(now)
        serialized = _encode(value)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO scoped_entries "
                    "(scope_digest, namespace, key_digest, value_json, updated_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(scope_digest, namespace, key_digest) "
                    "DO UPDATE SET value_json=excluded.value_json, "
                    "updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                    (
                        digest,
                        selected_namespace,
                        key_digest,
                        serialized,
                        current,
                        expires_at,
                    ),
                )
        except sqlite3.Error as error:
            raise _store_error("tenant_state_write_failed") from error

    def _scope_digest(self, scope: TenantScope) -> str:
        if not isinstance(scope, TenantScope):
            raise TypeError("scope must be a TenantScope")
        return scope.digest(self._scope_key)

    def _key_digest(self, scope_digest: str, namespace: str, key: str) -> str:
        selected_key = _identifier(key, "key")
        payload = canonical_json([scope_digest, namespace, selected_key]).encode("utf-8")
        return hmac.new(self._scope_key, _KEY_DOMAIN + payload, hashlib.sha256).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=self._timeout_seconds)
        connection.execute(f"PRAGMA busy_timeout = {int(self._timeout_seconds * 1_000)}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _transaction(self) -> sqlite3.Connection:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection


def _normalize_identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    normalized = unicodedata.normalize("NFKC", value).strip()
    return _identifier(normalized, label)


def _identifier(value: str, label: str) -> str:
    if not value or len(value) > 256 or len(value.encode("utf-8")) > 1_024:
        raise ValueError(f"{label} is invalid")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError(f"{label} is invalid")
    return value


def _name(value: str, label: str) -> str:
    selected = _identifier(value, label)
    if any(not (character.isalnum() or character in "._:-") for character in selected):
        raise ValueError(f"{label} is invalid")
    return selected


def _validate_scope_key(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) < 16:
        raise ValueError("scope_key must contain at least 16 bytes")
    return value


def _timestamp(value: float | None) -> float:
    selected = time() if value is None else value
    if isinstance(selected, bool) or not isinstance(selected, (int, float)):
        raise ValueError("timestamp must be a non-negative finite number")
    converted = float(selected)
    if not math.isfinite(converted) or converted < 0:
        raise ValueError("timestamp must be a non-negative finite number")
    return converted


def _ttl(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("ttl_seconds must be positive and finite")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError("ttl_seconds must be positive and finite")
    return converted


def _limit(value: int, *, maximum: int = 500) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _encode(value: JsonValue) -> str:
    _validate_json(value)
    return canonical_json(value)


def _validate_json(value: object) -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError("state value must contain finite JSON numbers")
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json(item)
        return
    raise ValueError("state value must be valid JSON")


def _decode(value: object) -> JsonValue:
    if not isinstance(value, str):
        raise _store_error("tenant_state_corrupt")
    try:
        decoded = json.loads(value)
        _validate_json(decoded)
        return cast(JsonValue, decoded)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise _store_error("tenant_state_corrupt") from error


def _stored_time(value: object) -> float:
    try:
        return _timestamp(cast(float, value))
    except (TypeError, ValueError) as error:
        raise _store_error("tenant_state_corrupt") from error


def _store_error(code: str) -> TenantStateError:
    messages = {
        "tenant_state_cleanup_failed": "Scoped state cleanup could not be completed.",
        "tenant_state_corrupt": "Scoped state is invalid or corrupted.",
        "tenant_state_open_failed": "Scoped state could not be opened.",
        "tenant_state_read_failed": "Scoped state could not be read.",
        "tenant_state_write_failed": "Scoped state could not be saved.",
    }
    return TenantStateError(code, messages[code])
