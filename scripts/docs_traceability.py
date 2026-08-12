"""Fail CI when release claims lose their runtime or test evidence."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

IMPLEMENTED_EVIDENCE = {
    "R2": (
        ("benchmarks/codex_live_eval.py", "def validate_live_artifact"),
        ("tests/test_codex_live_eval.py", "complete_real_usage"),
    ),
    "R1": (
        ("src/capabilityhub/admin_control.py", "class AuthenticatedAdminDispatcher"),
        ("tests/test_admin_control.py", "not_interchangeable"),
    ),
    "R4": (
        ("src/capabilityhub/service_adapter.py", "capability.execute"),
        ("tests/test_provider_conformance_matrix.py", "one_mcp_meta_tool_chain"),
    ),
    "R5": (
        ("src/capabilityhub/search.py", "max_card_bytes"),
        ("tests/test_scale_benchmark.py", "top3_hit"),
    ),
    "R3": (
        ("src/capabilityhub/models.py", "class CapabilityKind"),
        ("tests/test_registry.py", "CapabilityKind"),
    ),
    "R6": (
        ("src/capabilityhub/service.py", "def rehydrate"),
        ("tests/test_progressive_rehydration.py", "rehydration"),
    ),
    "R7": (
        ("src/capabilityhub/references.py", "class ReferenceSigner"),
        ("tests/test_references.py", "tampered"),
    ),
    "R8": (
        ("src/capabilityhub/admission.py", "validate_manifest_semantics"),
        ("tests/test_local_runtime.py", "invalid_driver_before_registry_admission"),
    ),
    "R9": (
        ("src/capabilityhub/registry.py", "class CapabilityRegistry"),
        ("tests/test_registry.py", "immutable"),
    ),
    "R13": (
        ("src/capabilityhub/activation_lock.py", "def export_activation_lock"),
        ("tests/test_activation_lock.py", "dependency"),
    ),
    "R14": (
        ("src/capabilityhub/registry.py", "resolve_projections"),
        ("tests/test_projection_admission.py", "conflict"),
    ),
    "R10": (
        ("src/capabilityhub/search.py", "SearchRankingConfig"),
        ("tests/test_search.py", "eligibility"),
    ),
    "R11": (
        ("src/capabilityhub/rag_index.py", "class DiskRagIndex"),
        ("tests/test_rag_index.py", "acl"),
    ),
    "R12": (
        ("src/capabilityhub/rag_index.py", "def expand"),
        ("tests/test_rag_index.py", "expansion"),
    ),
    "R15": (
        ("src/capabilityhub/draining.py", "class DrainController"),
        ("tests/test_drained_service.py", "draining"),
    ),
    "R16": (
        ("src/capabilityhub/drained_service.py", "cancellation_requests"),
        ("tests/test_runtime_http_drain.py", "rollback"),
    ),
    "R17": (
        ("src/capabilityhub/grant_policy.py", "class PrincipalGrantPolicy"),
        ("tests/test_grant_policy_runtime.py", "requires_new_authenticated_service_snapshot"),
    ),
    "R18": (
        ("src/capabilityhub/approval_store.py", "class ApprovalIntent"),
        ("tests/test_admin_entry_consistency.py", "distinct_authenticated_approver"),
    ),
    "R19": (
        ("src/capabilityhub/supply_chain_bundle.py", "class SigstoreBundleVerifier"),
        ("tests/test_supply_chain_bundle.py", "replay_and_log_fork"),
    ),
    "R20": (
        ("src/capabilityhub/secret_broker.py", "class KeyringSecretStore"),
        ("tests/test_platform_secret_store.py", "headless"),
    ),
    "R21": (
        ("src/capabilityhub/linux_sandbox.py", "def apply_linux_sandbox"),
        ("tests/test_linux_sandbox.py", "confine_provider_and_descendant"),
    ),
    "R22": (
        ("src/capabilityhub/resilience.py", "class ResilientProviderExecutor"),
        ("tests/test_resilience.py", "retry"),
    ),
    "R23": (
        ("src/capabilityhub/model_execution.py", "class OpenAIReasoningExecutor"),
        ("tests/test_service_adapter.py", "reasoning_executor"),
    ),
    "R24": (
        ("src/capabilityhub/orchestration.py", "class ReasoningOrchestrator"),
        ("tests/test_orchestration.py", "restart"),
    ),
    "R25": (
        ("src/capabilityhub/runtime.py", "DurableHierarchicalBudgetProvider"),
        ("tests/test_tenant_business_isolation.py", "opaque_hierarchical_budget"),
    ),
    "R26": (
        ("src/capabilityhub/context_removal.py", "class ContextRemovalCoordinator"),
        ("tests/test_context_removal.py", "pending_then_acknowledged"),
    ),
    "R27": (
        ("src/capabilityhub/scoped_context_state.py", "class ScopedContextState"),
        ("tests/test_scoped_context_state.py", "cross_scope"),
    ),
    "R28": (
        ("src/capabilityhub/admin_control.py", "class AdminRequestEnvelope"),
        ("tests/test_admin_dispatcher.py", "admin-dashboard"),
    ),
    "R29": (
        ("src/capabilityhub/idempotency.py", "class SqliteIdempotencyStore"),
        ("tests/test_idempotency.py", "concurrent"),
    ),
    "R30": (
        ("src/capabilityhub/secure_audit.py", "class SecureAuditLedger"),
        ("tests/test_secure_audit.py", "tamper"),
    ),
    "R31": (
        ("src/capabilityhub/dependency_observer.py", "class LiveDependencyObserver"),
        ("tests/test_dependency_observer.py", "fails_closed"),
    ),
    "R32": (
        ("benchmarks/rag_scale.py", "DiskRagIndex"),
        ("tests/test_scale_benchmark.py", "concurrent"),
    ),
    "R33": (
        ("src/capabilityhub/compatibility.py", "MINIMUM_DEPRECATION_DAYS"),
        ("tests/test_service_adapter.py", "old_client_new_server"),
    ),
    "R34": (
        ("src/capabilityhub/production_profile.py", "def validate_production_profile"),
        ("tests/test_wheel_smoke.py", "base_wheel_smoke_is_real_and_offline"),
    ),
    "R35": (
        ("src/capabilityhub/release_certification.py", "def certify_release"),
        ("tests/test_release_certification.py", "fake_live_and_mixed_revision"),
    ),
    "R36": (
        ("src/capabilityhub/webui.py", "class DashboardServer"),
        ("tests/browser/test_dashboard_browser.py", '"width": 390'),
    ),
}

REQUIRED_RUNTIME_CLAIMS = {
    "separate admin plane": (
        ("src/capabilityhub/runtime.py", "def local_admin_control"),
        ("tests/test_admin_control.py", "not_interchangeable"),
    ),
    "three data operations": (
        ("src/capabilityhub/http_control.py", "capability.execute"),
        ("tests/test_http_control.py", "capability.execute"),
    ),
    "helpme menu": (
        ("plugins/capabilityhub/skills/helpme/SKILL.md", "/helpme"),
        ("tests/test_plugin_package.py", "/helpme"),
    ),
    "myskills menu": (
        ("plugins/capabilityhub/skills/myskills/SKILL.md", "/myskills"),
        ("tests/test_plugin_package.py", "/myskills"),
    ),
}

STALE_PHRASES = {
    "authenticated control-plane credentials remain open",
    "public-key/Sigstore identity remains open",
    "not yet an automatic registry admission gate",
    "no 1m RAG",
    "authenticated tenant identity remains open",
}


def traceability_errors(root: Path = ROOT) -> tuple[str, ...]:
    errors: list[str] = []
    matrix = _read(root, "docs/completion-matrix.md")
    implemented = set(
        re.findall(r"^\| (R\d+) \|[^\n]+\| Implemented \|", matrix, flags=re.MULTILINE)
    )
    unknown = implemented - IMPLEMENTED_EVIDENCE.keys()
    if unknown:
        errors.append("Implemented rows lack traceability rules: " + ", ".join(sorted(unknown)))
    for requirement in sorted(implemented & IMPLEMENTED_EVIDENCE.keys()):
        _require_pairs(root, requirement, IMPLEMENTED_EVIDENCE[requirement], errors)
    for claim, pairs in REQUIRED_RUNTIME_CLAIMS.items():
        _require_pairs(root, claim, pairs, errors)
    release_text = "\n".join(
        _read(root, path)
        for path in (
            "README.md",
            "docs/completion-matrix.md",
            "docs/release-readiness.md",
        )
    )
    for phrase in sorted(STALE_PHRASES):
        if phrase.casefold() in release_text.casefold():
            errors.append(f"stale release wording remains: {phrase}")
    return tuple(errors)


def _require_pairs(
    root: Path,
    label: str,
    pairs: tuple[tuple[str, str], ...],
    errors: list[str],
) -> None:
    for path, marker in pairs:
        target = root / path
        if not target.is_file() or marker not in target.read_text(encoding="utf-8"):
            errors.append(f"{label}: missing {path} marker {marker!r}")


def _read(root: Path, path: str) -> str:
    return (root / path).read_text(encoding="utf-8")


def main() -> int:
    errors = traceability_errors()
    if errors:
        print("\n".join(errors))
        return 1
    print("documentation traceability passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
