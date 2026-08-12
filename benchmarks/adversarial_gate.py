"""Deterministic adversarial release gate using the real service boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.degraded import (
    DegradedModePolicy,
    Dependency,
    DependencyObservation,
    DependencyStatus,
    Operation,
)
from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    OperationSpec,
    OperationType,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext


@dataclass(frozen=True, slots=True)
class AdversarialCase:
    case_id: str
    expected_code: str
    actual_code: str
    passed: bool


@dataclass(frozen=True, slots=True)
class AdversarialGateReport:
    schema: str
    deterministic: bool
    external_credentials_used: bool
    cases: tuple[AdversarialCase, ...]

    @property
    def release_ready(self) -> bool:
        return (
            self.deterministic
            and not self.external_credentials_used
            and all(item.passed for item in self.cases)
        )


class _OversizeProvider:
    name = "adversarial-provider"

    def __init__(self, manifest: CapabilityManifest) -> None:
        self._manifest = manifest

    def discover(self) -> tuple[CapabilityManifest, ...]:
        return (self._manifest,)

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        return ExecutionResult(
            identity.revision,
            request.operation,
            "x" * 8_192,
            self.name,
            1,
            "adversarial-output",
        )


def run_adversarial_gate() -> AdversarialGateReport:
    service, context, budget, execution_ref = _fixture()
    prefix, signature = execution_ref.rsplit(".", 1)
    tampered_ref = prefix + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    cases = (
        _expect(
            "tampered-reference",
            "reference_tampered",
            lambda: service.execute(
                ExecutionRequest(tampered_ref, "read", {}, "task"),
                context=context,
                budget=budget,
            ),
        ),
        _expect(
            "cross-principal-reference",
            "reference_scope_mismatch",
            lambda: service.execute(
                ExecutionRequest(execution_ref, "read", {}, "task"),
                context=ServiceContext("tenant", "other", "session"),
                budget=budget,
            ),
        ),
        _expect(
            "oversize-provider-output",
            "provider_output_budget_exceeded",
            lambda: service.execute(
                ExecutionRequest(execution_ref, "read", {}, "task"),
                context=context,
                budget=budget,
                max_output_tokens=64,
            ),
        ),
        _dependency_case(),
    )
    report = AdversarialGateReport(
        schema="capabilityhub.adversarial-release-gate.v1",
        deterministic=True,
        external_credentials_used=False,
        cases=cases,
    )
    if not report.release_ready:
        raise RuntimeError("adversarial release gate failed")
    return report


def report_json(report: AdversarialGateReport) -> dict[str, Any]:
    return asdict(report)


def _fixture() -> tuple[CapabilityHubService, ServiceContext, BudgetLedger, str]:
    manifest = CapabilityManifest(
        CapabilityIdentity("gate", "oversize", "1", "sha256:" + "e" * 64),
        CapabilityKind.API,
        "Adversarial output fixture.",
        "adversarial-provider",
        (OperationSpec("read", OperationType.EXECUTE),),
    )
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    service = CapabilityHubService(
        registry=registry,
        providers=(_OversizeProvider(manifest),),
        references=ReferenceSigner(b"adversarial-release-gate-key-material"),
        audit=MemoryAuditSink(),
    )
    context = ServiceContext("tenant", "principal", "session")
    budget = BudgetLedger(
        "task",
        {"bytes": 50_000, "executions": 2, "loads": 1, "portable_tokens": 10_000},
    )
    card = service.search(
        "adversarial output", task_id="task", context=context, budget=budget
    ).cards[0]
    loaded = service.load(
        card.capability_ref,
        task_id="task",
        context=context,
        budget=budget,
        operation_names=("read",),
    )
    return service, context, budget, loaded.execution_ref


def _expect(case_id: str, expected: str, call: Any) -> AdversarialCase:
    try:
        call()
    except CapabilityHubError as error:
        actual = error.code
    else:
        actual = "unexpected_success"
    return AdversarialCase(case_id, expected, actual, actual == expected)


def _dependency_case() -> AdversarialCase:
    observations = (
        *(
            DependencyObservation(item, DependencyStatus.AVAILABLE, 100, 10)
            for item in Dependency
            if item is not Dependency.POLICY
        ),
        DependencyObservation(Dependency.POLICY, DependencyStatus.UNAVAILABLE, 100, 10),
    )
    decision = DegradedModePolicy(clock=lambda: 101).decide(Operation.EXECUTE, observations)
    actual = (
        "dependency.policy.unavailable"
        if "dependency.policy.unavailable" in decision.reasons
        else "unexpected_allow"
    )
    return AdversarialCase(
        "policy-disconnect-fails-closed",
        "dependency.policy.unavailable",
        actual,
        actual == "dependency.policy.unavailable" and decision.outcome.value == "deny",
    )
