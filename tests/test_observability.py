from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

import pytest

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.observability import (
    OBSERVABILITY_SCHEMA,
    AdapterCategory,
    DeterministicSampler,
    InMemoryObservability,
    ProviderCategory,
    SqliteMetricStore,
    TraceContext,
)


@dataclass
class _Clock:
    now: float = 0

    def __call__(self) -> float:
        return self.now


def test_trace_context_hashes_correlation_and_survives_adapter_handoff() -> None:
    raw = "SECRET-RAW-IDENTITY-correlation"
    initial = TraceContext.from_correlation(raw, AdapterCategory.CLI)
    handed_off = initial.for_adapter(AdapterCategory.MCP, parent_span_id="a" * 32)

    assert initial.correlation_id == handed_off.correlation_id
    assert initial.adapter is AdapterCategory.CLI
    assert handed_off.adapter is AdapterCategory.MCP
    assert handed_off.parent_span_id == "a" * 32
    assert raw not in repr(initial)


def test_span_has_only_allowlisted_privacy_safe_fields_and_counters() -> None:
    clock = _Clock(1)
    observer = InMemoryObservability(
        allowed_operations=("execute",),
        allowed_error_codes=("provider_timeout",),
        sampler=DeterministicSampler(1),
        clock=clock,
        wall_clock=lambda: 10,
    )
    context = TraceContext.from_correlation("raw-correlation", "http")
    span = observer.start_span(
        context,
        operation="execute",
        provider_category=ProviderCategory.API,
    )
    clock.now = 1.125
    record = span.finish(portable_tokens=7, payload_bytes=31)

    assert record.latency_ms == 125
    assert record.portable_tokens == 7
    assert record.payload_bytes == 31
    assert record.error_code is None
    assert set(asdict(record)) == {
        "adapter",
        "correlation_id",
        "error_code",
        "latency_ms",
        "operation",
        "parent_span_id",
        "payload_bytes",
        "portable_tokens",
        "provider_category",
        "span_id",
    }
    serialized = str(asdict(record))
    for forbidden in ("arguments", "output", "secret", "https://", "C:\\"):
        assert forbidden not in serialized


def test_unknown_labels_collapse_and_errors_are_always_sampled() -> None:
    observer = InMemoryObservability(
        allowed_operations=("execute",),
        allowed_error_codes=("known_error",),
        sampler=DeterministicSampler(0),
    )
    context = TraceContext.from_correlation("correlation", "SECRET-ADAPTER")
    record = observer.start_span(
        context,
        operation="https://secret.example/private/path",
        provider_category="SECRET-PROVIDER-NAME",
    ).finish(error_code="SECRET-ERROR-CANARY")

    assert record.adapter is AdapterCategory.OTHER
    assert record.operation == "other"
    assert record.provider_category is ProviderCategory.OTHER
    assert record.error_code == "other_error"
    assert observer.spans == (record,)
    assert "SECRET" not in observer.export_jsonl()


def test_success_sampling_is_deterministic_and_metrics_include_unsampled_spans() -> None:
    context = TraceContext.from_correlation("same-correlation", "library")
    sampler = DeterministicSampler(0.5)
    assert sampler.selected(context.correlation_id, has_error=False) == sampler.selected(
        context.correlation_id, has_error=False
    )

    observer = InMemoryObservability(sampler=DeterministicSampler(0))
    observer.start_span(
        context, operation="execute", provider_category=ProviderCategory.API
    ).finish(portable_tokens=2)

    assert observer.spans == ()
    metrics = observer.metric_snapshots()
    assert len(metrics) == 1
    assert metrics[0].count == 1
    assert metrics[0].portable_tokens == 2


def test_span_and_metric_cardinality_are_strictly_bounded() -> None:
    observer = InMemoryObservability(
        allowed_operations=("search", "load", "execute"),
        sampler=DeterministicSampler(1),
        span_limit=2,
        metric_series_limit=2,
    )
    context = TraceContext.from_correlation("correlation", "cli")
    for operation in ("search", "load", "execute"):
        observer.start_span(
            context, operation=operation, provider_category=ProviderCategory.API
        ).finish()

    assert len(observer.spans) == 2
    metrics = observer.metric_snapshots()
    assert len(metrics) == 2
    assert sum(metric.count for metric in metrics) == 3
    assert any(metric.key.operation == "other" for metric in metrics)


