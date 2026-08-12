from __future__ import annotations

import json

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
    ReasoningTier,
    SectionDescriptor,
    SideEffect,
)
from capabilityhub.orchestration import (
    AppliedReasoningRouter,
    ReasoningConstraints,
    ReasoningEndpoint,
    ReasoningWorkload,
)
from capabilityhub.protocol import AdapterKind, RequestEnvelope, parse_request
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext
from capabilityhub.service_adapter import CapabilityHubServiceAdapter


def _setup(kind: AdapterKind = AdapterKind.LIBRARY) -> CapabilityHubServiceAdapter:
    manifest = CapabilityManifest(
        identity=CapabilityIdentity("test", "api", "1.0.0", "adapter-revision"),
        kind=CapabilityKind.API,
        summary="A fixture that finds records.",
        provider="fixture",
        operations=(OperationSpec("find", OperationType.EXECUTE),),
        sections=(SectionDescriptor("contract", "text/plain", "find contract", 3),),
    )
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    provider = StaticProvider(
        (StaticFixture(manifest, {"find": {"items": [1]}}),),
        name="fixture",
    )
    service = CapabilityHubService(
        registry=registry,
        providers=(provider,),
        references=ReferenceSigner(b"service-adapter-key", clock=lambda: 100),
        audit=MemoryAuditSink(),
    )
    budgets: dict[str, BudgetLedger] = {}

    def budget_provider(task_id: str) -> BudgetLedger:
        return budgets.setdefault(
            task_id,
            BudgetLedger(
                task_id,
                {"bytes": 50_000, "loads": 10, "executions": 10, "portable_tokens": 10_000},
            ),
        )

    return CapabilityHubServiceAdapter(
        service,
        kind=kind,
        context_provider=lambda: ServiceContext("tenant", "principal", "session"),
        budget_provider=budget_provider,
        inventory_provider=lambda: {"total": 1, "categories": {"api": 1}},
    )


def _request(
    adapter: CapabilityHubServiceAdapter,
    operation: str,
    payload: dict[str, object],
) -> RequestEnvelope:
    handshake = adapter.handshake
    return parse_request(
        adapter.kind,
        {
            "request_id": "request-1",
            "correlation_id": "correlation-1",
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


@pytest.mark.parametrize("kind", list(AdapterKind))
def test_real_service_roundtrip_uses_same_logic_for_every_adapter_kind(kind: AdapterKind) -> None:
    adapter = _setup(kind)
    search = adapter.dispatch(
        _request(
            adapter,
            "capability.search",
            {
                "query": "records",
                "task_id": "task",
                "kinds": ["api"],
                "include_inventory": True,
            },
        )
    )
    assert isinstance(search, dict)
    assert search["inventory"] == {"total": 1, "categories": {"api": 1}}
    cards = search["cards"]
    assert isinstance(cards, list)
    capability_ref = cards[0]["capability_ref"]

    loaded = adapter.dispatch(
        _request(
            adapter,
            "capability.load",
            {
                "capability_ref": capability_ref,
                "task_id": "task",
                "section_names": ["contract"],
                "operation_names": ["find"],
            },
        )
    )
    assert isinstance(loaded, dict)
    assert loaded["sections"] == [
        {
            "content": "find contract",
            "media_type": "text/plain",
            "name": "contract",
            "portable_tokens": 3,
            "sensitive": False,
        }
    ]

    executed = adapter.dispatch(
        _request(
            adapter,
            "capability.execute",
            {
                "execution_ref": loaded["execution_ref"],
                "operation": "find",
                "arguments": {},
                "task_id": "task",
            },
        )
    )
    assert isinstance(executed, dict)
    assert executed["output"] == {"items": [1]}
    json.dumps(executed)


def test_http_shared_adapter_propagates_idempotency_key_for_result_reuse() -> None:
    adapter = _setup(AdapterKind.HTTP)
    search = adapter.dispatch(
        _request(
            adapter,
            "capability.search",
            {"query": "records", "task_id": "task", "kinds": ["api"]},
        )
    )
    assert isinstance(search, dict)
    cards = search["cards"]
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
    payload = {
        "execution_ref": loaded["execution_ref"],
        "operation": "find",
        "arguments": {},
        "task_id": "task",
        "idempotency_key": "http-shared-key",
    }

    first = adapter.dispatch(_request(adapter, "capability.execute", payload))
    replay = adapter.dispatch(_request(adapter, "capability.execute", payload))

    assert replay == first


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        ("capability.search", {"query": "records", "task_id": "task", "extra": "secret"}),
        ("capability.search", {"query": "records"}),
        ("capability.search", {"query": "records", "task_id": "task", "limit": True}),
        (
            "capability.execute",
            {
                "execution_ref": "ref",
                "operation": "find",
                "arguments": {"value": float("nan")},
                "task_id": "task",
            },
        ),
        (
            "capability.load",
            {"capability_ref": "ref", "task_id": "task", "section_names": "contract"},
        ),
        (
            "capability.execute",
            {"execution_ref": "ref", "operation": "find", "arguments": [], "task_id": "task"},
        ),
    ],
)
def test_exact_payload_boundary_rejects_unknown_missing_and_wrong_types(
    operation: str,
    payload: dict[str, object],
) -> None:
    adapter = _setup()
    with pytest.raises(CapabilityHubError) as caught:
        adapter.dispatch(_request(adapter, operation, payload))
    assert caught.value.code == "invalid_adapter_payload"
    assert caught.value.category is ErrorCategory.INPUT
    assert caught.value.details == {}
    assert "secret" not in str(caught.value)


