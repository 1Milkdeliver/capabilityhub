"""Privacy-minimized spans and bounded-cardinality metric aggregation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import RLock
from time import monotonic, time
from typing import Protocol
from uuid import uuid4

from capabilityhub.errors import CapabilityHubError, ErrorCategory

OBSERVABILITY_SCHEMA = "capabilityhub.observability.metrics"
OBSERVABILITY_VERSION = 1
_LABEL = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")


class ProviderCategory(StrEnum):
    SKILL = "skill"
    MCP = "mcp"
    CLI = "cli"
    API = "api"
    RAG = "rag"
    OTHER = "other"


class AdapterCategory(StrEnum):
    CLI = "cli"
    MCP = "mcp"
    HTTP = "http"
    LIBRARY = "library"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Hashed correlation context safe to carry across transport adapters."""

    correlation_id: str
    adapter: AdapterCategory
    parent_span_id: str | None = None

    @classmethod
    def from_correlation(cls, correlation_id: str, adapter: AdapterCategory | str) -> TraceContext:
        if not isinstance(correlation_id, str) or not correlation_id or len(correlation_id) > 512:
            raise ValueError("correlation_id is invalid")
        digest = hashlib.sha256(
            b"capabilityhub-correlation-v1\0" + correlation_id.encode("utf-8")
        ).hexdigest()
        return cls(digest, _adapter(adapter))

    def for_adapter(
        self,
        adapter: AdapterCategory | str,
        *,
        parent_span_id: str | None = None,
    ) -> TraceContext:
        parent = self.parent_span_id if parent_span_id is None else _span_id(parent_span_id)
        return TraceContext(self.correlation_id, _adapter(adapter), parent)

    def __post_init__(self) -> None:
        if not _hex(self.correlation_id, 64):
            raise ValueError("correlation_id must be a SHA-256 digest")
        if self.parent_span_id is not None:
            _span_id(self.parent_span_id)


@dataclass(frozen=True, slots=True)
class SpanRecord:
    correlation_id: str
    span_id: str
    parent_span_id: str | None
    adapter: AdapterCategory
    operation: str
    provider_category: ProviderCategory
    latency_ms: float
    portable_tokens: int
    payload_bytes: int
    error_code: str | None


@dataclass(frozen=True, slots=True)
class MetricKey:
    adapter: str
    operation: str
    provider_category: str
    outcome: str
    error_code: str


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    key: MetricKey
    count: int
    error_count: int
    latency_ms_total: float
    latency_ms_max: float
    portable_tokens: int
    payload_bytes: int
    updated_at: float


@dataclass(frozen=True, slots=True)
class ObservabilityHealth:
    status: str
    failure_count: int
    error_code: str | None = None


@dataclass(slots=True)
class _MetricAccumulator:
    count: int = 0
    error_count: int = 0
    latency_ms_total: float = 0
    latency_ms_max: float = 0
    portable_tokens: int = 0
    payload_bytes: int = 0
    updated_at: float = 0

    def add(self, record: SpanRecord, updated_at: float) -> None:
        self.count += 1
        self.error_count += record.error_code is not None
        self.latency_ms_total += record.latency_ms
        self.latency_ms_max = max(self.latency_ms_max, record.latency_ms)
        self.portable_tokens += record.portable_tokens
        self.payload_bytes += record.payload_bytes
        self.updated_at = max(self.updated_at, updated_at)


class MetricStore(Protocol):
    def increment(self, key: MetricKey, record: SpanRecord, *, updated_at: float) -> None: ...


class DeterministicSampler:
    def __init__(self, rate: float = 0.1) -> None:
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise ValueError("sampling rate must be between zero and one")
        selected = float(rate)
        if not math.isfinite(selected) or not 0 <= selected <= 1:
            raise ValueError("sampling rate must be between zero and one")
        self._threshold = int(selected * (1 << 256))

    def selected(self, correlation_digest: str, *, has_error: bool) -> bool:
        if has_error:
            return True
        if not _hex(correlation_digest, 64):
            raise ValueError("correlation digest is invalid")
        return int(correlation_digest, 16) < self._threshold