def test_sqlite_aggregate_export_and_bounded_retention_are_safe(tmp_path) -> None:
    path = tmp_path / "metrics.sqlite3"
    wall = _Clock(10)
    store = SqliteMetricStore(path)
    observer = InMemoryObservability(
        allowed_operations=("search", "execute"),
        persistent_metrics=store,
        sampler=DeterministicSampler(0),
        wall_clock=wall,
    )
    context = TraceContext.from_correlation("SECRET-CORRELATION", "mcp")
    observer.start_span(context, operation="search", provider_category=ProviderCategory.RAG).finish(
        payload_bytes=20
    )
    wall.now = 20
    observer.start_span(
        context, operation="execute", provider_category=ProviderCategory.API
    ).finish(error_code="SECRET-ERROR")

    snapshots = store.snapshots()
    assert len(snapshots) == 2
    assert sum(item.count for item in snapshots) == 2
    exported = store.export_jsonl()
    assert OBSERVABILITY_SCHEMA in exported
    assert "SECRET-CORRELATION" not in exported
    assert "SECRET-ERROR" not in exported
    assert "arguments" not in exported
    assert store.cleanup_before(15, limit=1) == 1
    assert len(store.snapshots()) == 1

    persisted = path.read_bytes()
    assert b"SECRET-CORRELATION" not in persisted
    assert b"SECRET-ERROR" not in persisted


def test_concurrent_recording_is_lossless_for_metrics_and_bounded_for_spans() -> None:
    observer = InMemoryObservability(
        sampler=DeterministicSampler(1),
        span_limit=25,
        metric_series_limit=10,
    )
    context = TraceContext.from_correlation("concurrent", "http")

    def record(index: int) -> None:
        observer.start_span(
            context,
            operation="execute",
            provider_category=ProviderCategory.API,
        ).finish(portable_tokens=index % 3, payload_bytes=1)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(record, range(200)))

    metrics = observer.metric_snapshots()
    assert len(metrics) == 1
    assert metrics[0].count == 200
    assert metrics[0].payload_bytes == 200
    assert len(observer.spans) == 25


def test_corrupt_persistence_and_sink_failures_are_stably_redacted(tmp_path) -> None:
    path = tmp_path / "metrics.sqlite3"
    store = SqliteMetricStore(path)
    observer = InMemoryObservability(persistent_metrics=store)
    observer.start_span(
        TraceContext.from_correlation("correlation", "cli"),
        operation="execute",
        provider_category=ProviderCategory.CLI,
    ).finish()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE observability_metrics SET count = ?",
            ("SECRET-CORRUPTION",),
        )

    with pytest.raises(CapabilityHubError) as corrupt:
        store.snapshots()
    assert corrupt.value.code == "observability_persistence_corrupt"
    assert corrupt.value.category is ErrorCategory.INTERNAL
    assert "SECRET-CORRUPTION" not in str(corrupt.value.as_dict())

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE observability_metrics SET operation = ?",
            ("https://SECRET.example/private",),
        )
    with pytest.raises(CapabilityHubError) as unsafe_export:
        store.export_jsonl()
    assert unsafe_export.value.code == "observability_persistence_corrupt"
    assert "SECRET" not in str(unsafe_export.value.as_dict())

    class _BrokenStore:
        def increment(self, key, record, *, updated_at):
            del key, record, updated_at
            raise RuntimeError("SECRET-SINK-FAILURE")

    broken = InMemoryObservability(persistent_metrics=_BrokenStore())
    with pytest.raises(CapabilityHubError) as failed:
        broken.start_span(
            TraceContext.from_correlation("correlation", "cli"),
            operation="execute",
            provider_category=ProviderCategory.CLI,
        ).finish()
    assert failed.value.code == "observability_persistence_failed"
    assert "SECRET-SINK-FAILURE" not in str(failed.value.as_dict())
    assert broken.health().status == "degraded"
    assert broken.health().failure_count == 1


def test_span_finish_is_one_shot_and_label_allowlists_are_validated() -> None:
    observer = InMemoryObservability()
    handle = observer.start_span(
        TraceContext.from_correlation("correlation", "cli"),
        operation="execute",
        provider_category=ProviderCategory.CLI,
    )
    handle.finish()
    with pytest.raises(CapabilityHubError) as caught:
        handle.finish()
    assert caught.value.code == "observability_span_already_finished"

    with pytest.raises(ValueError, match="allowlist"):
        InMemoryObservability(allowed_operations=("https://secret/path",))
