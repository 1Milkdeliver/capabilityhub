"""Tenant-isolated SQLite FTS index for production RAG providers."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import canonical_json
from capabilityhub.tenancy import TenantScope

_BENCHMARK_SCOPE = TenantScope("benchmark", "benchmark", "benchmark", "benchmark")
_PUBLIC = "*"


@dataclass(frozen=True, slots=True)
class RagPassage:
    passage_id: str
    citation: str
    text: str
    filters: Mapping[str, str] = field(default_factory=dict)
    allowed_principals: tuple[str, ...] = (_PUBLIC,)
    created_at: float = 0.0
    fresh_until: float | None = None
    retain_until: float | None = None


@dataclass(frozen=True, slots=True)
class RagHit:
    chunk_id: int | str
    citation: str
    text: str
    score: float
    expansion_handle: str
    created_at: float
    fresh_until: float | None
    retain_until: float | None
    fresh: bool


class DiskRagIndex:
    """Disk-backed index whose content, ACL and handles disclose no raw identity."""

    def __init__(self, path: str | Path, *, scope_key: bytes) -> None:
        if len(scope_key) < 16:
            raise ValueError("scope_key must contain at least 16 bytes")
        self.path = Path(path).resolve()
        self._key = bytes(scope_key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS rag_passages (
                        tenant_digest TEXT NOT NULL,
                        passage_id TEXT NOT NULL,
                        citation TEXT NOT NULL,
                        text TEXT NOT NULL,
                        content_digest TEXT NOT NULL,
                        filters_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        fresh_until REAL,
                        retain_until REAL,
                        PRIMARY KEY (tenant_digest, passage_id),
                        UNIQUE (tenant_digest, content_digest)
                    );
                    CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
                        tenant_digest UNINDEXED, passage_id UNINDEXED, text
                    );
                    CREATE TABLE IF NOT EXISTS rag_acl (
                        tenant_digest TEXT NOT NULL,
                        passage_id TEXT NOT NULL,
                        subject_digest TEXT NOT NULL,
                        PRIMARY KEY (tenant_digest, passage_id, subject_digest)
                    );
                    """
                )
        except sqlite3.Error as error:
            raise _index_error("rag_index_open_failed") from error

    def build(self, chunks: Iterable[tuple[int, str, str]]) -> int:
        """Compatibility ingestion used by the scale benchmark."""

        passages = (
            RagPassage(str(chunk_id), citation, text) for chunk_id, citation, text in chunks
        )
        return self.replace(_BENCHMARK_SCOPE, passages)

    def replace(self, scope: TenantScope, passages: Iterable[RagPassage]) -> int:
        tenant = self._tenant(scope)
        try:
            with self._connect() as connection:
                connection.execute("DELETE FROM rag_acl WHERE tenant_digest = ?", (tenant,))
                connection.execute("DELETE FROM rag_fts WHERE tenant_digest = ?", (tenant,))
                connection.execute("DELETE FROM rag_passages WHERE tenant_digest = ?", (tenant,))
                records: list[tuple[object, ...]] = []
                acl: list[tuple[str, str, str]] = []
                for passage in passages:
                    self._validate_passage(passage)
                    content_digest = hashlib.sha256(passage.text.encode("utf-8")).hexdigest()
                    records.append(
                        (
                            tenant,
                            passage.passage_id,
                            passage.citation,
                            passage.text,
                            content_digest,
                            canonical_json(dict(sorted(passage.filters.items()))),
                            passage.created_at,
                            passage.fresh_until,
                            passage.retain_until,
                        )
                    )
                    acl.extend(
                        (tenant, passage.passage_id, self._subject(principal))
                        for principal in passage.allowed_principals
                    )
                    if len(records) == 2_000:
                        self._insert_batch(connection, records, acl)
                        records.clear()
                        acl.clear()
                self._insert_batch(connection, records, acl)
                connection.execute(
                    "INSERT INTO rag_fts(tenant_digest, passage_id, text) "
                    "SELECT tenant_digest, passage_id, text FROM rag_passages "
                    "WHERE tenant_digest = ?",
                    (tenant,),
                )
                connection.execute("INSERT INTO rag_fts(rag_fts) VALUES ('optimize')")
                row = connection.execute(
                    "SELECT count(*) FROM rag_passages WHERE tenant_digest = ?", (tenant,)
                ).fetchone()
            return int(row[0])
        except sqlite3.Error as error:
            raise _index_error("rag_index_write_failed") from error

    @staticmethod
    def _insert_batch(
        connection: sqlite3.Connection,
        records: list[tuple[object, ...]],
        acl: list[tuple[str, str, str]],
    ) -> None:
        connection.executemany(
            "INSERT OR IGNORE INTO rag_passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", records
        )
        connection.executemany("INSERT OR IGNORE INTO rag_acl VALUES (?, ?, ?)", acl)

    def set_acl(
        self, scope: TenantScope, passage_id: str, allowed_principals: Iterable[str]
    ) -> None:
        tenant = self._tenant(scope)
        selected = tuple(dict.fromkeys(allowed_principals))
        if not selected or any(not item for item in selected):
            raise ValueError("allowed_principals must not be empty")
        try:
            with self._connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM rag_passages WHERE tenant_digest = ? AND passage_id = ?",
                    (tenant, passage_id),
                ).fetchone()
                if exists is None:
                    raise _unavailable()
                connection.execute(
                    "DELETE FROM rag_acl WHERE tenant_digest = ? AND passage_id = ?",
                    (tenant, passage_id),
                )
                connection.executemany(
                    "INSERT INTO rag_acl VALUES (?, ?, ?)",
                    ((tenant, passage_id, self._subject(item)) for item in selected),
                )
        except CapabilityHubError:
            raise
        except sqlite3.Error as error:
            raise _index_error("rag_acl_update_failed") from error

    def purge_expired(self, scope: TenantScope, *, now: float) -> int:
        """Delete retained content only inside the caller's tenant partition."""

        if not math.isfinite(now):
            raise ValueError("now must be finite")
        tenant = self._tenant(scope)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT passage_id FROM rag_passages WHERE tenant_digest = ? "
                    "AND retain_until IS NOT NULL AND retain_until <= ?",
                    (tenant, now),
                ).fetchall()
                passage_ids = tuple(str(row[0]) for row in rows)
                if not passage_ids:
                    return 0
                connection.executemany(
                    "DELETE FROM rag_fts WHERE tenant_digest = ? AND passage_id = ?",
                    ((tenant, passage_id) for passage_id in passage_ids),
                )
                connection.executemany(
                    "DELETE FROM rag_acl WHERE tenant_digest = ? AND passage_id = ?",
                    ((tenant, passage_id) for passage_id in passage_ids),
                )
                connection.executemany(
                    "DELETE FROM rag_passages WHERE tenant_digest = ? AND passage_id = ?",
                    ((tenant, passage_id) for passage_id in passage_ids),
                )
            return len(passage_ids)
        except sqlite3.Error as error:
            raise _index_error("rag_retention_cleanup_failed") from error

    def checkpoint(self) -> None:
        """Checkpoint and truncate WAL bytes before durable storage accounting."""

        try:
            with self._connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as error:
            raise _index_error("rag_index_checkpoint_failed") from error

    def storage_bytes(self) -> int:
        """Return deterministic durable index bytes after a WAL checkpoint."""

        self.checkpoint()
        return sum(
            candidate.stat().st_size
            for candidate in self.path.parent.glob(f"{self.path.name}*")
            if candidate.is_file()
        )

    def search(
        self,
        query: str,
        *,
        scope: TenantScope = _BENCHMARK_SCOPE,
        top_k: int = 5,
        filters: Mapping[str, str] | None = None,
        max_bytes: int = 64_000,
        now: float = 0.0,
    ) -> tuple[RagHit, ...]:
        if not query.strip() or not 1 <= top_k <= 20 or max_bytes < 1 or not math.isfinite(now):
            raise ValueError("query bounds are invalid")
        selected_filters = dict(filters or {})
        phrase = '"' + query.replace('"', '""') + '"'
        tenant = self._tenant(scope)
        subjects = (self._subject(_PUBLIC), self._subject(scope.principal))
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT p.passage_id, p.citation, p.text, bm25(rag_fts), "
                    "p.filters_json, p.created_at, p.fresh_until, p.retain_until "
                    "FROM rag_fts JOIN rag_passages p "
                    "ON p.tenant_digest = rag_fts.tenant_digest "
                    "AND p.passage_id = rag_fts.passage_id "
                    "WHERE rag_fts MATCH ? AND p.tenant_digest = ? "
                    "AND (p.retain_until IS NULL OR p.retain_until > ?) "
                    "AND EXISTS (SELECT 1 FROM rag_acl a WHERE a.tenant_digest = ? "
                    "AND a.passage_id = p.passage_id AND a.subject_digest IN (?, ?)) "
                    "ORDER BY bm25(rag_fts), p.passage_id LIMIT ?",
                    (phrase, tenant, now, tenant, *subjects, top_k * 20),
                ).fetchall()
        except sqlite3.Error as error:
            raise _index_error("rag_index_search_failed") from error
        hits: list[RagHit] = []
        used = 0
        for row in rows:
            metadata = cast(dict[str, str], json.loads(str(row[4])))
            if any(metadata.get(key) != value for key, value in selected_filters.items()):
                continue
            text = str(row[2])
            size = len(text.encode("utf-8"))
            if used + size > max_bytes:
                break
            used += size
            passage_id = str(row[0])
            hits.append(
                RagHit(
                    _chunk_id(passage_id),
                    str(row[1]),
                    text,
                    float(row[3]),
                    self._handle(tenant, passage_id),
                    float(row[5]),
                    None if row[6] is None else float(row[6]),
                    None if row[7] is None else float(row[7]),
                    row[6] is None or float(row[6]) > now,
                )
            )
            if len(hits) == top_k:
                break
        return tuple(hits)

    def expand(
        self,
        handle: str,
        *,
        scope: TenantScope,
        max_bytes: int = 64_000,
        now: float = 0.0,
    ) -> RagHit:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        tenant = self._tenant(scope)
        passage_id = self._verify_handle(handle, tenant)
        subjects = (self._subject(_PUBLIC), self._subject(scope.principal))
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT p.citation, p.text, p.created_at, p.fresh_until, p.retain_until "
                    "FROM rag_passages p WHERE p.tenant_digest = ? AND p.passage_id = ? "
                    "AND (p.retain_until IS NULL OR p.retain_until > ?) "
                    "AND EXISTS (SELECT 1 FROM rag_acl a WHERE a.tenant_digest = ? "
                    "AND a.passage_id = p.passage_id AND a.subject_digest IN (?, ?))",
                    (tenant, passage_id, now, tenant, *subjects),
                ).fetchone()
        except sqlite3.Error as error:
            raise _index_error("rag_index_expand_failed") from error
        if row is None or len(str(row[1]).encode("utf-8")) > max_bytes:
            raise _unavailable()
        return RagHit(
            _chunk_id(passage_id),
            str(row[0]),
            str(row[1]),
            0.0,
            handle,
            float(row[2]),
            None if row[3] is None else float(row[3]),
            None if row[4] is None else float(row[4]),
            row[3] is None or float(row[3]) > now,
        )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def _tenant(self, scope: TenantScope) -> str:
        isolated = TenantScope(scope.tenant, "rag-index", "rag-index", "rag-index")
        return isolated.digest(self._key)

    def _subject(self, principal: str) -> str:
        material = b"rag-subject\0" + principal.encode()
        return hmac.new(self._key, material, hashlib.sha256).hexdigest()

    def _handle(self, tenant: str, passage_id: str) -> str:
        body = f"{tenant}.{passage_id}"
        signature = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
        return f"rag1.{passage_id}.{signature}"

    def _verify_handle(self, handle: str, tenant: str) -> str:
        try:
            prefix, passage_id, _signature = handle.split(".", 2)
        except ValueError as error:
            raise _unavailable() from error
        expected = self._handle(tenant, passage_id)
        if prefix != "rag1" or not hmac.compare_digest(handle, expected):
            raise _unavailable()
        return passage_id

    @staticmethod
    def _validate_passage(passage: RagPassage) -> None:
        if not passage.passage_id or not passage.citation or not passage.text:
            raise ValueError("passage fields must not be empty")
        if any(not key or not value for key, value in passage.filters.items()):
            raise ValueError("passage filters must be non-empty strings")
        if not passage.allowed_principals:
            raise ValueError("passage ACL must not be empty")
        if passage.retain_until is not None and passage.retain_until <= passage.created_at:
            raise ValueError("retention must end after creation")


def _chunk_id(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def _unavailable() -> CapabilityHubError:
    return CapabilityHubError(
        code="rag_passage_unavailable",
        category=ErrorCategory.REFERENCE,
        safe_message="The selected passage is unavailable.",
    )


def _index_error(code: str) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.INTERNAL,
        safe_message="The RAG index operation failed.",
    )
