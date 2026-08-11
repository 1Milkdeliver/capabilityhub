from __future__ import annotations

from dataclasses import replace

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.authorization import ParameterAuthorizer, PermissionConstraint
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError, ErrorCategory
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
    SideEffect,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.resilience import FailureCertainty, ResilientProviderExecutor, RetryPolicy
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
    provider_executor: ResilientProviderExecutor[ExecutionResult] | None = None,
    retry_certainty_classifier=None,
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
        provider_executor=provider_executor,
        retry_certainty_classifier=retry_certainty_classifier,
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


class CountingProvider(StaticProvider):
    calls = 0

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        type(self).calls += 1
        return super().execute(identity, request, context)


class RetryOnceProvider(StaticProvider):
    calls = 0

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        type(self).calls += 1
        if type(self).calls == 1:
            raise CapabilityHubError(
                code="provider_temporarily_unavailable",
                category=ErrorCategory.PROVIDER,
                safe_message="The provider is temporarily unavailable.",
                retryable=True,
            )
        return super().execute(identity, request, context)


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


def test_service_retries_only_when_failure_is_explicitly_not_applied() -> None:
    RetryOnceProvider.calls = 0
    executor: ResilientProviderExecutor[ExecutionResult] = ResilientProviderExecutor(
        retry_policy=RetryPolicy(max_attempts=2, initial_backoff_seconds=0),
        sleeper=lambda _seconds: None,
    )
    service, context, budget, _ = _setup(
        provider_type=RetryOnceProvider,
        provider_executor=executor,
        retry_certainty_classifier=lambda _error: FailureCertainty.NOT_APPLIED,
    )
    _, execution_ref = _search_and_load(service, context, budget)

    result = service.execute(
        ExecutionRequest(execution_ref, "find", {"query": "x"}, "task"),
        context=context,
        budget=budget,
    )

    assert result.output == {"items": [1]}
    assert RetryOnceProvider.calls == 2


def test_search_filters_capabilities_before_disclosure_by_permission() -> None:
    restricted = replace(_manifest(), permissions=("records.private",))
    service, _, budget, _ = _setup(restricted)
    denied_context = ServiceContext("tenant", "principal", "session")

    hidden = service.search(
        "records",
        task_id="task",
        context=denied_context,
        budget=budget,
        max_output_tokens=2_000,
    )
    allowed = service.search(
        "records",
        task_id="task-allowed",
        context=replace(denied_context, granted_permissions=frozenset({"records.private"})),
        budget=budget,
        max_output_tokens=2_000,
    )

    assert hidden.cards == ()
    assert hidden.total_matches == 0
    assert [card.revision for card in allowed.cards] == [restricted.identity.revision]