def test_service_typed_error_propagates_without_adapter_rewriting() -> None:
    adapter = _setup()
    with pytest.raises(CapabilityHubError) as caught:
        adapter.dispatch(
            _request(
                adapter,
                "capability.load",
                {"capability_ref": "not-a-reference", "task_id": "task"},
            )
        )
    assert caught.value.category is ErrorCategory.REFERENCE
    assert caught.value.code != "adapter_provider_failed"


def test_empty_search_query_remains_available_for_inventory_only_requests() -> None:
    adapter = _setup()
    result = adapter.dispatch(
        _request(
            adapter,
            "capability.search",
            {
                "query": "",
                "task_id": "inventory-task",
                "include_inventory": True,
                "include_cards": False,
            },
        )
    )

    assert isinstance(result, dict)
    assert result["cards"] == []
    assert result["inventory"] == {"total": 1, "categories": {"api": 1}}


def test_provider_failures_are_typed_and_redacted() -> None:
    base = _setup()

    def fail() -> ServiceContext:
        raise RuntimeError("private tenant credential")

    adapter = CapabilityHubServiceAdapter(
        base._service,
        kind=AdapterKind.LIBRARY,
        context_provider=fail,
        budget_provider=lambda task_id: BudgetLedger(task_id, {"portable_tokens": 100}),
    )
    with pytest.raises(CapabilityHubError) as caught:
        adapter.dispatch(
            _request(
                adapter,
                "capability.search",
                {"query": "records", "task_id": "task"},
            )
        )
    assert caught.value.code == "adapter_provider_failed"
    assert caught.value.category is ErrorCategory.INTERNAL
    assert "credential" not in str(caught.value)
    assert caught.value.details == {}


def test_adapter_kind_mismatch_is_rejected() -> None:
    adapter = _setup(AdapterKind.HTTP)
    request = _request(adapter, "capability.search", {"query": "records", "task_id": "task"})
    mismatched = RequestEnvelope(
        adapter=AdapterKind.CLI,
        request_id=request.request_id,
        correlation_id=request.correlation_id,
        operation=request.operation,
        payload=request.payload,
        handshake=request.handshake,
        negotiation=request.negotiation,
    )
    with pytest.raises(CapabilityHubError, match="different adapter kind"):
        adapter.dispatch(mismatched)


def test_cancellation_is_explicitly_unsupported_or_delegated() -> None:
    adapter = _setup()
    with pytest.raises(CapabilityHubError) as caught:
        adapter.cancel("correlation-1")
    assert caught.value.code == "cancellation_unsupported"

    cancelled: list[str] = []
    delegated = CapabilityHubServiceAdapter(
        adapter._service,
        kind=AdapterKind.LIBRARY,
        context_provider=lambda: ServiceContext("tenant", "principal", "session"),
        budget_provider=lambda task_id: BudgetLedger(task_id, {"portable_tokens": 100}),
        cancel_callback=lambda correlation_id: not cancelled.append(correlation_id),
    )
    assert delegated.cancel("correlation-2") is True
    assert cancelled == ["correlation-2"]