class InMemoryObservability:
    """Thread-safe sampled span buffer and bounded metric aggregator."""

    def __init__(
        self,
        *,
        allowed_operations: Iterable[str] = ("search", "load", "execute"),
        allowed_error_codes: Iterable[str] = (),
        sampler: DeterministicSampler | None = None,
        span_limit: int = 1_000,
        metric_series_limit: int = 2_000,
        persistent_metrics: MetricStore | None = None,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], float] = time,
    ) -> None:
        if not 1 <= span_limit <= 100_000:
            raise ValueError("span_limit must be between 1 and 100000")
        if not 1 <= metric_series_limit <= 100_000:
            raise ValueError("metric_series_limit must be between 1 and 100000")
        self._operations = _allowlist(allowed_operations, "operation")
        self._error_codes = _allowlist(allowed_error_codes, "error code")
        self._sampler = sampler or DeterministicSampler()
        self._spans: deque[SpanRecord] = deque(maxlen=span_limit)
        self._metrics: dict[MetricKey, _MetricAccumulator] = {}
        self._metric_series_limit = metric_series_limit
        self._persistent_metrics = persistent_metrics
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = RLock()
        self._failure_count = 0
        self._last_error: str | None = None

    @property
    def spans(self) -> tuple[SpanRecord, ...]:
        with self._lock:
            return tuple(self._spans)

    def start_span(
        self,
        context: TraceContext,
        *,
        operation: str,
        provider_category: ProviderCategory | str,
    ) -> SpanHandle:
        if not isinstance(context, TraceContext):
            raise TypeError("context must be a TraceContext")
        return SpanHandle(
            observer=self,
            context=context,
            span_id=uuid4().hex,
            operation=self._label(operation, self._operations, fallback="other"),
            provider_category=_provider_category(provider_category),
            started_at=_timestamp(self._clock()),
        )

    def metric_snapshots(self, *, limit: int = 500) -> tuple[MetricSnapshot, ...]:
        selected_limit = _limit(limit)
        with self._lock:
            return tuple(
                _snapshot(key, accumulator)
                for key, accumulator in sorted(
                    self._metrics.items(), key=lambda item: _key_tuple(item[0])
                )[:selected_limit]
            )

    def export_jsonl(self, *, limit: int = 500) -> str:
        envelope = {"schema": OBSERVABILITY_SCHEMA, "version": OBSERVABILITY_VERSION}
        lines = [json.dumps(envelope, sort_keys=True, separators=(",", ":"))]
        lines.extend(_metric_json(snapshot) for snapshot in self.metric_snapshots(limit=limit))
        return "\n".join(lines) + "\n"

    def health(self) -> ObservabilityHealth:
        with self._lock:
            return ObservabilityHealth(
                status="ok" if self._last_error is None else "degraded",
                failure_count=self._failure_count,
                error_code=self._last_error,
            )

    def _finish(
        self,
        *,
        context: TraceContext,
        span_id: str,
        operation: str,
        provider_category: ProviderCategory,
        started_at: float,
        portable_tokens: int,
        payload_bytes: int,
        error_code: str | None,
    ) -> SpanRecord:
        ended_at = _timestamp(self._clock())
        latency_ms = max(0.0, (ended_at - started_at) * 1_000)
        safe_operation = self._label(operation, self._operations, fallback="other")
        safe_provider_category = _provider_category(provider_category)
        safe_error = (
            None
            if error_code is None
            else self._label(error_code, self._error_codes, fallback="other_error")
        )
        record = SpanRecord(
            correlation_id=context.correlation_id,
            span_id=span_id,
            parent_span_id=context.parent_span_id,
            adapter=context.adapter,
            operation=safe_operation,
            provider_category=safe_provider_category,
            latency_ms=latency_ms,
            portable_tokens=_counter(portable_tokens, "portable_tokens"),
            payload_bytes=_counter(payload_bytes, "payload_bytes"),
            error_code=safe_error,
        )
        key = MetricKey(
            adapter=record.adapter.value,
            operation=record.operation,
            provider_category=record.provider_category.value,
            outcome="error" if record.error_code is not None else "success",
            error_code=record.error_code or "none",
        )
        updated_at = _timestamp(self._wall_clock())
        with self._lock:
            selected_key = key
            overflow_key = _overflow_key()
            regular_count = len(self._metrics) - int(overflow_key in self._metrics)
            if (
                key not in self._metrics
                and key != overflow_key
                and regular_count >= max(0, self._metric_series_limit - 1)
            ):
                selected_key = overflow_key
            accumulator = self._metrics.setdefault(selected_key, _MetricAccumulator())
            accumulator.add(record, updated_at)
            if self._sampler.selected(
                record.correlation_id, has_error=record.error_code is not None
            ):
                self._spans.append(record)
            if self._persistent_metrics is not None:
                try:
                    self._persistent_metrics.increment(selected_key, record, updated_at=updated_at)
                except Exception as error:
                    self._failure_count += 1
                    self._last_error = "observability_persistence_failed"
                    if isinstance(error, CapabilityHubError):
                        raise
                    raise _observation_error("observability_persistence_failed") from error
        return record

    @staticmethod
    def _label(value: object, allowlist: frozenset[str], *, fallback: str) -> str:
        return value if isinstance(value, str) and value in allowlist else fallback


