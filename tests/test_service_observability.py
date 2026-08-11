from __future__ import annotations

from dataclasses import dataclass

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
    SectionDescriptor,
)
from capabilityhub.observability import DeterministicSampler, InMemoryObservability
from capabilityhub.protocol import AdapterKind, RequestEnvelope, parse_request
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext
from capabilityhub.service_adapter import CapabilityHubServiceAdapter


@dataclass
class _Setup:
    service: CapabilityHubService
    observer: InMemoryObservability | None
    budgets: dict[str, BudgetLedger]

    def adapter(self, kind: AdapterKind) -> CapabilityHubServiceAdapter:
        return CapabilityHubServiceAdapter(
            self.service,
            kind=kind,
            context_provider=lambda: ServiceContext("RAW-TENANT", "RAW-PRINCIPAL", "RAW-SESSION"),
            budget_provider=self._budget,
            observability=self.observer,
        )

    def _budget(self, task_id: str) -> BudgetLedger:
        return self.budgets.setdefault(
            task_id,
            BudgetLedger(
                task_id,
                {"bytes": 100_000, "loads": 10, "executions": 10, "portable_tokens": 100_000},
            ),
        )


def _setup(observer: InMemoryObservability | None) -> _Setup:
    manifest = CapabilityManifest(
        identity=CapabilityIdentity(
            "RAW-NAMESPACE",
            "private-api",
            "1",
            "RAW-REVISION-PATH-C:/private",
        ),
        kind=CapabilityKind.API,
        summary="Private fixture records",
        provider="RAW-PROVIDER-IDENTITY",
        operations=(OperationSpec("find", OperationType.EXECUTE),),
        sections=(SectionDescriptor("contract", "text/plain", "SECRET-CONTRACT", 2),),
    )
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    provider = StaticProvider(
        (StaticFixture(manifest, {"find": {"SECRET-OUTPUT": "https://private/path"}}),),
        name="RAW-PROVIDER-IDENTITY",
    )
    service = CapabilityHubService(
        registry=registry,
        providers=(provider,),
        references=ReferenceSigner(b"service-observability-reference-key"),
        audit=MemoryAuditSink(),
    )
    return _Setup(service, observer, {})


def _request(
    adapter: CapabilityHubServiceAdapter,
    operation: str,
    payload: dict[str, object],
    *,
    correlation_id: str = "RAW-CORRELATION-SECRET",
) -> RequestEnvelope:
    handshake = adapter.handshake
    return parse_request(
        adapter.kind,
        {
            "request_id": "request",
            "correlation_id": correlation_id,
            "operation": operation,
            "payload": payload,
            "handshake": {
                "api_versions": list(handshake.api_versions),
                "supported_features": list(handshake.supported_features),
                "required_features": list(handshake.required_features),
            },
        },
        server_handshake=handshake,
    )


def test_real_search_load_execute_emit_only_privacy_safe_observations() -> None:
    observer = InMemoryObservability(sampler=DeterministicSampler(1))
    adapter = _setup(observer).adapter(AdapterKind.HTTP)
    searched = adapter.dispatch(
        _request(
            adapter,
            "capability.search",
            {"query": "records", "task_id": "task"},
        )
    )
    assert isinstance(searched, dict)
    cards = searched["cards"]
    assert isinstance(cards, list)
    loaded = adapter.dispatch(
        _request(
            adapter,
            "capability.load",
            {
                "capability_ref": cards[0]["capability_ref"],
                "task_id": "task",
                "operation_names": ["find"],
            },
        )
    )
    assert isinstance(loaded, dict)
    executed = adapter.dispatch(
        _request(
            adapter,
            "capability.execute",
            {
                "execution_ref": loaded["execution_ref"],
                "operation": "find",
                "arguments": {
                    "secret": "SECRET-ARGUMENT",
                    "url": "https://private.example/path",
                    "path": "C:/private/file",
                },
                "task_id": "task",
            },
        )
    )
    assert isinstance(executed, dict)
    assert executed["output"] == {"SECRET-OUTPUT": "https://private/path"}

    assert [span.operation for span in observer.spans] == ["search", "load", "execute"]
    exported = observer.export_jsonl()
    serialized_spans = repr(observer.spans)
    for forbidden in (
        "RAW-CORRELATION-SECRET",
        "RAW-TENANT",
        "RAW-PRINCIPAL",
        "RAW-SESSION",
        "RAW-PROVIDER-IDENTITY",
        "RAW-REVISION",
        "SECRET-ARGUMENT",
        "SECRET-OUTPUT",
        "https://private",
        "C:/private",
    ):
        assert forbidden not in exported
        assert forbidden not in serialized_spans