def test_parameter_authorizer_filters_search_and_denies_out_of_scope_execution(tmp_path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    manifest = replace(
        _manifest(),
        permissions=("filesystem.read",),
        operations=(
            OperationSpec(
                "find",
                OperationType.EXECUTE,
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
        ),
    )
    service, context, budget, _ = _setup(manifest, outputs={"find": {"items": []}})
    authorizer = ParameterAuthorizer(
        {"filesystem.read": PermissionConstraint(path_roots=(allowed_root,))}
    )
    constrained = replace(
        context,
        granted_permissions=frozenset({"filesystem.read"}),
        parameter_authorizer=authorizer,
    )
    _, execution_ref = _search_and_load(service, constrained, budget)

    with pytest.raises(CapabilityHubError) as denied:
        service.execute(
            ExecutionRequest(
                execution_ref,
                "find",
                {"path": str(tmp_path / "outside.txt")},
                "task",
            ),
            context=constrained,
            budget=budget,
        )

    assert denied.value.code == "argument_authorization_denied"
    assert denied.value.details == {"reason_codes": ("path_outside_allowed_roots",)}


def test_execute_requires_an_exact_bound_approval_reference() -> None:
    manifest = replace(
        _manifest(),
        operations=(
            OperationSpec(
                "find",
                OperationType.EXECUTE,
                side_effect=SideEffect.REVERSIBLE_WRITE,
                requires_approval=True,
            ),
        ),
    )
    service, context, budget, audit = _setup(manifest, outputs={"find": {"updated": True}})
    _, execution_ref = _search_and_load(service, context, budget)
    arguments: dict[str, JsonValue] = {"query": "x"}

    with pytest.raises(CapabilityHubError) as missing:
        service.execute(
            ExecutionRequest(execution_ref, "find", arguments, "task"),
            context=replace(context, approved=True),
            budget=budget,
        )
    assert missing.value.code == "approval_required"

    approval_ref = service.issue_approval(
        revision=manifest.identity.revision,
        operation="find",
        arguments=arguments,
        task_id="task",
        context=context,
    )
    result = service.execute(
        ExecutionRequest(
            execution_ref,
            "find",
            arguments,
            "task",
            approval_ref=approval_ref,
            idempotency_key="approval-test",
        ),
        context=context,
        budget=budget,
    )

    assert result.output == {"updated": True}
    assert [event.event_type for event in audit.events][-2:] == [
        "approval_issue",
        "execute",
    ]


def test_idempotency_replays_one_result_without_a_second_provider_call() -> None:
    CountingProvider.calls = 0
    service, context, budget, audit = _setup(provider_type=CountingProvider)
    _, execution_ref = _search_and_load(service, context, budget)
    request = ExecutionRequest(
        execution_ref,
        "find",
        {"query": "x"},
        "task",
        idempotency_key="same-request",
    )

    first = service.execute(request, context=context, budget=budget)
    replay = service.execute(request, context=context, budget=budget)

    assert replay == first
    assert CountingProvider.calls == 1
    assert budget.snapshot().used["executions"] == 1
    assert audit.events[-1].reason_codes == ("idempotent_replay",)


def test_idempotency_key_rejects_different_arguments_without_execution() -> None:
    CountingProvider.calls = 0
    service, context, budget, _ = _setup(provider_type=CountingProvider)
    _, execution_ref = _search_and_load(service, context, budget)
    service.execute(
        ExecutionRequest(
            execution_ref,
            "find",
            {"query": "first"},
            "task",
            idempotency_key="same-key",
        ),
        context=context,
        budget=budget,
    )

    with pytest.raises(CapabilityHubError) as conflict:
        service.execute(
            ExecutionRequest(
                execution_ref,
                "find",
                {"query": "changed"},
                "task",
                idempotency_key="same-key",
            ),
            context=context,
            budget=budget,
        )

    assert conflict.value.code == "idempotency_conflict"
    assert CountingProvider.calls == 1
    assert budget.snapshot().used["executions"] == 1


def test_failed_keyed_execution_is_not_automatically_replayed() -> None:
    service, context, budget, _ = _setup(provider_type=FailingProvider)
    _, execution_ref = _search_and_load(service, context, budget)
    request = ExecutionRequest(
        execution_ref,
        "find",
        {},
        "task",
        idempotency_key="uncertain",
    )

    with pytest.raises(CapabilityHubError) as failed:
        service.execute(request, context=context, budget=budget)
    with pytest.raises(CapabilityHubError) as replay:
        service.execute(request, context=context, budget=budget)

    assert failed.value.code == "provider_unhandled_error"
    assert replay.value.code == "idempotency_outcome_unknown"
    assert budget.snapshot().used["executions"] == 1


@pytest.mark.parametrize(
    ("changed_operation", "changed_arguments", "changed_context"),
    [
        ("count", {"query": "x"}, False),
        ("find", {"query": "changed"}, False),
        ("find", {"query": "x"}, True),
    ],
)
def test_approval_reference_rejects_changed_intent(
    changed_operation, changed_arguments, changed_context
) -> None:
    manifest = replace(
        _manifest(),
        operations=(
            OperationSpec(
                "find",
                OperationType.EXECUTE,
                side_effect=SideEffect.REVERSIBLE_WRITE,
            ),
            OperationSpec(
                "count",
                OperationType.EXECUTE,
                side_effect=SideEffect.REVERSIBLE_WRITE,
            ),
        ),
    )
    service, context, budget, _ = _setup(manifest)
    _, execution_ref = _search_and_load(service, context, budget, operations=("find", "count"))
    original_arguments: dict[str, JsonValue] = {"query": "x"}
    approval_ref = service.issue_approval(
        revision=manifest.identity.revision,
        operation="find",
        arguments=original_arguments,
        task_id="task",
        context=context,
    )
    selected_context = replace(context, principal_id="other") if changed_context else context

    with pytest.raises(CapabilityHubError) as raised:
        service.execute(
            ExecutionRequest(
                execution_ref,
                changed_operation,
                changed_arguments,
                "task",
                approval_ref=approval_ref,
            ),
            context=selected_context,
            budget=budget,
        )

    assert raised.value.code == "reference_scope_mismatch"


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


def test_execute_validates_input_and_output_json_schemas() -> None:
    manifest = replace(
        _manifest(),
        operations=(
            OperationSpec(
                "find",
                OperationType.EXECUTE,
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"items": {"type": "array"}},
                    "required": ["items"],
                },
            ),
        ),
    )
    service, context, budget, _ = _setup(manifest, outputs={"find": {"items": [1]}})
    _, execution_ref = _search_and_load(service, context, budget)

    with pytest.raises(CapabilityHubError) as invalid_input:
        service.execute(
            ExecutionRequest(execution_ref, "find", {"query": 1}, "task"),
            context=context,
            budget=budget,
        )
    assert invalid_input.value.code == "invalid_operation_arguments"
    assert budget.snapshot().used["executions"] == 0

    valid = service.execute(
        ExecutionRequest(execution_ref, "find", {"query": "x"}, "task"),
        context=context,
        budget=budget,
    )
    assert valid.output == {"items": [1]}


def test_execute_rejects_provider_output_that_breaks_declared_schema() -> None:
    manifest = replace(
        _manifest(),
        operations=(
            OperationSpec(
                "find",
                OperationType.EXECUTE,
                output_schema={"type": "object", "required": ["items"]},
            ),
        ),
    )
    service, context, budget, _ = _setup(manifest, outputs={"find": {"unexpected": True}})
    _, execution_ref = _search_and_load(service, context, budget)

    with pytest.raises(CapabilityHubError) as invalid_output:
        service.execute(
            ExecutionRequest(execution_ref, "find", {}, "task"),
            context=context,
            budget=budget,
        )

    assert invalid_output.value.code == "invalid_provider_result"
    assert budget.snapshot().used["executions"] == 1


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
