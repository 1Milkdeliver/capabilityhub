from __future__ import annotations

from dataclasses import replace

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
    OperationSpec,
    OperationType,
    SectionDescriptor,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext


def _manifest(*, kind: CapabilityKind = CapabilityKind.API) -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity("test", kind.value, "1.0.0", f"digest-{kind.value}"),
        kind=kind,
        summary=f"A {kind.value} fixture for records.",
        provider="fixture",
        operations=(
            OperationSpec("find", OperationType.EXECUTE),
            OperationSpec("count", OperationType.EXECUTE),
        ),
        sections=(
            SectionDescriptor("contract", "text/plain", "contract text", 4),
            SectionDescriptor("examples", "text/plain", "example text", 3),
        ),
    )


def _setup(
    manifest: CapabilityManifest | None = None,
    *,
    outputs: dict[str, JsonValue] | None = None,
    provider_type: type[StaticProvider] = StaticProvider,
) -> tuple[CapabilityHubService, ServiceContext, BudgetLedger, MemoryAuditSink]:
    selected = manifest or _manifest()
    registry = CapabilityRegistry()
    registry.register(selected)
    registry.activate(selected.identity.coordinate, selected.identity.revision)
    fixture_outputs: dict[str, JsonValue] = (
        outputs if outputs is not None else {"find": {"items": [1]}, "count": 1}
    )
    provider = provider_type((StaticFixture(selected, fixture_outputs),), name="fixture")
    audit = MemoryAuditSink()
    service = CapabilityHubService(
        registry=registry,
        providers=(provider,),
        references=ReferenceSigner(b"service-test-key", clock=lambda: 100),
        audit=audit,
    )
    context = ServiceContext("tenant", "principal", "session", max_output_tokens=1_000)
    budget = BudgetLedger(
        "task",
        {"bytes": 50_000, "loads": 10, "executions": 10, "portable_tokens": 10_000},
    )
    return service, context, budget, audit


class MisreportingProvider(StaticProvider):
    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        return replace(super().execute(identity, request, context), portable_tokens=0)


class FailingProvider(StaticProvider):
    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        raise RuntimeError("private provider diagnostic")


def _search_and_load(
    service: CapabilityHubService,
    context: ServiceContext,
    budget: BudgetLedger,
    *,
    operations: tuple[str, ...] = ("find",),
) -> tuple[str, str]:
    search = service.search(
        "records", task_id="task", context=context, budget=budget, max_output_tokens=2_000
    )
    loaded = service.load(
        search.cards[0].capability_ref,
        task_id="task",
        context=context,
        budget=budget,
        section_names=("contract",),
        operation_names=operations,
        max_output_tokens=2_000,
    )
    return loaded.revision, loaded.execution_ref


def test_three_tool_flow_loads_only_selected_material_and_executes_named_provider() -> None:
    service, context, budget, audit = _setup()
    revision, execution_ref = _search_and_load(service, context, budget)
    result = service.execute(
        ExecutionRequest(execution_ref, "find", {"query": "x"}, "task"),
        context=context,
        budget=budget,
    )

    assert result.capability_revision == revision
    assert result.provider == "fixture"
    assert result.output == {"items": [1]}
    assert [event.event_type for event in audit.events] == ["search", "load", "execute"]
    assert all(event.outcome == "success" for event in audit.events)
    snapshot = budget.snapshot()
    assert snapshot.used["loads"] == 1
    assert snapshot.used["executions"] == 1


def test_execute_rejects_an_operation_not_selected_by_load_before_budget_use() -> None:
    service, context, budget, audit = _setup()
    _, execution_ref = _search_and_load(service, context, budget, operations=("find",))

    with pytest.raises(CapabilityHubError) as raised:
        service.execute(
            ExecutionRequest(execution_ref, "count", {}, "task"),
            context=context,
            budget=budget,
        )
    assert raised.value.code == "operation_not_loaded"
    assert budget.snapshot().used["executions"] == 0
    assert audit.events[-1].outcome == "failure"


def test_load_ref_is_scope_bound_and_tamper_rejected() -> None:
    service, context, budget, _ = _setup()
    response = service.search(
        "records", task_id="task", context=context, budget=budget, max_output_tokens=2_000
    )
    other = ServiceContext("tenant", "other", "session")
    with pytest.raises(CapabilityHubError) as raised:
        service.load(
            response.cards[0].capability_ref,
            task_id="task",
            context=other,
            budget=budget,
        )
    assert raised.value.code == "reference_scope_mismatch"


