from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ConflictSpec,
    DependencySpec,
    OmissionKind,
    OperationSpec,
    OperationType,
    SectionDescriptor,
)
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext


def _budget(name: str = "task") -> BudgetLedger:
    return BudgetLedger(
        name,
        {"bytes": 100_000, "loads": 20, "portable_tokens": 20_000},
    )


def _setup() -> tuple[
    CapabilityHubService,
    ServiceContext,
    BudgetLedger,
    list[float],
    CapabilityManifest,
    str,
]:
    dependency = CapabilityManifest(
        CapabilityIdentity("core", "auth", "1.2.0", "digest-auth"),
        CapabilityKind.API,
        "Authentication dependency.",
        "fixture",
        (OperationSpec("verify", OperationType.EXECUTE),),
    )
    target = CapabilityManifest(
        CapabilityIdentity("core", "records", "2.0.0", "digest-records"),
        CapabilityKind.API,
        "Progressively loaded records capability.",
        "fixture",
        (
            OperationSpec("find", OperationType.EXECUTE),
            OperationSpec("count", OperationType.EXECUTE),
        ),
        sections=(
            SectionDescriptor("contract", "text/plain", "public contract", 4),
            SectionDescriptor("examples", "text/plain", "example continuation", 5),
            SectionDescriptor("secret", "text/plain", "protected continuation", 5, sensitive=True),
        ),
        dependencies=(DependencySpec("core/auth", "^1.0"),),
        conflicts=(ConflictSpec("route", "/records"),),
    )
    registry = CapabilityRegistry()
    registry.register_many((dependency, target))
    registry.activate(dependency.identity.coordinate, dependency.identity.revision)
    registry.activate(target.identity.coordinate, target.identity.revision)
    now = [100.0]
    signer = ReferenceSigner(b"progressive-rehydration", clock=lambda: now[0])
    provider = StaticProvider((StaticFixture(target, {"find": [], "count": 1}),), name="fixture")
    service = CapabilityHubService(
        registry=registry,
        providers=(provider,),
        references=signer,
        audit=MemoryAuditSink(),
        load_ref_ttl_seconds=30,
    )
    context = ServiceContext("tenant", "principal", "session")
    load_ref = signer.issue(
        revision=target.identity.revision,
        scope=context.reference_scope,
        purpose="load",
        ttl_seconds=30,
    )
    return service, context, _budget(), now, target, load_ref


def _initial_load(
    service: CapabilityHubService,
    context: ServiceContext,
    budget: BudgetLedger,
    load_ref: str,
):
    return service.load(
        load_ref,
        task_id="task",
        context=context,
        budget=budget,
        section_names=("contract",),
        operation_names=("find",),
    )


def test_load_returns_deterministic_dependency_conflict_and_omission_metadata() -> None:
    service, context, budget, _, _, load_ref = _setup()

    loaded = _initial_load(service, context, budget, load_ref)

    assert loaded.omitted_sections == ("examples", "secret")
    assert loaded.omitted_operations == ("count",)
    assert [(notice.kind, notice.code) for notice in loaded.notices] == [
        ("dependency", "dependency.required"),
        ("conflict", "conflict.declared"),
    ]
    assert loaded.notices[0].attributes == {
        "coordinate": "core/auth",
        "optional": False,
        "version_constraint": "^1.0",
    }
    assert loaded.notices[1].attributes == {
        "type": "route",
        "value_digest": "sha256:" + hashlib.sha256(b"/records").hexdigest(),
    }
    assert [handle.kind for handle in loaded.rehydration_handles] == [
        OmissionKind.SECTION,
        OmissionKind.SECTION,
        OmissionKind.OPERATION,
    ]
    assert loaded.omitted_section_count == 2
    assert loaded.omitted_operation_count == 1
    assert loaded.omitted_notice_count == 0
    assert loaded.unhandled_omission_count == 0


def test_large_manifest_load_metadata_is_explicitly_bounded() -> None:
    sections = tuple(
        SectionDescriptor(f"section-{index}", "text/plain", "small", 2) for index in range(101)
    )
    operations = tuple(
        OperationSpec(f"operation-{index}", OperationType.EXECUTE) for index in range(101)
    )
    conflicts = tuple(ConflictSpec("route", f"/{index}/" + "x" * 1_900) for index in range(100))
    manifest = CapabilityManifest(
        CapabilityIdentity("core", "large", "1", "digest-large"),
        CapabilityKind.API,
        "Large progressive fixture.",
        "fixture",
        operations,
        sections=sections,
        conflicts=conflicts,
    )
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    signer = ReferenceSigner(b"large-progressive", clock=lambda: 100)
    service = CapabilityHubService(
        registry=registry,
        providers=(StaticProvider((StaticFixture(manifest, {}),), name="fixture"),),
        references=signer,
        audit=MemoryAuditSink(),
    )
    context = ServiceContext("tenant", "principal", "large")
    reference = signer.issue(
        revision=manifest.identity.revision,
        scope=context.reference_scope,
        purpose="load",
        ttl_seconds=300,
    )

    loaded = service.load(
        reference,
        task_id="large",
        context=context,
        budget=_budget("large"),
        section_names=("section-0",),
        operation_names=("operation-0",),
        max_output_tokens=2_000,
    )

    assert loaded.omitted_section_count == 100
    assert loaded.omitted_operation_count == 100
    assert len(loaded.omitted_sections) == 8
    assert len(loaded.omitted_operations) == 8
    assert len(loaded.rehydration_handles) == 4
    assert loaded.unhandled_omission_count == 196
    assert len(loaded.notices) == 4
    assert loaded.omitted_notice_count == 96
    assert all("x" * 100 not in repr(notice) for notice in loaded.notices)
    assert loaded.portable_tokens <= 2_000