def test_shared_adapter_applies_task_tier_without_spending_reasoning_budget() -> None:
    base = _setup()
    budget = BudgetLedger(
        "reasoning-task",
        {
            "bytes": 50_000,
            "loads": 10,
            "executions": 10,
            "portable_tokens": 10_000,
            "reasoning_tokens": 2_000,
        },
    )
    router = AppliedReasoningRouter(
        budget_provider=lambda _task_id: budget,
        endpoints=(
            ReasoningEndpoint("low", ReasoningTier.LOW),
            ReasoningEndpoint("medium", ReasoningTier.MEDIUM),
            ReasoningEndpoint("high", ReasoningTier.HIGH),
        ),
    )
    adapter = CapabilityHubServiceAdapter(
        base._service,
        kind=AdapterKind.LIBRARY,
        context_provider=lambda: ServiceContext("tenant", "principal", "session"),
        budget_provider=lambda _task_id: budget,
        reasoning_router=router,
        reasoning_workload_provider=lambda _operation, _payload: ReasoningWorkload(
            dependency_count=3
        ),
        reasoning_constraints_provider=lambda _request: ReasoningConstraints(
            maximum_tier=ReasoningTier.MEDIUM
        ),
    )

    result = adapter.dispatch(
        _request(
            adapter,
            "capability.search",
            {"query": "records", "task_id": "reasoning-task"},
        )
    )

    assert isinstance(result, dict)
    assert adapter.reasoning_state("reasoning-task") == {
        "application": {
            "applied": True,
            "error_code": None,
            "outcome": "success",
        },
        "budget_remaining": 2_000,
        "endpoint": "medium",
        "estimated_tokens": 1_024,
        "operation": "capability.search",
        "reason_codes": [
            "policy_floor:medium",
            "cheapest_eligible",
            "dependency_floor:medium",
            "endpoint_eligible",
        ],
        "should_stop": False,
        "tier": "medium",
    }
    assert budget.snapshot().used["reasoning_tokens"] == 0


def test_shared_adapter_fails_before_service_when_tier_is_unaffordable() -> None:
    base = _setup()
    budget = BudgetLedger(
        "budget-small",
        {
            "bytes": 50_000,
            "loads": 10,
            "executions": 10,
            "portable_tokens": 10_000,
            "reasoning_tokens": 500,
        },
    )
    adapter = CapabilityHubServiceAdapter(
        base._service,
        kind=AdapterKind.LIBRARY,
        context_provider=lambda: ServiceContext("tenant", "principal", "session"),
        budget_provider=lambda _task_id: budget,
        reasoning_workload_provider=lambda _operation, _payload: ReasoningWorkload(
            risk=SideEffect.REVERSIBLE_WRITE
        ),
    )

    with pytest.raises(CapabilityHubError) as caught:
        adapter.dispatch(
            _request(
                adapter,
                "capability.search",
                {"query": "records", "task_id": "budget-small"},
            )
        )

    assert caught.value.code == "no_eligible_reasoning_tier"
    assert budget.snapshot().used["portable_tokens"] == 0


def test_shared_adapter_escalates_after_typed_failure_then_stops_repeat() -> None:
    adapter = _setup(AdapterKind.CLI)
    payload = {"capability_ref": "invalid", "task_id": "failed-task"}

    with pytest.raises(CapabilityHubError):
        adapter.dispatch(_request(adapter, "capability.load", payload))
    with pytest.raises(CapabilityHubError):
        adapter.dispatch(_request(adapter, "capability.load", payload))
    state = adapter.reasoning_state("failed-task")
    assert state is not None
    assert state["tier"] == "medium"
    assert "escalated:previous_typed_failure" in state["reason_codes"]

    with pytest.raises(CapabilityHubError) as stopped:
        adapter.dispatch(_request(adapter, "capability.load", payload))
    assert stopped.value.code == "reasoning_no_progress"


def test_reasoning_dependency_failure_is_redacted_by_shared_adapter() -> None:
    base = _setup()

    def fail_budget(_task_id: str) -> BudgetLedger:
        raise RuntimeError("SECRET reasoning backend credential")

    adapter = CapabilityHubServiceAdapter(
        base._service,
        kind=AdapterKind.LIBRARY,
        context_provider=lambda: ServiceContext("tenant", "principal", "session"),
        budget_provider=fail_budget,
    )

    with pytest.raises(CapabilityHubError) as caught:
        adapter.dispatch(
            _request(
                adapter,
                "capability.search",
                {"query": "records", "task_id": "failed-budget"},
            )
        )

    assert caught.value.code == "adapter_provider_failed"
    assert "SECRET" not in str(caught.value)