@dataclass(slots=True)
class SpanHandle:
    observer: InMemoryObservability
    context: TraceContext
    span_id: str
    operation: str
    provider_category: ProviderCategory
    started_at: float
    _finished: bool = False

    def finish(
        self,
        *,
        portable_tokens: int = 0,
        payload_bytes: int = 0,
        error_code: str | None = None,
    ) -> SpanRecord:
        if self._finished:
            raise _observation_error("observability_span_already_finished")
        self._finished = True
        return self.observer._finish(
            context=self.context,
            span_id=self.span_id,
            operation=self.operation,
            provider_category=self.provider_category,
            started_at=self.started_at,
            portable_tokens=portable_tokens,
            payload_bytes=payload_bytes,
            error_code=error_code,
        )


class SqliteMetricStore:
    """Optional aggregate-only persistence with bounded retention and export."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5) -> None:
        self._path = Path(path).resolve()
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self._timeout_seconds = timeout_seconds
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS observability_metrics (
                        adapter TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        provider_category TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        error_code TEXT NOT NULL,
                        count INTEGER NOT NULL,
                        error_count INTEGER NOT NULL,
                        latency_ms_total REAL NOT NULL,
                        latency_ms_max REAL NOT NULL,
                        portable_tokens INTEGER NOT NULL,
                        payload_bytes INTEGER NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (
                            adapter, operation, provider_category, outcome, error_code
                        )
                    )
                    """
                )
        except sqlite3.Error as error:
            raise _observation_error("observability_persistence_failed") from error

    @property
    def path(self) -> Path:
        return self._path

    def increment(self, key: MetricKey, record: SpanRecord, *, updated_at: float) -> None:
        try:
            _validate_metric_key(key)
            _validate_record(record)
        except (TypeError, ValueError) as error:
            raise _observation_error("observability_metric_invalid") from error
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO observability_metrics VALUES "
                    "(?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(adapter, operation, provider_category, outcome, error_code) "
                    "DO UPDATE SET count=count+1, error_count=error_count+excluded.error_count, "
                    "latency_ms_total=latency_ms_total+excluded.latency_ms_total, "
                    "latency_ms_max=MAX(latency_ms_max, excluded.latency_ms_max), "
                    "portable_tokens=portable_tokens+excluded.portable_tokens, "
                    "payload_bytes=payload_bytes+excluded.payload_bytes, "
                    "updated_at=MAX(updated_at, excluded.updated_at)",
                    (
                        *_key_tuple(key),
                        int(record.error_code is not None),
                        record.latency_ms,
                        record.latency_ms,
                        record.portable_tokens,
                        record.payload_bytes,
                        _timestamp(updated_at),
                    ),
                )
        except sqlite3.Error as error:
            raise _observation_error("observability_persistence_failed") from error

    def snapshots(self, *, limit: int = 500) -> tuple[MetricSnapshot, ...]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT adapter, operation, provider_category, outcome, error_code, "
                    "count, error_count, latency_ms_total, latency_ms_max, "
                    "portable_tokens, payload_bytes, updated_at FROM observability_metrics "
                    "ORDER BY adapter, operation, provider_category, outcome, error_code LIMIT ?",
                    (_limit(limit),),
                ).fetchall()
        except sqlite3.Error as error:
            raise _observation_error("observability_persistence_failed") from error
        return tuple(_stored_snapshot(row) for row in rows)

    def cleanup_before(self, cutoff: float, *, limit: int = 500) -> int:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM observability_metrics WHERE rowid IN ("
                    "SELECT rowid FROM observability_metrics WHERE updated_at < ? "
                    "ORDER BY updated_at LIMIT ?)",
                    (_timestamp(cutoff), _limit(limit, maximum=10_000)),
                )
                return cursor.rowcount
        except sqlite3.Error as error:
            raise _observation_error("observability_retention_failed") from error

    def export_jsonl(self, *, limit: int = 500) -> str:
        envelope = {"schema": OBSERVABILITY_SCHEMA, "version": OBSERVABILITY_VERSION}
        lines = [json.dumps(envelope, sort_keys=True, separators=(",", ":"))]
        lines.extend(_metric_json(snapshot) for snapshot in self.snapshots(limit=limit))
        return "\n".join(lines) + "\n"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=self._timeout_seconds)
        connection.execute(f"PRAGMA busy_timeout = {int(self._timeout_seconds * 1_000)}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


def _snapshot(key: MetricKey, value: _MetricAccumulator) -> MetricSnapshot:
    return MetricSnapshot(
        key,
        value.count,
        value.error_count,
        value.latency_ms_total,
        value.latency_ms_max,
        value.portable_tokens,
        value.payload_bytes,
        value.updated_at,
    )


def _stored_snapshot(row: tuple[object, ...]) -> MetricSnapshot:
    try:
        key = MetricKey(*(str(value) for value in row[:5]))
        _validate_metric_key(key)
        snapshot = MetricSnapshot(
            key=key,
            count=_stored_int(row[5]),
            error_count=_stored_int(row[6]),
            latency_ms_total=_stored_float(row[7]),
            latency_ms_max=_stored_float(row[8]),
            portable_tokens=_stored_int(row[9]),
            payload_bytes=_stored_int(row[10]),
            updated_at=_stored_float(row[11]),
        )
        if any(
            value < 0
            for value in (
                snapshot.count,
                snapshot.error_count,
                snapshot.latency_ms_total,
                snapshot.latency_ms_max,
                snapshot.portable_tokens,
                snapshot.payload_bytes,
                snapshot.updated_at,
            )
        ):
            raise ValueError
        return snapshot
    except (TypeError, ValueError, OverflowError) as error:
        raise _observation_error("observability_persistence_corrupt") from error


def _metric_json(snapshot: MetricSnapshot) -> str:
    payload = {
        "adapter": snapshot.key.adapter,
        "count": snapshot.count,
        "error_code": snapshot.key.error_code,
        "error_count": snapshot.error_count,
        "latency_ms_max": snapshot.latency_ms_max,
        "latency_ms_total": snapshot.latency_ms_total,
        "operation": snapshot.key.operation,
        "outcome": snapshot.key.outcome,
        "payload_bytes": snapshot.payload_bytes,
        "portable_tokens": snapshot.portable_tokens,
        "provider_category": snapshot.key.provider_category,
        "updated_at": snapshot.updated_at,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _key_tuple(key: MetricKey) -> tuple[str, str, str, str, str]:
    return key.adapter, key.operation, key.provider_category, key.outcome, key.error_code


def _overflow_key() -> MetricKey:
    return MetricKey("other", "other", "other", "other", "other_error")


def _validate_metric_key(key: MetricKey) -> None:
    if key.adapter not in {item.value for item in AdapterCategory}:
        raise ValueError("metric adapter label is invalid")
    if key.provider_category not in {item.value for item in ProviderCategory}:
        raise ValueError("metric provider category label is invalid")
    if key.outcome not in {"success", "error", "other"}:
        raise ValueError("metric outcome label is invalid")
    if _LABEL.fullmatch(key.operation) is None or _LABEL.fullmatch(key.error_code) is None:
        raise ValueError("metric label is invalid")


def _validate_record(record: SpanRecord) -> None:
    if not _hex(record.correlation_id, 64) or not _hex(record.span_id, 32):
        raise ValueError("span identifiers are invalid")
    if record.parent_span_id is not None:
        _span_id(record.parent_span_id)
    if not math.isfinite(record.latency_ms) or record.latency_ms < 0:
        raise ValueError("span latency is invalid")
    _counter(record.portable_tokens, "portable_tokens")
    _counter(record.payload_bytes, "payload_bytes")


def _allowlist(values: Iterable[str], label: str) -> frozenset[str]:
    selected = frozenset(values)
    if len(selected) > 64 or any(
        not isinstance(value, str) or _LABEL.fullmatch(value) is None for value in selected
    ):
        raise ValueError(f"{label} allowlist is invalid or exceeds 64 entries")
    return selected


def _provider_category(value: ProviderCategory | str) -> ProviderCategory:
    try:
        return ProviderCategory(value)
    except ValueError:
        return ProviderCategory.OTHER


def _adapter(value: AdapterCategory | str) -> AdapterCategory:
    try:
        return AdapterCategory(value)
    except ValueError:
        return AdapterCategory.OTHER


def _counter(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _stored_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("stored metric integer is invalid")
    return value


def _stored_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("stored metric number is invalid")
    selected = float(value)
    if not math.isfinite(selected):
        raise ValueError("stored metric number is invalid")
    return selected


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timestamp must be a non-negative finite number")
    selected = float(value)
    if not math.isfinite(selected) or selected < 0:
        raise ValueError("timestamp must be a non-negative finite number")
    return selected


def _limit(value: int, *, maximum: int = 500) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _span_id(value: object) -> str:
    if not isinstance(value, str) or not _hex(value, 32):
        raise ValueError("parent_span_id must be a 128-bit hexadecimal identifier")
    return value


def _hex(value: str, length: int) -> bool:
    return len(value) == length and all(character in "0123456789abcdef" for character in value)


def _observation_error(code: str) -> CapabilityHubError:
    messages = {
        "observability_metric_invalid": "The observability metric is invalid.",
        "observability_persistence_corrupt": "Stored observability metrics are invalid.",
        "observability_persistence_failed": "Observability metrics could not be saved.",
        "observability_retention_failed": "Observability retention could not be applied.",
        "observability_span_already_finished": "The observation span is already finished.",
    }
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.INTERNAL,
        safe_message=messages[code],
        retryable=False,
    )
