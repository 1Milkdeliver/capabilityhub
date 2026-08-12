"""Durable exact-intent approval workflow with single-use consumption."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from time import time
from uuid import uuid4

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import canonical_json
from capabilityhub.models import JsonValue
from capabilityhub.tenancy import TenantScope


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CONSUMED = "consumed"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ApprovalIntent:
    revision: str
    operation: str
    arguments_digest: str
    tenant_id: str
    principal_id: str
    session_id: str
    task_id: str
    side_effect: str
    policy_revision: str

    @classmethod
    def from_arguments(
        cls,
        *,
        revision: str,
        operation: str,
        arguments: Mapping[str, JsonValue],
        tenant_id: str,
        principal_id: str,
        session_id: str,
        task_id: str,
        side_effect: str,
        policy_revision: str,
    ) -> ApprovalIntent:
        return cls(
            revision=revision,
            operation=operation,
            arguments_digest=_arguments_digest(arguments),
            tenant_id=tenant_id,
            principal_id=principal_id,
            session_id=session_id,
            task_id=task_id,
            side_effect=side_effect,
            policy_revision=policy_revision,
        )

    def __post_init__(self) -> None:
        for field_name in (
            "revision",
            "operation",
            "tenant_id",
            "principal_id",
            "session_id",
            "task_id",
            "side_effect",
            "policy_revision",
        ):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} must be non-empty")
        if len(self.arguments_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.arguments_digest
        ):
            raise ValueError("arguments_digest must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    intent: ApprovalIntent
    status: ApprovalStatus
    created_at: float
    expires_at: float
    decided_at: float | None = None
    decided_by: str | None = None
    consumed_at: float | None = None


class ApprovalStoreError(CapabilityHubError):
    def __init__(self, code: str, message: str, *, approval_id: str) -> None:
        super().__init__(
            code=code,
            category=ErrorCategory.APPROVAL,
            safe_message=message,
            retryable=False,
            details={"approval_id": approval_id},
        )


class SqliteApprovalStore:
    """Persist approval decisions without retaining argument bodies."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS approval_records (
                    approval_id TEXT PRIMARY KEY,
                    revision TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    arguments_digest TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    side_effect TEXT NOT NULL,
                    policy_revision TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'approved', 'denied', 'consumed', 'expired')
                    ),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    decided_at REAL,
                    decided_by TEXT,
                    consumed_at REAL
                );
                CREATE INDEX IF NOT EXISTS approval_status_expiry
                    ON approval_records(status, expires_at);
                """
            )

    @property
    def path(self) -> Path:
        return self._path

    def request(
        self,
        intent: ApprovalIntent,
        *,
        ttl_seconds: float,
        approval_id: str | None = None,
        now: float | None = None,
    ) -> ApprovalRecord:
        if isinstance(ttl_seconds, bool) or not isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        identifier = approval_id or f"apr_{uuid4().hex}"
        if not identifier:
            raise ValueError("approval_id must be non-empty")
        created = _now(now)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO approval_records VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL)",
                    (
                        identifier,
                        intent.revision,
                        intent.operation,
                        intent.arguments_digest,
                        intent.tenant_id,
                        intent.principal_id,
                        intent.session_id,
                        intent.task_id,
                        intent.side_effect,
                        intent.policy_revision,
                        created,
                        created + ttl_seconds,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("approval_id must be unique") from error
        return self.get(identifier, now=created)

    def get(self, approval_id: str, *, now: float | None = None) -> ApprovalRecord:
        current = _now(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, current, approval_id=approval_id)
            row = connection.execute(f"{_SELECT} WHERE approval_id = ?", (approval_id,)).fetchone()
        if row is None:
            raise _error("approval_not_found", "The approval request was not found.", approval_id)
        return _record(row)

    def list(
        self,
        *,
        status: ApprovalStatus | str | None = None,
        limit: int = 100,
        now: float | None = None,
    ) -> tuple[ApprovalRecord, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        selected = ApprovalStatus(status) if status is not None else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, _now(now))
            if selected is None:
                rows = connection.execute(
                    f"{_SELECT} ORDER BY created_at DESC, approval_id DESC LIMIT ?", (limit,)
                )
            else:
                rows = connection.execute(
                    f"{_SELECT} WHERE status = ? "
                    "ORDER BY created_at DESC, approval_id DESC LIMIT ?",
                    (selected.value, limit),
                )
            return tuple(_record(row) for row in rows)

    def approve(
        self,
        approval_id: str,
        *,
        decided_by: str,
        now: float | None = None,
    ) -> ApprovalRecord:
        return self._decide(approval_id, ApprovalStatus.APPROVED, decided_by=decided_by, now=now)

    def deny(
        self,
        approval_id: str,
        *,
        decided_by: str,
        now: float | None = None,
    ) -> ApprovalRecord:
        return self._decide(approval_id, ApprovalStatus.DENIED, decided_by=decided_by, now=now)

    def consume(
        self,
        approval_id: str,
        intent: ApprovalIntent,
        *,
        now: float | None = None,
    ) -> ApprovalRecord:
        current = _now(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, current, approval_id=approval_id)
            row = connection.execute(f"{_SELECT} WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None:
                raise _error(
                    "approval_not_found", "The approval request was not found.", approval_id
                )
            record = _record(row)
            if not _same_intent(record.intent, intent):
                raise _error(
                    "approval_intent_mismatch",
                    "The approval does not match this exact execution intent.",
                    approval_id,
                )
            if record.status is ApprovalStatus.EXPIRED:
                raise _error("approval_expired", "The approval has expired.", approval_id)
            if record.status is ApprovalStatus.CONSUMED:
                raise _error(
                    "approval_already_consumed", "The approval was already used.", approval_id
                )
            if record.status is not ApprovalStatus.APPROVED:
                raise _error("approval_not_approved", "The approval is not approved.", approval_id)
            connection.execute(
                "UPDATE approval_records SET status = 'consumed', consumed_at = ? "
                "WHERE approval_id = ? AND status = 'approved'",
                (current, approval_id),
            )
            consumed = connection.execute(
                f"{_SELECT} WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        assert consumed is not None
        return _record(consumed)

    def _decide(
        self,
        approval_id: str,
        status: ApprovalStatus,
        *,
        decided_by: str,
        now: float | None,
    ) -> ApprovalRecord:
        if not decided_by:
            raise ValueError("decided_by must be non-empty")
        current = _now(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, current, approval_id=approval_id)
            row = connection.execute(
                "SELECT status FROM approval_records WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise _error(
                    "approval_not_found", "The approval request was not found.", approval_id
                )
            current_status = ApprovalStatus(row[0])
            if current_status is ApprovalStatus.EXPIRED:
                raise _error("approval_expired", "The approval has expired.", approval_id)
            if current_status is not ApprovalStatus.PENDING:
                raise _error(
                    "approval_invalid_transition",
                    "The approval request is no longer pending.",
                    approval_id,
                )
            connection.execute(
                "UPDATE approval_records SET status = ?, decided_at = ?, decided_by = ? "
                "WHERE approval_id = ? AND status = 'pending'",
                (status.value, current, decided_by, approval_id),
            )
            decided = connection.execute(
                f"{_SELECT} WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        assert decided is not None
        return _record(decided)

    @staticmethod
    def _expire(
        connection: sqlite3.Connection, now: float, *, approval_id: str | None = None
    ) -> None:
        where = " AND approval_id = ?" if approval_id is not None else ""
        parameters: tuple[object, ...] = (now, approval_id) if approval_id is not None else (now,)
        connection.execute(
            "UPDATE approval_records SET status = 'expired' "
            "WHERE status IN ('pending', 'approved') AND expires_at <= ?" + where,
            parameters,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


_SELECT = (
    "SELECT approval_id, revision, operation, arguments_digest, tenant_id, principal_id, "
    "session_id, task_id, side_effect, policy_revision, status, created_at, expires_at, "
    "decided_at, decided_by, consumed_at FROM approval_records"
)


def _record(row: tuple[object, ...]) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=str(row[0]),
        intent=ApprovalIntent(*(str(value) for value in row[1:10])),
        status=ApprovalStatus(str(row[10])),
        created_at=_stored_float(row[11]),
        expires_at=_stored_float(row[12]),
        decided_at=_stored_float(row[13]) if row[13] is not None else None,
        decided_by=str(row[14]) if row[14] is not None else None,
        consumed_at=_stored_float(row[15]) if row[15] is not None else None,
    )


def _arguments_digest(arguments: Mapping[str, JsonValue]) -> str:
    return hashlib.sha256(canonical_json(dict(arguments)).encode("utf-8")).hexdigest()


def _same_intent(left: ApprovalIntent, right: ApprovalIntent) -> bool:
    return hmac.compare_digest(_intent_fingerprint(left), _intent_fingerprint(right))


def _intent_fingerprint(intent: ApprovalIntent) -> bytes:
    return hashlib.sha256(
        canonical_json(
            {
                "arguments_digest": intent.arguments_digest,
                "operation": intent.operation,
                "policy_revision": intent.policy_revision,
                "principal_id": intent.principal_id,
                "revision": intent.revision,
                "session_id": intent.session_id,
                "side_effect": intent.side_effect,
                "task_id": intent.task_id,
                "tenant_id": intent.tenant_id,
            }
        ).encode("utf-8")
    ).digest()


def _now(value: float | None) -> float:
    selected = time() if value is None else value
    if isinstance(selected, bool) or not isfinite(selected) or selected < 0:
        raise ValueError("now must be non-negative")
    return selected


def _stored_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("stored approval timestamp is invalid")
    return float(value)


def _error(code: str, message: str, approval_id: str) -> ApprovalStoreError:
    return ApprovalStoreError(code, message, approval_id=approval_id)


class ScopedApprovalStore:
    """Exact-intent approvals partitioned by an opaque caller-scope digest."""

    def __init__(self, path: str | Path, *, scope_key: bytes) -> None:
        if not isinstance(scope_key, bytes) or len(scope_key) < 16:
            raise ValueError("scope_key must contain at least 16 bytes")
        self._path = Path(path).resolve()
        self._scope_key = scope_key
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scoped_approval_records (
                    scope_digest TEXT NOT NULL,
                    approval_digest TEXT NOT NULL,
                    approval_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    arguments_digest TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    side_effect TEXT NOT NULL,
                    policy_revision TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'approved', 'denied', 'consumed', 'expired')
                    ),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    decided_at REAL,
                    decided_by TEXT,
                    consumed_at REAL,
                    PRIMARY KEY (scope_digest, approval_digest)
                );
                CREATE INDEX IF NOT EXISTS scoped_approval_status_expiry
                    ON scoped_approval_records(scope_digest, status, expires_at);
                """
            )

    def request(
        self,
        scope: TenantScope,
        intent: ApprovalIntent,
        *,
        ttl_seconds: float,
        approval_id: str | None = None,
        now: float | None = None,
    ) -> ApprovalRecord:
        self._require_matching_scope(scope, intent)
        if isinstance(ttl_seconds, bool) or not isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        identifier = approval_id or f"apr_{uuid4().hex}"
        if not identifier:
            raise ValueError("approval_id must be non-empty")
        current = _now(now)
        scope_digest, approval_digest = self._digests(scope, identifier)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO scoped_approval_records VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL)",
                    (
                        scope_digest,
                        approval_digest,
                        identifier,
                        intent.revision,
                        intent.operation,
                        intent.arguments_digest,
                        intent.tenant_id,
                        intent.principal_id,
                        intent.session_id,
                        intent.task_id,
                        intent.side_effect,
                        intent.policy_revision,
                        current,
                        current + ttl_seconds,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("approval_id must be unique within its scope") from error
        return self.get(scope, identifier, now=current)

    def get(
        self, scope: TenantScope, approval_id: str, *, now: float | None = None
    ) -> ApprovalRecord:
        current = _now(now)
        scope_digest, approval_digest = self._digests(scope, approval_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, scope_digest, current, approval_digest=approval_digest)
            row = connection.execute(
                f"{_SCOPED_SELECT} WHERE scope_digest = ? AND approval_digest = ?",
                (scope_digest, approval_digest),
            ).fetchone()
        if row is None:
            raise _error("approval_not_found", "The approval request was not found.", approval_id)
        return _record(row)

    def list(
        self,
        scope: TenantScope,
        *,
        status: ApprovalStatus | str | None = None,
        limit: int = 100,
        now: float | None = None,
    ) -> tuple[ApprovalRecord, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        scope_digest = scope.digest(self._scope_key)
        selected = ApprovalStatus(status) if status is not None else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, scope_digest, _now(now))
            parameters: tuple[object, ...]
            where = "scope_digest = ?"
            parameters = (scope_digest,)
            if selected is not None:
                where += " AND status = ?"
                parameters += (selected.value,)
            rows = connection.execute(
                f"{_SCOPED_SELECT} WHERE {where} "
                "ORDER BY created_at DESC, approval_digest DESC LIMIT ?",
                (*parameters, limit),
            ).fetchall()
        return tuple(_record(row) for row in rows)

    def approve(
        self, scope: TenantScope, approval_id: str, *, decided_by: str, now: float | None = None
    ) -> ApprovalRecord:
        return self._decide(scope, approval_id, ApprovalStatus.APPROVED, decided_by, now)

    def deny(
        self, scope: TenantScope, approval_id: str, *, decided_by: str, now: float | None = None
    ) -> ApprovalRecord:
        return self._decide(scope, approval_id, ApprovalStatus.DENIED, decided_by, now)

    def consume(
        self,
        scope: TenantScope,
        approval_id: str,
        intent: ApprovalIntent,
        *,
        now: float | None = None,
    ) -> ApprovalRecord:
        self._require_matching_scope(scope, intent)
        current = _now(now)
        scope_digest, approval_digest = self._digests(scope, approval_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, scope_digest, current, approval_digest=approval_digest)
            row = connection.execute(
                f"{_SCOPED_SELECT} WHERE scope_digest = ? AND approval_digest = ?",
                (scope_digest, approval_digest),
            ).fetchone()
            if row is None:
                raise _error(
                    "approval_not_found", "The approval request was not found.", approval_id
                )
            record = _record(row)
            if not _same_intent(record.intent, intent):
                raise _error(
                    "approval_intent_mismatch",
                    "The approval does not match this exact execution intent.",
                    approval_id,
                )
            if record.status is not ApprovalStatus.APPROVED:
                code = {
                    ApprovalStatus.EXPIRED: "approval_expired",
                    ApprovalStatus.CONSUMED: "approval_already_consumed",
                }.get(record.status, "approval_not_approved")
                raise _error(code, "The approval cannot be consumed.", approval_id)
            cursor = connection.execute(
                "UPDATE scoped_approval_records SET status = 'consumed', consumed_at = ? "
                "WHERE scope_digest = ? AND approval_digest = ? AND status = 'approved'",
                (current, scope_digest, approval_digest),
            )
            if cursor.rowcount != 1:
                raise _error(
                    "approval_already_consumed", "The approval cannot be consumed.", approval_id
                )
        return self.get(scope, approval_id, now=current)

    def _decide(
        self,
        scope: TenantScope,
        approval_id: str,
        status: ApprovalStatus,
        decided_by: str,
        now: float | None,
    ) -> ApprovalRecord:
        if not decided_by:
            raise ValueError("decided_by must be non-empty")
        current = _now(now)
        scope_digest, approval_digest = self._digests(scope, approval_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, scope_digest, current, approval_digest=approval_digest)
            row = connection.execute(
                "SELECT status FROM scoped_approval_records "
                "WHERE scope_digest = ? AND approval_digest = ?",
                (scope_digest, approval_digest),
            ).fetchone()
            if row is None:
                raise _error(
                    "approval_not_found", "The approval request was not found.", approval_id
                )
            if ApprovalStatus(str(row[0])) is not ApprovalStatus.PENDING:
                raise _error(
                    "approval_invalid_transition",
                    "The approval request is no longer pending.",
                    approval_id,
                )
            connection.execute(
                "UPDATE scoped_approval_records SET status = ?, decided_at = ?, decided_by = ? "
                "WHERE scope_digest = ? AND approval_digest = ? AND status = 'pending'",
                (status.value, current, decided_by, scope_digest, approval_digest),
            )
        return self.get(scope, approval_id, now=current)

    def _digests(self, scope: TenantScope, approval_id: str) -> tuple[str, str]:
        if not approval_id:
            raise ValueError("approval_id must be non-empty")
        scope_digest = scope.digest(self._scope_key)
        approval_digest = hmac.new(
            self._scope_key,
            b"capabilityhub-approval-id-v1\0"
            + scope_digest.encode("ascii")
            + approval_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return scope_digest, approval_digest

    @staticmethod
    def _require_matching_scope(scope: TenantScope, intent: ApprovalIntent) -> None:
        if (
            scope.tenant,
            scope.principal,
            scope.session,
            scope.task,
        ) != (
            intent.tenant_id,
            intent.principal_id,
            intent.session_id,
            intent.task_id,
        ):
            raise _error(
                "approval_not_found",
                "The approval request was not found.",
                "unavailable",
            )

    @staticmethod
    def _expire(
        connection: sqlite3.Connection,
        scope_digest: str,
        now: float,
        *,
        approval_digest: str | None = None,
    ) -> None:
        where = " AND approval_digest = ?" if approval_digest is not None else ""
        parameters: tuple[object, ...] = (
            (now, scope_digest, approval_digest)
            if approval_digest is not None
            else (now, scope_digest)
        )
        connection.execute(
            "UPDATE scoped_approval_records SET status = 'expired' "
            "WHERE status IN ('pending', 'approved') AND expires_at <= ? "
            "AND scope_digest = ?" + where,
            parameters,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


_SCOPED_SELECT = (
    "SELECT approval_id, revision, operation, arguments_digest, tenant_id, principal_id, "
    "session_id, task_id, side_effect, policy_revision, status, created_at, expires_at, "
    "decided_at, decided_by, consumed_at FROM scoped_approval_records"
)