def test_skill_content_is_load_only_and_receives_no_execution_ref() -> None:
    service, context, budget, _ = _setup(_manifest(kind=CapabilityKind.SKILL))
    search = service.search(
        "records", task_id="task", context=context, budget=budget, max_output_tokens=2_000
    )
    loaded = service.load(
        search.cards[0].capability_ref,
        task_id="task",
        context=context,
        budget=budget,
        section_names=("contract",),
        max_output_tokens=2_000,
    )
    assert loaded.sections[0].content == "contract text"
    assert loaded.execution_ref == ""


def test_load_hard_budget_fails_without_charging_a_load() -> None:
    service, context, budget, audit = _setup()
    response = service.search(
        "records", task_id="task", context=context, budget=budget, max_output_tokens=2_000
    )
    with pytest.raises(CapabilityHubError) as raised:
        service.load(
            response.cards[0].capability_ref,
            task_id="task",
            context=context,
            budget=budget,
            max_output_tokens=1,
        )
    assert raised.value.code == "load_output_budget_exceeded"
    assert budget.snapshot().used["loads"] == 0
    assert audit.events[-1].outcome == "failure"


def test_provider_output_over_hard_limit_is_not_returned_but_attempt_is_charged() -> None:
    service, context, budget, audit = _setup(outputs={"find": "x" * 1_000, "count": 1})
    _, execution_ref = _search_and_load(service, context, budget)
    with pytest.raises(CapabilityHubError) as raised:
        service.execute(
            ExecutionRequest(execution_ref, "find", {}, "task"),
            context=context,
            budget=budget,
            max_output_tokens=10,
        )
    assert raised.value.code == "provider_output_budget_exceeded"
    assert budget.snapshot().used["executions"] == 1
    assert audit.events[-1].reason_codes == ("provider_output_budget_exceeded",)


def test_service_recalculates_untrusted_provider_token_count() -> None:
    service, context, budget, _ = _setup(provider_type=MisreportingProvider)
    _, execution_ref = _search_and_load(service, context, budget)

    result = service.execute(
        ExecutionRequest(execution_ref, "find", {}, "task"),
        context=context,
        budget=budget,
    )

    assert result.portable_tokens > 0


def test_unhandled_provider_error_is_wrapped_in_safe_contract() -> None:
    service, context, budget, audit = _setup(provider_type=FailingProvider)
    _, execution_ref = _search_and_load(service, context, budget)

    with pytest.raises(CapabilityHubError) as raised:
        service.execute(
            ExecutionRequest(execution_ref, "find", {}, "task"),
            context=context,
            budget=budget,
        )

    assert raised.value.code == "provider_unhandled_error"
    assert "private provider diagnostic" not in raised.value.safe_message
    assert audit.events[-1].reason_codes == ("provider_unhandled_error",)


def test_catalog_fork_preserves_execution_grants_and_audit_order() -> None:
    manifest = _manifest()
    service, context, budget, audit = _setup(manifest)
    _, execution_ref = _search_and_load(service, context, budget)
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    provider = StaticProvider(
        (StaticFixture(manifest, {"find": {"items": [2]}, "count": 2}),),
        name="fixture",
    )

    refreshed = service.fork_catalog(registry=registry, providers=(provider,))
    result = refreshed.execute(
        ExecutionRequest(execution_ref, "find", {}, "task"),
        context=context,
        budget=budget,
    )

    assert result.output == {"items": [2]}
    assert [event.sequence for event in audit.events] == [1, 2, 3]


def test_sensitive_section_requires_explicit_permission() -> None:
    manifest = replace(
        _manifest(),
        sections=(SectionDescriptor("secret", "text/plain", "sensitive", 3, sensitive=True),),
    )
    service, context, budget, _ = _setup(manifest)
    search = service.search(
        "records", task_id="task", context=context, budget=budget, max_output_tokens=2_000
    )

    with pytest.raises(CapabilityHubError) as denied:
        service.load(
            search.cards[0].capability_ref,
            task_id="task",
            context=context,
            budget=budget,
            section_names=("secret",),
        )
    assert denied.value.code == "sensitive_section_denied"

    allowed_context = replace(context, granted_permissions=frozenset({"content.sensitive"}))
    allowed_search = service.search(
        "records",
        task_id="task",
        context=allowed_context,
        budget=budget,
        max_output_tokens=2_000,
    )
    loaded = service.load(
        allowed_search.cards[0].capability_ref,
        task_id="task",
        context=allowed_context,
        budget=budget,
        section_names=("secret",),
    )
    assert loaded.sections[0].sensitive