def test_same_correlation_keeps_trace_identity_across_adapter_kinds() -> None:
    observer = InMemoryObservability(sampler=DeterministicSampler(1))
    setup = _setup(observer)
    cli = setup.adapter(AdapterKind.CLI)
    mcp = setup.adapter(AdapterKind.MCP)

    for adapter, task in ((cli, "cli-task"), (mcp, "mcp-task")):
        adapter.dispatch(
            _request(
                adapter,
                "capability.search",
                {"query": "records", "task_id": task},
                correlation_id="shared-raw-correlation",
            )
        )

    assert len(observer.spans) == 2
    assert observer.spans[0].correlation_id == observer.spans[1].correlation_id
    assert {span.adapter.value for span in observer.spans} == {"cli", "mcp"}
    assert "shared-raw-correlation" not in repr(observer.spans)


def test_service_error_is_force_sampled_and_original_typed_error_is_unchanged() -> None:
    observer = InMemoryObservability(
        sampler=DeterministicSampler(0),
        allowed_error_codes=("invalid_reference",),
    )
    adapter = _setup(observer).adapter(AdapterKind.LIBRARY)

    with pytest.raises(CapabilityHubError) as caught:
        adapter.dispatch(
            _request(
                adapter,
                "capability.load",
                {"capability_ref": "not-a-reference", "task_id": "task"},
            )
        )

    assert caught.value.code == "invalid_reference"
    assert len(observer.spans) == 1
    assert observer.spans[0].operation == "load"
    assert observer.spans[0].error_code == "invalid_reference"
    assert observer.metric_snapshots()[0].error_count == 1


def test_observability_start_and_finish_failures_never_change_business_result() -> None:
    class _BrokenObserver:
        def start_span(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("SECRET-OBSERVER-FAILURE")

    setup = _setup(None)
    adapter = CapabilityHubServiceAdapter(
        setup.service,
        kind=AdapterKind.LIBRARY,
        context_provider=lambda: ServiceContext("tenant", "principal", "session"),
        budget_provider=setup._budget,
        observability=_BrokenObserver(),  # type: ignore[arg-type]
    )

    result = adapter.dispatch(
        _request(
            adapter,
            "capability.search",
            {"query": "records", "task_id": "task"},
        )
    )
    assert isinstance(result, dict)
    assert result["total_matches"] == 1

    class _BrokenStore:
        def increment(self, key, record, *, updated_at):
            del key, record, updated_at
            raise RuntimeError("SECRET-PERSISTENCE-FAILURE")

    finish_setup = _setup(
        InMemoryObservability(
            sampler=DeterministicSampler(1),
            persistent_metrics=_BrokenStore(),
        )
    )
    finish_adapter = finish_setup.adapter(AdapterKind.LIBRARY)
    finish_result = finish_adapter.dispatch(
        _request(
            finish_adapter,
            "capability.search",
            {"query": "records", "task_id": "finish-task"},
        )
    )
    assert isinstance(finish_result, dict)
    assert finish_result["total_matches"] == 1


def test_unconfigured_adapter_performs_no_observability_work(monkeypatch) -> None:
    adapter = _setup(None).adapter(AdapterKind.LIBRARY)

    def unexpected(*args, **kwargs):
        del args, kwargs
        raise AssertionError("observability should not run")

    monkeypatch.setattr("capabilityhub.service_adapter.TraceContext.from_correlation", unexpected)
    result = adapter.dispatch(
        _request(
            adapter,
            "capability.search",
            {"query": "records", "task_id": "task"},
        )
    )

    assert isinstance(result, dict)
    assert result["total_matches"] == 1