def test_rehydration_handle_loads_exact_section_without_raw_target() -> None:
    service, context, budget, _, _, load_ref = _setup()
    loaded = _initial_load(service, context, budget, load_ref)
    examples_handle = loaded.rehydration_handles[0]

    continued = service.rehydrate(
        examples_handle,
        task_id="task-continuation",
        context=context,
        budget=budget,
    )

    assert [section.name for section in continued.sections] == ["examples"]
    assert continued.operations == ()
    assert "examples" not in repr(examples_handle)
    assert "example continuation" not in repr(examples_handle)
    assert "/records" not in repr(examples_handle)


def test_operation_rehydration_issues_grant_for_only_that_operation() -> None:
    service, context, budget, _, _, load_ref = _setup()
    loaded = _initial_load(service, context, budget, load_ref)

    continued = service.rehydrate(
        loaded.rehydration_handles[-1],
        task_id="task-operation",
        context=context,
        budget=budget,
    )

    assert [operation.name for operation in continued.operations] == ["count"]
    assert continued.sections == ()
    assert continued.execution_ref


@pytest.mark.parametrize("field", ["reference", "selector_digest", "kind", "expires_at"])
def test_tampered_handle_is_rejected(field: str) -> None:
    service, context, budget, _, _, load_ref = _setup()
    handle = _initial_load(service, context, budget, load_ref).rehydration_handles[0]
    if field == "reference":
        prefix, payload, signature = handle.reference.split(".")
        replacement = "A" if signature[0] != "A" else "B"
        tampered = replace(handle, reference=f"{prefix}.{payload}.{replacement}{signature[1:]}")
    elif field == "selector_digest":
        tampered = replace(handle, selector_digest="sha256:" + "0" * 64)
    elif field == "kind":
        tampered = replace(handle, kind=OmissionKind.OPERATION)
    else:
        tampered = replace(handle, expires_at=handle.expires_at + 1)

    with pytest.raises(CapabilityHubError) as caught:
        service.rehydrate(
            tampered,
            task_id="tampered",
            context=context,
            budget=budget,
        )

    assert caught.value.code in {
        "reference_tampered",
        "reference_purpose_mismatch",
        "rehydration_expiry_mismatch",
    }
    assert budget.snapshot().used["loads"] == 1


def test_expired_or_wrong_scope_handle_is_rejected_without_budget_charge() -> None:
    service, context, budget, now, _, load_ref = _setup()
    handle = _initial_load(service, context, budget, load_ref).rehydration_handles[0]
    used = budget.snapshot().used["loads"]
    wrong_context = replace(context, principal_id="other")

    with pytest.raises(CapabilityHubError) as wrong_scope:
        service.rehydrate(
            handle,
            task_id="wrong-scope",
            context=wrong_context,
            budget=budget,
        )
    now[0] = float(handle.expires_at)
    with pytest.raises(CapabilityHubError) as expired:
        service.rehydrate(
            handle,
            task_id="expired",
            context=context,
            budget=budget,
        )

    assert wrong_scope.value.code == "reference_scope_mismatch"
    assert expired.value.code == "reference_expired"
    assert budget.snapshot().used["loads"] == used


def test_sensitive_rehydration_rechecks_permission() -> None:
    service, context, budget, _, _, load_ref = _setup()
    sensitive = _initial_load(service, context, budget, load_ref).rehydration_handles[1]

    with pytest.raises(CapabilityHubError) as denied:
        service.rehydrate(
            sensitive,
            task_id="denied",
            context=context,
            budget=budget,
        )
    allowed = service.rehydrate(
        sensitive,
        task_id="allowed",
        context=replace(context, granted_permissions=frozenset({"content.sensitive"})),
        budget=budget,
    )

    assert denied.value.code == "sensitive_section_denied"
    assert allowed.sections[0].sensitive is True


def test_rehydration_response_and_handles_are_inside_hard_budget() -> None:
    service, context, budget, _, _, load_ref = _setup()
    handle = _initial_load(service, context, budget, load_ref).rehydration_handles[0]
    used = budget.snapshot().used["loads"]

    with pytest.raises(CapabilityHubError) as caught:
        service.rehydrate(
            handle,
            task_id="small-budget",
            context=context,
            budget=budget,
            max_output_tokens=1,
        )

    assert caught.value.code == "load_output_budget_exceeded"
    assert budget.snapshot().used["loads"] == used


def test_rehydration_is_deterministic_under_concurrent_readers() -> None:
    service, context, budget, _, _, load_ref = _setup()
    handle = _initial_load(service, context, budget, load_ref).rehydration_handles[0]

    def rehydrate(index: int):
        return service.rehydrate(
            handle,
            task_id=f"parallel-{index}",
            context=context,
            budget=_budget(f"parallel-{index}"),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(rehydrate, range(20)))

    assert all(result.sections == results[0].sections for result in results)
    assert all(result.rehydration_handles == results[0].rehydration_handles for result in results)
    assert all(result.portable_tokens == results[0].portable_tokens for result in results)
