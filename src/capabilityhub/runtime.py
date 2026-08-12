"""Small local runtime helpers used by the command-line interface."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from importlib import import_module, metadata, util
from pathlib import Path
from secrets import token_bytes
from threading import RLock, Timer
from time import monotonic
from typing import cast

from capabilityhub.activation_lock import (
    export_activation_lock,
    validate_activation_lock_json,
)
from capabilityhub.admin_control import (
    AdminControlAccess,
    AdminPrincipal,
    LoopbackAdminControl,
)
from capabilityhub.approval_store import (
    ApprovalIntent,
    ApprovalRecord,
    ApprovalStatus,
    ScopedApprovalStore,
)
from capabilityhub.audit import AuditEvent, AuditSink, ScopedAuditSink, read_scoped_audit
from capabilityhub.auth import AuthIdentity
from capabilityhub.authorization import ParameterAuthorizer
from capabilityhub.budget import BudgetLedger, BudgetSnapshot
from capabilityhub.budget_store import SqliteBudgetLedger, SqliteBudgetRepository
from capabilityhub.compatibility import (
    FeatureHandshake,
    decide_compatibility,
    v1alpha1_handshake,
)
from capabilityhub.connections import (
    ConnectionProber,
    ProbeResult,
    configured_mcp_targets,
)
from capabilityhub.context_state import LocalContextState
from capabilityhub.degraded import (
    DegradedDecision,
    DegradedModePolicy,
    Dependency,
    DependencyStatus,
    SafeFallback,
)
from capabilityhub.degraded import Operation as DependencyOperation
from capabilityhub.drained_service import (
    DrainedCapabilityHubService,
    SignedExecutionBindingResolver,
)
from capabilityhub.draining import DrainController, DrainOutcome, LifecycleState
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.hierarchical_budget import (
    DurableHierarchicalBudgetProvider,
    SQLiteHierarchicalBudgetStore,
    load_or_create_hmac_key,
)
from capabilityhub.http_control import HttpControlAccess, LoopbackHttpControl
from capabilityhub.idempotency import SqliteIdempotencyStore
from capabilityhub.insights import loaded_view, providers_view, routing_view
from capabilityhub.lifecycle import StagedUpdateManager
from capabilityhub.local_runtime import (
    LocalCatalogGeneration,
    LocalCatalogMonitor,
    local_dependency_observations,
)
from capabilityhub.manifest import load_manifest
from capabilityhub.manifest_export import manifest_to_document
from capabilityhub.migration import migrate_manifest
from capabilityhub.models import (
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
    LoadedCapability,
    OperationSpec,
    ReasoningTier,
    SideEffect,
)
from capabilityhub.observability import InMemoryObservability, SqliteMetricStore
from capabilityhub.openapi_import import OpenApiSelection, import_openapi_file
from capabilityhub.orchestration import ReasoningOrchestrator
from capabilityhub.protocol import AdapterKind, in_process_request
from capabilityhub.providers.base import CapabilityProvider
from capabilityhub.providers.skill import SkillProvider
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.reasoning import ReasoningRouter
from capabilityhub.reasoning_store import SQLiteReasoningStore
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.residency import ResidentSection
from capabilityhub.resilience import (
    CircuitBreaker,
    ResilientProviderExecutor,
    RetryPolicy,
    classify_adapter_failure,
)
from capabilityhub.retention import AuditRetentionManager
from capabilityhub.search import SearchResponse
from capabilityhub.secure_audit import (
    ResilientAuditSink,
    SecureAuditLedger,
    load_or_create_signing_key,
)
from capabilityhub.service import (
    CapabilityHubService,
    ServiceContext,
    enforce_dependency_decision,
)
from capabilityhub.service_adapter import CapabilityHubServiceAdapter
from capabilityhub.state import (
    PreferenceScope,
    resolved_preferences,
    set_lifecycle,
    set_locale,
)
from capabilityhub.supervision import ProcessProviderSupervisor
from capabilityhub.supply_chain import (
    ArtifactAttestation,
    ArtifactMaterial,
    SupplyChainPolicy,
    SupplyChainVerifier,
)
from capabilityhub.tenancy import SqliteScopedState, TenantScope
from capabilityhub.update_store import SQLiteUpdateStore
from capabilityhub.webui import (
    ApprovalProvider,
    ContextProvider,
    DashboardServer,
    LanguageProvider,
    LifecycleProvider,
    SearchProvider,
    StatusSnapshot,
)

DEFAULT_LOCAL_BUDGETS = {
    "bytes": 1_000_000,
    "executions": 10,
    "loads": 100,
    "portable_tokens": 100_000,
    "reasoning_tokens": 100_000,
}
DEFAULT_LOCAL_HTTP_AGGREGATE_BUDGETS = {
    counter: limit * 100 for counter, limit in DEFAULT_LOCAL_BUDGETS.items()
}
LOCAL_POLICY_REVISION = "local-v1"
_LOCAL_STORE_INIT_LOCK = RLock()
_LOCAL_PROVIDER_LOCK = RLock()
_LOCAL_PROVIDER_SUPERVISORS: dict[Path, ProcessProviderSupervisor] = {}
_LOCAL_PROVIDER_EXECUTORS: dict[Path, ResilientProviderExecutor[ExecutionResult]] = {}
DEFAULT_CONTEXT_TOKENS = 16_000
SECURE_AUDIT_KEY_ENV = "CAPABILITYHUB_AUDIT_KEY"
LOCAL_LAST_GOOD_MAX_AGE_SECONDS = 300.0


def validate(paths: list[str | Path]) -> int:
    """Validate manifest files and return their count without executing providers."""
    for path in paths:
        load_manifest(path)
    return len(paths)


def local_manifest_export(path: str | Path) -> dict[str, JsonValue]:
    """Export one validated manifest deterministically without invoking a Provider."""

    return manifest_to_document(load_manifest(path))


def local_openapi_import(
    path: str | Path,
    *,
    operation_ids: list[str],
    allowed_hosts: list[str],
    namespace: str,
    name: str,
    version: str,
) -> dict[str, JsonValue]:
    """Preview an offline, allowlisted OpenAPI projection as a capability manifest."""

    try:
        selection = OpenApiSelection(
            namespace=namespace,
            name=name,
            version=version,
            operation_ids=tuple(operation_ids),
            allowed_hosts=tuple(allowed_hosts),
        )
    except ValueError as error:
        raise CapabilityHubError(
            code="openapi_selection_invalid",
            category=ErrorCategory.INPUT,
            safe_message="The OpenAPI operation or host selection is invalid.",
        ) from error
    result = import_openapi_file(path, selection=selection)
    return {
        "manifest": manifest_to_document(result.manifest),
        "selected_operation_ids": list(result.selected_operation_ids),
        "server_origin": result.server_origin,
        "source_digest": result.source_digest,
    }


def local_activation_lock(
    project_root: str | Path | None = None,
    *,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Export the exact active catalog without loading capability bodies."""

    selected = _select_local_scope(project_root, monitor)
    return export_activation_lock(selected.snapshot().registry)


def local_activation_lock_verify(
    path: str | Path,
    project_root: str | Path | None = None,
    *,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Verify an activation lock against the current active catalog."""

    try:
        source = Path(path).read_bytes()
    except OSError as error:
        raise CapabilityHubError(
            code="activation_lock_unreadable",
            category=ErrorCategory.INPUT,
            safe_message="The activation lock could not be read.",
        ) from error
    selected = _select_local_scope(project_root, monitor)
    result = validate_activation_lock_json(source, selected.snapshot().registry)
    return {
        "capability_count": result.capability_count,
        "lock_digest": result.lock_digest,
        "valid": True,
    }


def local_manifest_migrate(path: str | Path) -> dict[str, JsonValue]:
    """Migrate one JSON manifest in memory and return the document plus explicit report."""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CapabilityHubError(
            code="manifest_unreadable",
            category=ErrorCategory.INPUT,
            safe_message="Manifest could not be read as JSON.",
        ) from error
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise CapabilityHubError(
            code="migration_invalid_shape",
            category=ErrorCategory.INPUT,
            safe_message="The manifest must be an object.",
        )
    result = migrate_manifest(raw)
    return {
        "document": result.document,
        "report": cast(dict[str, JsonValue], _jsonable(result.report)),
    }


def local_compatibility(
    *,
    api_versions: list[str] | None = None,
    supported_features: list[str] | None = None,
    required_features: list[str] | None = None,
) -> dict[str, JsonValue]:
    """Compare a caller handshake with the local v1alpha1 compatibility surface."""

    server = v1alpha1_handshake()
    required = tuple(required_features or ())
    supported = tuple(
        dict.fromkeys((*(supported_features or server.supported_features), *required))
    )
    client = FeatureHandshake(
        tuple(api_versions or server.api_versions),
        supported,
        required,
    )
    return {
        "client": cast(dict[str, JsonValue], _jsonable(client)),
        "decision": cast(dict[str, JsonValue], _jsonable(decide_compatibility(client, server))),
        "server": cast(dict[str, JsonValue], _jsonable(server)),
    }


def discover_skills(directories: list[str | Path]) -> tuple[CapabilityManifest, ...]:
    """Discover inert Skill manifests from approved local directories."""
    return SkillProvider(directories).discover()


def dashboard(
    snapshot_provider: Callable[[], StatusSnapshot],
    *,
    port: int = 0,
    search_provider: SearchProvider | None = None,
    lifecycle_provider: LifecycleProvider | None = None,
    language_provider: LanguageProvider | None = None,
    approval_provider: ApprovalProvider | None = None,
    context_provider: ContextProvider | None = None,
) -> DashboardServer:
    """Create (but do not implicitly retain) a localhost-only dashboard server."""
    server = DashboardServer(
        snapshot_provider,
        host="127.0.0.1",
        port=port,
        search_provider=search_provider,
        lifecycle_provider=lifecycle_provider,
        language_provider=language_provider,
        approval_provider=approval_provider,
        context_provider=context_provider,
    )
    server.start()
    return server


def local_inventory(
    project_root: str | Path | None = None,
    *,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Return safe global inventory counts without capability bodies or credentials."""

    selected = _select_local_scope(project_root, monitor)
    return selected.snapshot().inventory_json()


def _local_cli_adapter(
    service: CapabilityHubService,
    *,
    context: ServiceContext,
    budget: BudgetLedger,
    project: Path,
) -> CapabilityHubServiceAdapter:
    """Bind trusted local state behind the same adapter used by remote transports."""

    return CapabilityHubServiceAdapter(
        service,
        kind=AdapterKind.CLI,
        context_provider=lambda: context,
        budget_provider=lambda _task_id: budget,
        observability=_runtime_observability(project),
    )


def _local_cli_dispatch(
    adapter: CapabilityHubServiceAdapter,
    operation: str,
    payload: Mapping[str, JsonValue],
    *,
    correlation_id: str | None = None,
) -> dict[str, JsonValue]:
    request_id = token_bytes(16).hex()
    result = adapter.dispatch(
        in_process_request(
            AdapterKind.CLI,
            operation,
            payload,
            request_id=request_id,
            correlation_id=correlation_id or request_id,
            handshake=adapter.handshake,
        )
    )
    if not isinstance(result, dict):
        raise CapabilityHubError(
            code="invalid_adapter_result",
            category=ErrorCategory.INTERNAL,
            safe_message="The shared adapter returned an invalid result.",
        )
    return result


def local_search(
    query: str,
    *,
    kinds: list[str] | None = None,
    limit: int = 8,
    granted_permissions: list[str] | None = None,
    parameter_authorizer: ParameterAuthorizer | None = None,
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Search compact local metadata and omit task-scoped MCP references."""

    selected = _select_local_scope(project_root, monitor)
    snapshot = selected.snapshot()
    dependency = _local_dependency_decision(snapshot, DependencyOperation.SEARCH)
    signer = ReferenceSigner(b"capabilityhub-local-cli-search")
    service = CapabilityHubService(
        registry=snapshot.registry,
        providers=snapshot.providers,
        references=signer,
        audit=_audit_sink(selected.project),
        dependency_decider=lambda _operation: dependency,
    )
    if granted_permissions is None and parameter_authorizer is None:
        granted_permissions = sorted(
            {
                permission
                for revision in snapshot.registry.activations.values()
                for permission in snapshot.registry.revision(revision).permissions
            }
        )
    context = _local_context(
        granted_permissions,
        parameter_authorizer=parameter_authorizer,
    )
    budget = BudgetLedger("local-search", {"bytes": 1_000_000, "portable_tokens": 900})
    adapter = _local_cli_adapter(
        service, context=context, budget=budget, project=selected.project
    )
    response = _local_cli_dispatch(
        adapter,
        "capability.search",
        {
            "query": query,
            "task_id": "local-cli",
            "kinds": None if kinds is None else list(cast(Iterable[JsonValue], kinds)),
            "limit": limit,
            "max_output_tokens": 900,
        },
    )
    cards = cast(list[JsonValue], response["cards"])
    results: list[JsonValue] = [
        {
            key: value
            for key, value in cast(dict[str, JsonValue], card).items()
            if key != "capability_ref"
        }
        for card in cards
    ]
    counts = cast(dict[str, JsonValue], response["kind_counts"])
    return {
        "dependency": _dependency_decision_json(dependency),
        "kind_counts": counts,
        "payload_bytes": response["payload_bytes"],
        "portable_tokens": response["portable_tokens"],
        "query": query,
        "results": results,
        "total_matches": response["total_matches"],
        "truncated": response["truncated"],
    }


def local_health(
    project_root: str | Path | None = None, *, home: str | Path | None = None
) -> dict[str, JsonValue]:
    """Check runtime wiring without scanning or loading the capability catalog."""

    project = _project(project_root)
    checks: list[JsonValue] = []
    project_ok = project.is_dir()
    checks.append({"check": "project_root", "status": "ok" if project_ok else "error"})
    assets = Path(__file__).with_name("web")
    assets_ok = all((assets / name).is_file() for name in ("index.html", "app.js", "style.css"))
    checks.append({"check": "dashboard_assets", "status": "ok" if assets_ok else "error"})
    checks.append(
        {"check": "mcp_sdk", "status": "available" if util.find_spec("mcp") else "missing"}
    )
    audit_health = _audit_health(project)
    checks.append({"check": "secure_audit", "status": audit_health})
    home_dir = Path(home).resolve() if home is not None else Path.home()
    config = home_dir / ".codex" / "config.toml"
    config_status = "missing"
    if config.is_file():
        try:
            with config.open("rb") as stream:
                tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError):
            config_status = "invalid"
        else:
            config_status = "ok"
    checks.append({"check": "codex_config", "status": config_status})
    try:
        version = metadata.version("capabilityhub")
    except metadata.PackageNotFoundError:
        version = "source"
    overall = (
        "ok"
        if project_ok and assets_ok and config_status != "invalid" and audit_health == "ok"
        else "degraded"
    )
    return {
        "catalog_loaded": False,
        "checks": checks,
        "scope": "local_wiring",
        "status": overall,
        "version": version,
    }


def local_connections(
    project_root: str | Path | None = None,
    *,
    monitor: LocalCatalogMonitor | None = None,
    probe: bool = False,
    probe_timeout_ms: int = 1_000,
    probe_concurrency: int = 4,
    allow_loopback: bool = False,
    connection_prober: ConnectionProber | None = None,
) -> dict[str, JsonValue]:
    """Describe configuration; probe only when explicitly requested."""

    selected = _select_local_scope(project_root, monitor)
    snapshot = selected.snapshot()
    active = snapshot.inventory.get("active_by_kind")
    active_counts = active if isinstance(active, dict) else {}
    probe_results: tuple[ProbeResult, ...] = ()
    if probe:
        if not 50 <= probe_timeout_ms <= 5_000:
            raise ValueError("probe_timeout_ms must be between 50 and 5000")
        if not 1 <= probe_concurrency <= 16:
            raise ValueError("probe_concurrency must be between 1 and 16")
        prober = connection_prober or ConnectionProber(
            timeout_seconds=probe_timeout_ms / 1_000,
            max_concurrency=probe_concurrency,
            allow_loopback=allow_loopback,
        )
        targets = configured_mcp_targets(selected.home / ".codex" / "config.toml")
        probe_results = prober.probe_all(targets)
    connections: list[JsonValue] = []
    for kind in CapabilityKind:
        configured = len(snapshot.registry.by_kind(kind))
        if kind is CapabilityKind.SKILL:
            state = "indexed" if configured else "not_found"
        elif kind is CapabilityKind.CLI:
            state = "available" if configured else "not_found"
        else:
            state = "configured_not_probed" if configured else "not_configured"
        row: dict[str, JsonValue] = {
            "active": active_counts.get(kind.value, 0),
            "configured": configured,
            "kind": kind.value,
            "state": state,
        }
        if probe:
            selected_results = tuple(item for item in probe_results if item.kind is kind)
            attempted = tuple(item for item in selected_results if item.attempted)
            row.update(
                {
                    "authenticated": "unknown",
                    "healthy": None,
                    "latency_bucket": _connection_latency(attempted),
                    "reachable": (
                        any(item.reachable is True for item in attempted) if attempted else None
                    ),
                    "reason_codes": _connection_reason_codes(selected_results, configured),
                    "transport_security": _connection_transport_security(attempted),
                }
            )
            if row["reachable"] is True:
                row["state"] = "reachable"
            elif attempted:
                row["state"] = "probe_failed"
            elif configured:
                row["state"] = "probe_unsupported"
        connections.append(row)
    response: dict[str, JsonValue] = {
        "connections": connections,
        "generation": snapshot.inventory.get("generation"),
        "network_probes_performed": sum(item.attempted for item in probe_results),
        "scope": "configuration_and_safe_probe" if probe else "configuration_only",
    }
    if probe:
        response["probe_results"] = [
            {
                "authenticated": item.authenticated,
                "configured": item.configured,
                "connection_id": item.connection_id,
                "healthy": item.healthy,
                "kind": item.kind.value,
                "latency_bucket": item.latency_bucket,
                "reachable": item.reachable,
                "reason_code": item.reason_code,
                "transport_security": item.transport_security,
            }
            for item in probe_results
        ]
    return response


def _connection_latency(results: tuple[ProbeResult, ...]) -> str:
    order = {"lt_50ms": 0, "50_199ms": 1, "200_999ms": 2, "gte_1000ms": 3}
    buckets = [item.latency_bucket for item in results if item.latency_bucket in order]
    return max(buckets, key=order.__getitem__) if buckets else "unknown"


def _connection_transport_security(results: tuple[ProbeResult, ...]) -> str:
    values = {item.transport_security for item in results}
    if len(values) == 1:
        return next(iter(values))
    if values:
        return "mixed"
    return "unknown"


def _connection_reason_codes(results: tuple[ProbeResult, ...], configured: int) -> list[JsonValue]:
    if results:
        return list(sorted({item.reason_code for item in results}))
    return ["probe_unsupported" if configured else "not_configured"]


def local_audit(
    project_root: str | Path | None = None,
    *,
    limit: int = 50,
    monitor: LocalCatalogMonitor | None = None,
    identity: AuthIdentity | None = None,
    task_id: str = "local-cli",
) -> dict[str, JsonValue]:
    """Return a bounded redacted tail of durable project audit events."""

    selected = _select_local_scope(project_root, monitor)
    if identity is None:
        events = _audit_events(selected.project, limit=limit)
        identity_source = "local-cli"
    else:
        events = read_scoped_audit(
            _scoped_state(selected.project),
            _tenant_scope(identity, task_id),
            limit=limit,
        )
        identity_source = identity.source
    rows: list[JsonValue] = [
        {
            "capability_revision": event.capability_revision,
            "event_type": event.event_type,
            "outcome": event.outcome,
            "payload_bytes": event.payload_bytes,
            "portable_tokens": event.portable_tokens,
            "reason_codes": list(event.reason_codes),
            "sequence": event.sequence,
            "task": hashlib.sha256(event.task_id.encode()).hexdigest()[:12],
        }
        for event in events
    ]
    return {
        "events": rows,
        "identity_source": identity_source,
        "limit": limit,
        "scope": "authenticated-task" if identity is not None else "project",
        "stored": len(rows),
    }


def local_secure_audit(
    action: str = "verify",
    *,
    source: str = "current",
    destination: str | Path | None = None,
    max_segments: int = 10,
    project_root: str | Path | None = None,
) -> dict[str, JsonValue]:
    """Verify, rotate, list, or export the default HMAC audit ledger."""

    project = _catalog_project(project_root)
    key = _secure_audit_key(project)
    manager = AuditRetentionManager(
        _secure_audit_root(project),
        signing_key=key,
        max_segments=max_segments,
    )
    if action == "verify":
        return {
            "configured": True,
            "key_environment": SECURE_AUDIT_KEY_ENV,
            "verification": cast(dict[str, JsonValue], _jsonable(manager.ledger.verify())),
        }
    if action == "list":
        return {
            "archives": [path.name for path in manager.archives],
            "configured": True,
            "current": cast(dict[str, JsonValue], _jsonable(manager.ledger.verify())),
        }
    if action == "rotate":
        result = manager.rotate()
        return {
            "archive": result.archive.name,
            "removed_segments": result.removed_segments,
            "retained_segments": result.retained_segments,
            "verification": cast(dict[str, JsonValue], _jsonable(result.verification)),
        }
    if action == "export":
        if destination is None:
            raise ValueError("secure audit export requires a destination")
        selected = (
            manager.ledger.directory if source == "current" else manager.archive_root / source
        )
        target = manager.export_jsonl(selected, destination)
        return {"exported": True, "destination": str(target), "source": source}
    raise ValueError("secure audit action must be verify, list, rotate, or export")


def local_observability(
    action: str = "list",
    *,
    destination: str | Path | None = None,
    limit: int = 500,
    project_root: str | Path | None = None,
) -> dict[str, JsonValue]:
    """Read or export privacy-minimized aggregate runtime metrics."""

    store = SqliteMetricStore(_state_path(_catalog_project(project_root)))
    if action == "list":
        return {
            "metrics": [
                cast(dict[str, JsonValue], _jsonable(item))
                for item in store.snapshots(limit=limit)
            ],
            "limit": limit,
        }
    if action == "export":
        if destination is None:
            raise ValueError("observability export requires a destination")
        target = Path(destination).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(store.export_jsonl(limit=limit), encoding="utf-8")
        return {"exported": True, "destination": str(target)}
    raise ValueError("observability action must be list or export")


def local_loaded(
    project_root: str | Path | None = None,
    *,
    limit: int = 20,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Show recent successful loads without retaining capability bodies in memory."""

    selected = _select_local_scope(project_root, monitor)
    generation = selected.snapshot()
    events = _restore_audit_revisions(
        _audit_events(selected.project, limit=500), generation.registry.revisions
    )
    return loaded_view(generation.registry, events, limit=limit)


def _restore_audit_revisions(
    events: tuple[AuditEvent, ...], revisions: Mapping[str, CapabilityManifest]
) -> tuple[AuditEvent, ...]:
    by_digest = {
        hashlib.sha256(
            b"capabilityhub-audit-identity-v1\0" + revision.encode()
        ).hexdigest(): revision
        for revision in revisions
    }
    return tuple(
        AuditEvent(
            event.event_id,
            event.sequence,
            event.task_id,
            event.event_type,
            by_digest.get(event.capability_revision or "", event.capability_revision),
            event.outcome,
            event.portable_tokens,
            event.payload_bytes,
            event.reason_codes,
            event.metadata,
        )
        for event in events
    )


def local_providers(
    project_root: str | Path | None = None,
    *,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Return real Provider groupings from the current local registry."""

    selected = _select_local_scope(project_root, monitor)
    result = providers_view(selected.snapshot().registry)
    executor = _local_provider_executor(selected.project)
    provider_names = sorted(provider.name for provider in selected.snapshot().providers)
    result["circuits"] = [
        {
            "consecutive_failures": snapshot.consecutive_failures,
            "provider": provider_name,
            "state": snapshot.state.value,
        }
        for provider_name in provider_names
        if (snapshot := executor.snapshot(provider_name)) is not None
    ]
    result["active_workers"] = _local_provider_supervisor(selected.project).active_count()
    return result


def local_routing(
    query: str,
    *,
    kinds: list[str] | None = None,
    limit: int = 8,
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Explain current deterministic search routing with zero model calls."""

    return routing_view(
        local_search(
            query,
            kinds=kinds,
            limit=limit,
            project_root=project_root,
            monitor=monitor,
        )
    )


def local_preferences(
    project_root: str | Path | None = None,
    *,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Return resolved non-secret language and lifecycle preferences."""

    selected = _select_local_scope(project_root, monitor)
    payload = resolved_preferences(home=selected.home, project=selected.project)
    capabilities = payload["capabilities"]
    paths = payload["paths"]
    assert isinstance(capabilities, dict) and isinstance(paths, dict)
    return {
        "capabilities": dict(capabilities),
        "locale": cast(JsonValue, payload["locale"]),
        "paths": dict(paths),
        "scope_precedence": ["project", "global", "auto"],
    }


def local_set_locale(
    locale: str,
    *,
    scope: str = "project",
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Persist a static-catalog locale without invoking a model translation."""

    selected = _select_local_scope(project_root, monitor)
    path = set_locale(
        locale,
        scope=cast(PreferenceScope, scope),
        home=selected.home,
        project=selected.project,
    )
    return {"locale": locale, "path": str(path), "scope": scope, "saved": True}


def local_lifecycle(
    project_root: str | Path | None = None,
    *,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """List persistent lifecycle overrides and their current catalog effect."""

    selected = _select_local_scope(project_root, monitor)
    snapshot = selected.snapshot()
    preferences = resolved_preferences(home=selected.home, project=selected.project)
    raw_states = preferences["capabilities"]
    assert isinstance(raw_states, dict)
    active = snapshot.registry.activations
    entries: list[JsonValue] = [
        {
            "active": coordinate in active,
            "coordinate": coordinate,
            "state": state,
        }
        for coordinate, state in sorted(raw_states.items())
    ]
    return {
        "entries": entries,
        "generation": snapshot.inventory.get("generation"),
        "inventory_status": snapshot.inventory.get("status"),
    }


def local_set_lifecycle(
    coordinate: str,
    state: str,
    *,
    scope: str = "project",
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Persist enable/disable/quarantine and atomically refresh local Inventory."""

    selected = _select_local_scope(project_root, monitor)
    before = selected.snapshot()
    known_coordinates = {
        manifest.identity.coordinate for manifest in before.registry.revisions.values()
    }
    if coordinate not in known_coordinates:
        raise CapabilityHubError(
            code="unknown_coordinate",
            category=ErrorCategory.REFERENCE,
            safe_message="Capability coordinate is not present in the local catalog.",
        )
    manifest = before.registry.revisions_for(coordinate)[-1]
    dependency = _local_dependency_decision(
        before,
        DependencyOperation.LIFECYCLE,
        provider_name=manifest.provider,
    )
    enforce_dependency_decision(dependency)
    path = set_lifecycle(
        coordinate,
        state,
        scope=cast(PreferenceScope, scope),
        home=selected.home,
        project=selected.project,
    )
    after = selected.snapshot(force=True)
    return {
        "active": coordinate in after.registry.activations,
        "coordinate": coordinate,
        "dependency": _dependency_decision_json(dependency),
        "generation": after.inventory.get("generation"),
        "path": str(path),
        "scope": scope,
        "state": state,
    }


def local_updates(
    project_root: str | Path | None = None,
    *,
    limit: int = 100,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """List durable staged-update state and in-flight revision pins."""

    selected, manager = _update_manager(project_root, monitor)
    return {
        "generation": selected.snapshot().inventory.get("generation"),
        "pins": [_jsonable(pin) for pin in manager.pins()],
        "states": [_jsonable(state) for state in manager.states(limit=limit)],
    }


def local_update_action(
    action: str,
    target: str,
    *,
    expected_active_revision: str | None = None,
    health_passed: bool | None = None,
    pin_id: str | None = None,
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
    artifact: bytes | None = None,
    publisher: str | None = None,
    artifact_registry: str | None = None,
    attestation: ArtifactAttestation | None = None,
    trust_mode: str = "strict",
    supply_chain_verifier: SupplyChainVerifier | None = None,
) -> dict[str, JsonValue]:
    """Stage, health-gate, activate, rollback, pin, or release an immutable revision."""

    if trust_mode not in {"strict", "development"}:
        raise ValueError("trust_mode must be strict or development")
    material: ArtifactMaterial | None = None
    supplied = (artifact, publisher, artifact_registry)
    if any(item is not None for item in supplied):
        if artifact is None or publisher is None or artifact_registry is None:
            raise ValueError("artifact, publisher, and artifact_registry must be supplied together")
        material = ArtifactMaterial(artifact, publisher, artifact_registry, attestation)
    verifier = supply_chain_verifier
    if trust_mode == "development":
        if material is None:
            raise ValueError("development trust mode requires explicit artifact material")
        verifier = SupplyChainVerifier(
            SupplyChainPolicy(
                environment="development",
                trusted_publishers=frozenset({material.publisher}),
                trusted_registries=frozenset({material.registry}),
                allow_unsigned_development=True,
            )
        )
    selected, manager = _update_manager(
        project_root,
        monitor,
        verifier=verifier,
        material=material,
    )
    if action in {"stage", "health", "activate"}:
        manifest = manager.registry.revision(target)
        coordinate = manifest.identity.coordinate
        _bootstrap_update_pointer(manager, coordinate)
    else:
        coordinate = target
    if action == "stage":
        state = manager.state(coordinate)
        result: object = manager.stage(
            target,
            expected_active_revision=(
                expected_active_revision
                if expected_active_revision is not None
                else state.active_revision
            ),
        )
    elif action == "health":
        if health_passed is None:
            raise ValueError("update health requires an explicit pass or fail result")
        result = manager.record_health(target, passed=health_passed)
    elif action == "activate":
        state = manager.state(coordinate)
        result = manager.activate(
            target,
            expected_active_revision=(
                expected_active_revision
                if expected_active_revision is not None
                else state.active_revision
            ),
        )
        selected.snapshot(force=True)
    elif action == "rollback":
        _bootstrap_update_pointer(manager, coordinate)
        state = manager.state(coordinate)
        if state.active_revision is None:
            raise ValueError("update rollback requires an active revision")
        result = manager.rollback(
            coordinate,
            expected_active_revision=expected_active_revision or state.active_revision,
        )
        selected.snapshot(force=True)
    elif action == "pin":
        if pin_id is None:
            raise ValueError("update pin requires a pin_id")
        _bootstrap_update_pointer(manager, coordinate)
        result = manager.pin_active(coordinate, pin_id)
    elif action == "release":
        result = {"pin_id": target, "released": manager.release_pin(target)}
    else:
        raise ValueError("unknown update action")
    return {
        "action": action,
        "generation": selected.snapshot().inventory.get("generation"),
        "result": _jsonable(result),
    }


def local_load(
    revision: str,
    *,
    section_names: list[str] | None = None,
    operation_names: list[str] | None = None,
    granted_permissions: list[str] | None = None,
    parameter_authorizer: ParameterAuthorizer | None = None,
    max_output_tokens: int = 2_000,
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Load selected material from one active revision through the service boundary."""

    selected = _select_local_scope(project_root, monitor)
    generation = selected.snapshot()
    manifest = generation.registry.revision(revision)
    dependency = _local_dependency_decision(
        generation,
        DependencyOperation.LOAD,
        provider_name=manifest.provider,
    )
    signer = ReferenceSigner(token_bytes(32))
    audit = _audit_sink(selected.project)
    service = CapabilityHubService(
        registry=generation.registry,
        providers=generation.providers,
        references=signer,
        audit=audit,
        dependency_decider=lambda _operation: dependency,
    )
    context = _local_context(
        granted_permissions,
        parameter_authorizer=parameter_authorizer,
    )
    budget = _persistent_budget(selected.project)
    load_ref = signer.issue(
        revision=revision,
        scope=context.reference_scope,
        purpose="load",
        ttl_seconds=60,
    )
    adapter = _local_cli_adapter(
        service, context=context, budget=budget, project=selected.project
    )
    loaded = _local_cli_dispatch(
        adapter,
        "capability.load",
        {
            "capability_ref": load_ref,
            "task_id": "local-cli",
            "section_names": (
                None
                if section_names is None
                else list(cast(Iterable[JsonValue], section_names))
            ),
            "operation_names": (
                None
                if operation_names is None
                else list(cast(Iterable[JsonValue], operation_names))
            ),
            "max_output_tokens": max_output_tokens,
        },
    )
    context_state = _context_state(selected.project)
    context_evictions: list[JsonValue] = []
    sections = cast(list[JsonValue], loaded["sections"])
    for raw_section in sections:
        section = cast(dict[str, JsonValue], raw_section)
        evictions = context_state.add(
            ResidentSection(
                key=f"{loaded['revision']}::{section['name']}",
                revision=cast(str, loaded["revision"]),
                section=cast(str, section["name"]),
                portable_tokens=cast(int, section["portable_tokens"]),
                sensitive=cast(bool, section["sensitive"]),
            )
        )
        context_evictions.extend(
            {
                "key": eviction.key,
                "portable_tokens": eviction.portable_tokens,
                "reason": eviction.reason,
            }
            for eviction in evictions
        )
    resident = context_state.snapshot()
    return {
        "budget": _budget_json(budget.snapshot()),
        "dependency": _dependency_decision_json(dependency),
        "execution_ref": None,
        "execution_requires_same_process_session": bool(loaded["execution_ref"]),
        "omitted_sections": loaded["omitted_sections"],
        "operations": loaded["operations"],
        "permissions": loaded["permissions"],
        "portable_tokens": loaded["portable_tokens"],
        "revision": loaded["revision"],
        "context": {
            "evictions": context_evictions,
            "generation": resident.generation,
            "used_portable_tokens": resident.used_portable_tokens,
        },
        "sections": sections,
    }


def local_context(
    project_root: str | Path | None = None,
) -> dict[str, JsonValue]:
    """Return the durable metadata-only view of currently resident sections."""

    snapshot = _context_state(_catalog_project(project_root)).snapshot()
    return {
        "entries": [_jsonable(entry) for entry in snapshot.entries],
        "generation": snapshot.generation,
        "max_portable_tokens": snapshot.max_portable_tokens,
        "used_portable_tokens": snapshot.used_portable_tokens,
    }


def local_context_action(
    action: str,
    key: str,
    *,
    project_root: str | Path | None = None,
) -> dict[str, JsonValue]:
    """Access, pin, unpin, or forget one resident metadata entry."""

    state = _context_state(_catalog_project(project_root))
    if action == "access":
        state.access(key)
    elif action == "pin":
        state.pin(key)
    elif action == "unpin":
        state.pin(key, False)
    elif action == "remove":
        state.remove(key)
    else:
        raise ValueError("context action must be access, pin, unpin, or remove")
    return local_context(project_root)


def local_reasoning(
    task_id: str,
    *,
    action: str = "state",
    eligible_tiers: list[str] | None = None,
    risk: str = "none",
    policy_minimum: str = "low",
    escalation_reason: str | None = None,
    attempt_id: str | None = None,
    evidence_id: str | None = None,
    project_root: str | Path | None = None,
) -> dict[str, JsonValue]:
    """Query or update durable budget-aware reasoning advice for one task."""

    project = _catalog_project(project_root)
    orchestrator = ReasoningOrchestrator(
        router=ReasoningRouter(policy_revision="local-reasoning-v1"),
        budget=_persistent_budget(project),
        store=SQLiteReasoningStore(_state_path(project)),
    )
    if action == "state":
        return orchestrator.state(task_id)
    if action == "reset":
        orchestrator.reset(task_id)
        return orchestrator.state(task_id)
    if action != "recommend":
        raise ValueError("reasoning action must be state, recommend, or reset")
    recommendation = orchestrator.recommend(
        task_id=task_id,
        eligible_tiers=(ReasoningTier(value) for value in eligible_tiers)
        if eligible_tiers
        else None,
        risk=SideEffect(risk),
        policy_minimum=ReasoningTier(policy_minimum),
        escalation_reason=escalation_reason,
        attempt_signature=attempt_id,
        evidence_signature=evidence_id,
    )
    return cast(dict[str, JsonValue], _jsonable(recommendation))


_CONFIGURED_PROVIDER = object()


def local_execute(
    revision: str,
    operation: str,
    arguments: dict[str, JsonValue],
    *,
    granted_permissions: list[str] | None = None,
    parameter_authorizer: ParameterAuthorizer | None = None,
    approval_id: str | None = None,
    allow_irreversible: bool = False,
    idempotency_key: str | None = None,
    deadline_ms: int = 30_000,
    max_output_tokens: int = 2_000,
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Execute through an explicitly configured project provider."""

    return _local_execute(
        revision,
        operation,
        arguments,
        _CONFIGURED_PROVIDER,
        granted_permissions=granted_permissions,
        parameter_authorizer=parameter_authorizer,
        approval_id=approval_id,
        allow_irreversible=allow_irreversible,
        idempotency_key=idempotency_key,
        deadline_ms=deadline_ms,
        max_output_tokens=max_output_tokens,
        project_root=project_root,
        monitor=monitor,
    )


def local_execute_static(
    revision: str,
    operation: str,
    arguments: dict[str, JsonValue],
    fixture_output: JsonValue,
    *,
    granted_permissions: list[str] | None = None,
    parameter_authorizer: ParameterAuthorizer | None = None,
    approved: bool = False,
    allow_irreversible: bool = False,
    idempotency_key: str | None = None,
    max_output_tokens: int = 2_000,
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Execute one deterministic static fixture through load, policy, and budget gates."""

    return _local_execute(
        revision,
        operation,
        arguments,
        fixture_output,
        granted_permissions=granted_permissions,
        parameter_authorizer=parameter_authorizer,
        approved=approved,
        approval_id=None,
        allow_irreversible=allow_irreversible,
        idempotency_key=idempotency_key,
        max_output_tokens=max_output_tokens,
        project_root=project_root,
        monitor=monitor,
    )


def _local_execute(
    revision: str,
    operation: str,
    arguments: dict[str, JsonValue],
    fixture_output: JsonValue | object,
    *,
    granted_permissions: list[str] | None = None,
    parameter_authorizer: ParameterAuthorizer | None = None,
    approved: bool = False,
    approval_id: str | None = None,
    allow_irreversible: bool = False,
    idempotency_key: str | None = None,
    deadline_ms: int = 30_000,
    max_output_tokens: int = 2_000,
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Shared local execution path for configured and deterministic providers."""

    selected = _select_local_scope(project_root, monitor)
    generation = selected.snapshot()
    manifest = generation.registry.revision(revision)
    providers = generation.providers
    if fixture_output is not _CONFIGURED_PROVIDER:
        provider = StaticProvider(
            (StaticFixture(manifest, {operation: cast(JsonValue, fixture_output)}),),
            name=manifest.provider,
        )
        providers = (provider,)
    def dependency_decider(selected_operation: DependencyOperation) -> DegradedDecision:
        return _local_dependency_decision(
            generation,
            selected_operation,
            provider_name=manifest.provider,
            providers=providers,
        )
    signer = ReferenceSigner(token_bytes(32))
    audit = _audit_sink(selected.project)
    service = CapabilityHubService(
        registry=generation.registry,
        providers=providers,
        references=signer,
        audit=audit,
        idempotency_store=_local_idempotency_store(
            selected.project,
            persist_results=fixture_output is _CONFIGURED_PROVIDER,
        ),
        provider_supervisor=_local_provider_supervisor(selected.project),
        provider_executor=_local_provider_executor(selected.project),
        retry_certainty_classifier=classify_adapter_failure,
        dependency_decider=dependency_decider,
    )
    context = _local_context(
        granted_permissions,
        parameter_authorizer=parameter_authorizer,
        allow_irreversible=allow_irreversible,
        deadline_ms=deadline_ms,
    )
    budget = _persistent_budget(selected.project)
    adapter = _local_cli_adapter(
        service, context=context, budget=budget, project=selected.project
    )
    correlation_id = token_bytes(16).hex()
    load_ref = signer.issue(
        revision=revision,
        scope=context.reference_scope,
        purpose="load",
        ttl_seconds=60,
    )
    loaded = _local_cli_dispatch(
        adapter,
        "capability.load",
        {
            "capability_ref": load_ref,
            "task_id": "local-cli",
            "section_names": [],
            "operation_names": [operation],
            "max_output_tokens": max_output_tokens,
        },
        correlation_id=correlation_id,
    )
    operation_spec = manifest.operation(operation)
    if operation_spec is None:
        raise CapabilityHubError(
            code="unknown_operation",
            category=ErrorCategory.REFERENCE,
            safe_message="The capability does not declare this operation.",
        )
    if approval_id is not None:
        intent = _approval_intent(
            revision,
            operation,
            arguments,
            context=context,
            side_effect=operation_spec.side_effect.value,
        )
        ScopedApprovalStore(
            _state_path(selected.project), scope_key=_tenant_scope_key(selected.project)
        ).consume(_scope_from_context(context, "local-cli"), approval_id, intent)
    approval_ref = (
        service.issue_approval(
            revision=revision,
            operation=operation,
            arguments=arguments,
            task_id="local-cli",
            context=context,
            ttl_seconds=60,
        )
        if approved or approval_id is not None
        else None
    )
    execution_ref = loaded["execution_ref"]
    if not isinstance(execution_ref, str):
        raise CapabilityHubError(
            code="execution_unavailable",
            category=ErrorCategory.REFERENCE,
            safe_message="The capability does not expose an executable operation.",
        )
    result = _local_cli_dispatch(
        adapter,
        "capability.execute",
        {
            "execution_ref": execution_ref,
            "operation": operation,
            "arguments": arguments,
            "task_id": "local-cli",
            "approval_ref": approval_ref,
            "idempotency_key": idempotency_key,
            "max_output_tokens": max_output_tokens,
        },
        correlation_id=correlation_id,
    )
    return {
        "audit_id": result["audit_id"],
        "budget": _budget_json(budget.snapshot()),
        "capability_revision": result["capability_revision"],
        "dependency": _dependency_decision_json(
            dependency_decider(DependencyOperation.EXECUTE)
        ),
        "operation": result["operation"],
        "output": result["output"],
        "portable_tokens": result["portable_tokens"],
        "provider": result["provider"],
    }


def local_approval_request(
    revision: str,
    operation: str,
    arguments: dict[str, JsonValue],
    *,
    ttl_seconds: int = 300,
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Create a durable approval request bound to one exact local execution intent."""

    selected = _select_local_scope(project_root, monitor)
    manifest = selected.snapshot().registry.revision(revision)
    operation_spec = manifest.operation(operation)
    if operation_spec is None:
        raise CapabilityHubError(
            code="unknown_operation",
            category=ErrorCategory.REFERENCE,
            safe_message="The capability does not declare this operation.",
        )
    context = _local_context(None)
    intent = _approval_intent(
            revision,
            operation,
            arguments,
            context=context,
            side_effect=operation_spec.side_effect.value,
        )
    record = ScopedApprovalStore(
        _state_path(selected.project), scope_key=_tenant_scope_key(selected.project)
    ).request(
        _scope_from_context(context, "local-cli"),
        intent,
        ttl_seconds=ttl_seconds,
    )
    return _approval_json(record)


def local_approvals(
    project_root: str | Path | None = None,
    *,
    status: ApprovalStatus | str | None = None,
    limit: int = 50,
) -> dict[str, JsonValue]:
    """Return a bounded approval queue without argument bodies or digests."""

    project = _catalog_project(project_root)
    context = _local_context(None)
    records = ScopedApprovalStore(
        _state_path(project), scope_key=_tenant_scope_key(project)
    ).list(_scope_from_context(context, "local-cli"), status=status, limit=limit)
    return {"approvals": [_approval_json(record) for record in records], "count": len(records)}


def local_approval_decide(
    approval_id: str,
    decision: str,
    *,
    project_root: str | Path | None = None,
) -> dict[str, JsonValue]:
    """Approve or deny one pending request as the local operator."""

    project = _catalog_project(project_root)
    context = _local_context(None)
    scope = _scope_from_context(context, "local-cli")
    store = ScopedApprovalStore(_state_path(project), scope_key=_tenant_scope_key(project))
    if decision == "approve":
        record = store.approve(scope, approval_id, decided_by="local-operator")
    elif decision == "deny":
        record = store.deny(scope, approval_id, decided_by="local-operator")
    else:
        raise ValueError("decision must be approve or deny")
    return _approval_json(record)


def local_budget_report(
    limits: dict[str, int] | None = None,
    project_root: str | Path | None = None,
) -> dict[str, JsonValue]:
    """Return or configure the durable local CLI budget without scanning the catalog."""

    project = _catalog_project(project_root)
    repository = SqliteBudgetRepository(_state_path(project))
    ledger = repository.ledger("local-cli", DEFAULT_LOCAL_BUDGETS)
    if limits:
        ledger = repository.configure("local-cli", limits)
    report = _budget_json(ledger.snapshot())
    report["persistent"] = True
    report["storage"] = "sqlite"
    return report


def local_benchmark(*, enforce_thresholds: bool = True) -> dict[str, JsonValue]:
    """Run the packaged deterministic disclosure benchmark."""

    from benchmarks.harness import assert_release_thresholds, run_benchmark

    report = run_benchmark()
    if enforce_thresholds:
        assert_release_thresholds(report)
    payload = _jsonable(report)
    assert isinstance(payload, dict)
    payload["thresholds_passed"] = True if enforce_thresholds else None
    return payload


def local_scale_benchmark() -> dict[str, JsonValue]:
    """Run the synthetic 10k metadata and 100-concurrent-read evidence path."""

    from benchmarks.scale import run_scale_benchmark

    payload = _jsonable(run_scale_benchmark())
    assert isinstance(payload, dict)
    return payload


def local_dashboard(
    project_root: str | Path | None = None,
    *,
    port: int = 0,
    monitor: LocalCatalogMonitor | None = None,
) -> DashboardServer:
    """Start a live local dashboard backed by one local catalog monitor."""

    selected = _select_local_scope(project_root, monitor)

    def snapshot() -> StatusSnapshot:
        inventory = local_inventory(monitor=selected)
        preferences = local_preferences(monitor=selected)
        loaded = local_loaded(limit=10, monitor=selected)
        return {
            "active_capabilities": [],
            "health": local_health(selected.project),
            "inventory": inventory,
            "connections": local_connections(monitor=selected),
            "loaded_capabilities": loaded.get("entries", []),
            "lifecycle": local_lifecycle(monitor=selected),
            "audit": local_audit(limit=10, monitor=selected),
            "preferences": {"locale": preferences.get("locale", "auto")},
            "providers": local_providers(monitor=selected),
            "approvals": local_approvals(selected.project, limit=10),
            "context": local_context(selected.project),
            "reasoning": local_reasoning("dashboard", project_root=selected.project),
            "updates": local_updates(monitor=selected),
            "secure_audit": {
                "configured": True,
                "key_environment": SECURE_AUDIT_KEY_ENV,
            },
        }

    def search(query: str, kind: str | None, limit: int) -> StatusSnapshot:
        return local_search(
            query,
            kinds=[kind] if kind is not None else None,
            limit=limit,
            monitor=selected,
        )

    def lifecycle(coordinate: str, state: str) -> StatusSnapshot:
        return local_set_lifecycle(coordinate, state, scope="project", monitor=selected)

    def language(locale: str) -> StatusSnapshot:
        return local_set_locale(locale, scope="project", monitor=selected)

    def approval(approval_id: str, decision: str) -> StatusSnapshot:
        return local_approval_decide(approval_id, decision, project_root=selected.project)

    def context(action: str, key: str) -> StatusSnapshot:
        return local_context_action(action, key, project_root=selected.project)

    return dashboard(
        snapshot,
        port=port,
        search_provider=search,
        lifecycle_provider=lifecycle,
        language_provider=language,
        approval_provider=approval,
        context_provider=context,
    )


@dataclass(frozen=True, slots=True)
class _HttpServiceGeneration:
    service: DrainedCapabilityHubService
    activations: dict[str, str]


class _RefreshingDrainedHttpService:
    """Route requests through immutable catalog generations with safe draining."""

    def __init__(
        self,
        base: CapabilityHubService,
        *,
        monitor: LocalCatalogMonitor,
        references: ReferenceSigner,
        update_store: SQLiteUpdateStore,
        cancellable: Callable[[CapabilityManifest, OperationSpec], bool],
        cancel: Callable[[str], bool] | None,
        drain_timeout_seconds: float,
    ) -> None:
        if drain_timeout_seconds < 0:
            raise ValueError("drain_timeout_seconds must be non-negative")
        self._base = base
        self._monitor = monitor
        self._references = references
        self._update_store = update_store
        self._cancellable = cancellable
        self._cancel = cancel
        self._drain_timeout_seconds = drain_timeout_seconds
        self._lock = RLock()
        self._timers: list[Timer] = []
        self._closed = False
        snapshot = monitor.snapshot(force=True)
        for coordinate, revision in snapshot.registry.activations.items():
            update_store.bootstrap_active(coordinate, revision)
        self._current = self._make_generation(base, snapshot.registry)

    def search(
        self,
        query: str,
        *,
        task_id: str,
        context: ServiceContext,
        budget: BudgetLedger,
        kinds: Iterable[CapabilityKind | str] | None = None,
        limit: int = 8,
        max_output_tokens: int = 900,
        include_cards: bool = True,
        inventory: dict[str, JsonValue] | None = None,
    ) -> SearchResponse:
        return self._service().search(
            query,
            task_id=task_id,
            context=context,
            budget=budget,
            kinds=kinds,
            limit=limit,
            max_output_tokens=max_output_tokens,
            include_cards=include_cards,
            inventory=inventory,
        )

    def load(
        self,
        capability_ref: str,
        *,
        task_id: str,
        context: ServiceContext,
        budget: BudgetLedger,
        section_names: Iterable[str] | None = None,
        operation_names: Iterable[str] | None = None,
        max_output_tokens: int = 2_000,
    ) -> LoadedCapability:
        return self._service().load(
            capability_ref,
            task_id=task_id,
            context=context,
            budget=budget,
            section_names=section_names,
            operation_names=operation_names,
            max_output_tokens=max_output_tokens,
        )

    def execute(
        self,
        request: ExecutionRequest,
        *,
        context: ServiceContext,
        budget: BudgetLedger,
        max_output_tokens: int | None = None,
    ) -> ExecutionResult:
        return self._service().execute(
            request,
            context=context,
            budget=budget,
            max_output_tokens=max_output_tokens,
        )

    def inventory(self) -> dict[str, JsonValue]:
        with self._lock:
            return self._monitor.snapshot(force=True).inventory_json()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            timers, self._timers = self._timers, []
        for timer in timers:
            timer.cancel()

    def _service(self) -> DrainedCapabilityHubService:
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP service lifecycle is closed")
            snapshot = self._monitor.snapshot(force=True)
            activations = dict(snapshot.registry.activations)
            if activations != self._current.activations:
                next_service = self._base.fork_catalog(
                    registry=snapshot.registry,
                    providers=snapshot.providers,
                )
                previous = self._current
                self._current = self._make_generation(next_service, snapshot.registry)
                self._drain_changed(previous, activations)
            return self._current.service

    def _make_generation(
        self,
        service: CapabilityHubService,
        registry: CapabilityRegistry,
    ) -> _HttpServiceGeneration:
        drain = DrainController()
        activations = dict(registry.activations)
        for coordinate, revision in activations.items():
            drain.register(coordinate, revision)
        wrapped = DrainedCapabilityHubService(
            service,
            drain=drain,
            resolver=SignedExecutionBindingResolver(
                references=self._references,
                registry=registry,
                cancellable=self._cancellable,
            ),
            cancel=self._cancel,
            pin_id_factory=lambda request, _binding: request.execution_ref,
            pin_revision=lambda coordinate, pin_id: self._update_store.pin_active(
                coordinate, pin_id
            ).revision,
            release_revision=self._update_store.release_pin,
        )
        return _HttpServiceGeneration(wrapped, activations)

    def _drain_changed(
        self,
        previous: _HttpServiceGeneration,
        active: Mapping[str, str],
    ) -> None:
        for coordinate, revision in previous.activations.items():
            if active.get(coordinate) == revision:
                continue
            snapshot = previous.service.begin_drain(coordinate, revision)
            if snapshot.state is LifecycleState.RETIRED:
                continue
            deadline = monotonic() + self._drain_timeout_seconds
            timer = Timer(
                self._drain_timeout_seconds,
                self._advance_safely,
                args=(previous.service, coordinate, revision, deadline),
            )
            timer.daemon = True
            self._timers.append(timer)
            timer.start()

    def _advance_safely(
        self,
        service: DrainedCapabilityHubService,
        coordinate: str,
        revision: str,
        deadline: float,
    ) -> None:
        try:
            remaining = deadline - monotonic()
            if remaining > 0:
                self._schedule_advance(service, coordinate, revision, deadline, remaining)
                return
            dispatch = service.advance_drain(coordinate, revision, deadline=deadline)
            if dispatch.progress.outcome is DrainOutcome.WAITING:
                self._schedule_advance(service, coordinate, revision, deadline, 0.01)
        except Exception:
            return

    def _schedule_advance(
        self,
        service: DrainedCapabilityHubService,
        coordinate: str,
        revision: str,
        deadline: float,
        delay: float,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            timer = Timer(
                max(delay, 0.001),
                self._advance_safely,
                args=(service, coordinate, revision, deadline),
            )
            timer.daemon = True
            self._timers.append(timer)
            timer.start()


class _LifecycleLoopbackHttpControl(LoopbackHttpControl):
    def __init__(self, *args: object, lifecycle: _RefreshingDrainedHttpService, **kwargs: object):
        self._lifecycle = lifecycle
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        super().close()
        self._lifecycle.close()


def local_http_control(
    project_root: str | Path | None = None,
    *,
    port: int = 0,
    granted_permissions: list[str] | None = None,
    tenant_id: str = "local",
    principal_id: str = "operator",
    session_id: str = "http",
    task_budget_limits: Mapping[str, int] | None = None,
    monitor: LocalCatalogMonitor | None = None,
    drain_timeout_seconds: float = 30.0,
    execution_cancellable: Callable[[CapabilityManifest, OperationSpec], bool] | None = None,
    cancel_execution: Callable[[str], bool] | None = None,
) -> tuple[LoopbackHttpControl, HttpControlAccess]:
    """Start loopback HTTP with durable tenant/principal/session/task budgets."""

    selected = _select_local_scope(project_root, monitor)
    generation = selected.snapshot()
    references = ReferenceSigner(token_bytes(32))
    supervisor = _local_provider_supervisor(selected.project)
    identity = AuthIdentity(tenant_id, principal_id, "http-loopback", session_id)
    service = CapabilityHubService(
        registry=generation.registry,
        providers=generation.providers,
        references=references,
        audit=ScopedAuditSink(
            _scoped_state(selected.project),
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            session_id=identity.session_id,
            identity_source=identity.source,
            delegate=_audit_sink(selected.project),
        ),
        idempotency_store=_local_idempotency_store(
            selected.project, scope_key=_tenant_scope_key(selected.project)
        ),
        provider_supervisor=supervisor if cancel_execution is None else None,
        provider_executor=_local_provider_executor(selected.project),
        retry_certainty_classifier=classify_adapter_failure,
    )
    context = ServiceContext(
        identity.tenant_id,
        identity.principal_id,
        identity.session_id,
        granted_permissions=frozenset(granted_permissions or ()),
    )
    selected_task_limits = {
        **DEFAULT_LOCAL_BUDGETS,
        **(dict(task_budget_limits) if task_budget_limits is not None else {}),
    }
    budget_provider = DurableHierarchicalBudgetProvider(
        SQLiteHierarchicalBudgetStore(
            _state_path(selected.project),
            hmac_key=load_or_create_hmac_key(_budget_hmac_key_path(selected.project)),
        ),
        tenant_scope=context.tenant_id,
        principal_scope=context.principal_id,
        session_scope=context.session_id,
        aggregate_limits=DEFAULT_LOCAL_HTTP_AGGREGATE_BUDGETS,
        task_limits=selected_task_limits,
    )
    lifecycle_service = _RefreshingDrainedHttpService(
        service,
        monitor=selected,
        references=references,
        update_store=SQLiteUpdateStore(_state_path(selected.project)),
        cancellable=execution_cancellable or (lambda _manifest, _operation: True),
        cancel=cancel_execution or supervisor.cancel,
        drain_timeout_seconds=drain_timeout_seconds,
    )
    adapter = CapabilityHubServiceAdapter(
        cast(CapabilityHubService, lifecycle_service),
        kind=AdapterKind.HTTP,
        context_provider=lambda: context,
        budget_provider=budget_provider,
        inventory_provider=lifecycle_service.inventory,
        observability=_runtime_observability(selected.project),
    )
    control = _LifecycleLoopbackHttpControl(
        adapter,
        port=port,
        lifecycle=lifecycle_service,
        identity=identity,
    )
    return control, control.start()


class _LocalAdminBackend:
    def __init__(self, project: Path, monitor: LocalCatalogMonitor | None) -> None:
        self._project = project
        self._monitor = monitor

    def dispatch(
        self,
        operation: str,
        payload: Mapping[str, JsonValue],
        identity: AuthIdentity,
    ) -> JsonValue:
        if operation == "lifecycle.list":
            _admin_payload(payload)
            return local_lifecycle(self._project, monitor=self._monitor)
        if operation == "lifecycle.set":
            _admin_payload(payload, required=("coordinate", "state"), optional=("scope",))
            return local_set_lifecycle(
                _admin_text(payload, "coordinate"),
                _admin_text(payload, "state"),
                scope=_admin_text(payload, "scope", default="project"),
                project_root=self._project,
                monitor=self._monitor,
            )
        if operation == "update.list":
            _admin_payload(payload, optional=("limit",))
            return local_updates(
                self._project,
                limit=_admin_int(payload, "limit", default=100),
                monitor=self._monitor,
            )
        if operation.startswith("update."):
            action = operation.removeprefix("update.")
            optional = ("expected_active_revision", "health_passed")
            _admin_payload(payload, required=("target",), optional=optional)
            expected = payload.get("expected_active_revision")
            health = payload.get("health_passed")
            if expected is not None and not isinstance(expected, str):
                raise ValueError("expected_active_revision must be text")
            if health is not None and not isinstance(health, bool):
                raise ValueError("health_passed must be boolean")
            return local_update_action(
                action,
                _admin_text(payload, "target"),
                expected_active_revision=expected,
                health_passed=health,
                project_root=self._project,
                monitor=self._monitor,
            )
        if operation == "approval.list":
            _admin_payload(payload, required=("task_id",), optional=("status", "limit"))
            task_id = _admin_text(payload, "task_id")
            status = payload.get("status")
            if status is not None and not isinstance(status, str):
                raise ValueError("status must be text")
            records = ScopedApprovalStore(
                _state_path(self._project), scope_key=_tenant_scope_key(self._project)
            ).list(
                _tenant_scope(identity, task_id),
                status=status,
                limit=_admin_int(payload, "limit", default=50),
            )
            return {
                "approvals": [_approval_json(record) for record in records],
                "count": len(records),
            }
        if operation == "approval.decide":
            _admin_payload(
                payload,
                required=("task_id", "approval_id", "decision"),
            )
            task_id = _admin_text(payload, "task_id")
            approval_id = _admin_text(payload, "approval_id")
            decision = _admin_text(payload, "decision")
            store = ScopedApprovalStore(
                _state_path(self._project), scope_key=_tenant_scope_key(self._project)
            )
            scope = _tenant_scope(identity, task_id)
            if decision == "approve":
                record = store.approve(scope, approval_id, decided_by=identity.principal_id)
            elif decision == "deny":
                record = store.deny(scope, approval_id, decided_by=identity.principal_id)
            else:
                raise ValueError("decision must be approve or deny")
            return _approval_json(record)
        if operation == "audit.query":
            _admin_payload(payload, required=("task_id",), optional=("limit",))
            return local_audit(
                self._project,
                limit=_admin_int(payload, "limit", default=50),
                monitor=self._monitor,
                identity=identity,
                task_id=_admin_text(payload, "task_id"),
            )
        if operation in {"policy.query", "policy.set"}:
            _admin_payload(
                payload,
                required=("task_id",),
                optional=("rules",) if operation == "policy.set" else (),
            )
            scope = _tenant_scope(identity, _admin_text(payload, "task_id"))
            state = _scoped_state(self._project)
            if operation == "policy.set":
                rules = payload.get("rules")
                if not isinstance(rules, dict):
                    raise ValueError("rules must be an object")
                state.set(scope, "rules", rules, namespace="admin-policy")
            return {"rules": state.get(scope, "rules", namespace="admin-policy") or {}}
        raise ValueError("unsupported administration operation")


def local_admin_control(
    project_root: str | Path | None = None,
    *,
    roles: Iterable[str] = ("auditor",),
    tenant_id: str = "local",
    principal_id: str = "operator",
    session_id: str = "admin",
    port: int = 0,
    token_ttl_seconds: int = 60,
    monitor: LocalCatalogMonitor | None = None,
) -> tuple[LoopbackAdminControl, AdminControlAccess]:
    """Start the separate administration plane; its token is never a data credential."""

    project = _catalog_project(project_root)
    identity = AuthIdentity(tenant_id, principal_id, "admin-loopback", session_id)
    principal = AdminPrincipal(identity, frozenset(roles))
    control = LoopbackAdminControl(
        _LocalAdminBackend(project, monitor),
        principal,
        port=port,
        token_ttl_seconds=token_ttl_seconds,
        audit=ScopedAuditSink(
            _scoped_state(project),
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            session_id=identity.session_id,
            identity_source=identity.source,
            delegate=_audit_sink(project),
        ),
    )
    return control, control.start()


def mcp_serve(project_root: str | Path | None = None) -> object:
    """Start optional MCP serving only when the separately implemented server exists."""
    try:
        module = import_module("capabilityhub.mcp_server")
    except ModuleNotFoundError as error:
        raise RuntimeError("MCP serving is not installed in this runtime.") from error
    serve = getattr(module, "serve", None)
    if not callable(serve):
        raise RuntimeError("MCP server does not expose a serve function.")
    typed_serve = cast(Callable[..., object], serve)
    project = _project(project_root) if project_root is not None else None
    return typed_serve(project=project)


def _local_provider_supervisor(project: Path) -> ProcessProviderSupervisor:
    selected = project.resolve()
    with _LOCAL_PROVIDER_LOCK:
        supervisor = _LOCAL_PROVIDER_SUPERVISORS.get(selected)
        if supervisor is None:
            supervisor = ProcessProviderSupervisor(strict_local_providers=True)
            _LOCAL_PROVIDER_SUPERVISORS[selected] = supervisor
        return supervisor


def _local_provider_executor(
    project: Path,
) -> ResilientProviderExecutor[ExecutionResult]:
    selected = project.resolve()
    with _LOCAL_PROVIDER_LOCK:
        executor = _LOCAL_PROVIDER_EXECUTORS.get(selected)
        if executor is None:
            executor = ResilientProviderExecutor(
                retry_policy=RetryPolicy(max_attempts=2),
                circuit_breaker=CircuitBreaker(),
            )
            _LOCAL_PROVIDER_EXECUTORS[selected] = executor
        return executor


def _local_dependency_decision(
    generation: LocalCatalogGeneration,
    operation: DependencyOperation,
    *,
    provider_name: str | None = None,
    providers: tuple[CapabilityProvider, ...] | None = None,
) -> DegradedDecision:
    fallbacks: tuple[SafeFallback, ...] = ()
    if operation is DependencyOperation.SEARCH:
        fallbacks = tuple(
            SafeFallback(
                operation,
                dependency,
                "last_good_catalog",
                statuses=(DependencyStatus.STALE,),
                max_age_seconds=LOCAL_LAST_GOOD_MAX_AGE_SECONDS,
            )
            for dependency in (Dependency.REGISTRY, Dependency.INDEX)
        )
    elif operation is DependencyOperation.LOAD:
        fallbacks = (
            SafeFallback(
                operation,
                Dependency.REGISTRY,
                "last_good_catalog",
                statuses=(DependencyStatus.STALE,),
                max_age_seconds=LOCAL_LAST_GOOD_MAX_AGE_SECONDS,
            ),
            SafeFallback(
                operation,
                Dependency.PROVIDER,
                "manifest_only_load",
                statuses=(DependencyStatus.UNAVAILABLE, DependencyStatus.UNKNOWN),
            ),
        )
    observations = local_dependency_observations(
        generation,
        policy_available=True,
        provider_name=provider_name,
        providers=providers,
    )
    return DegradedModePolicy().decide(
        operation,
        observations,
        safe_fallbacks=fallbacks,
    )


def _dependency_decision_json(decision: DegradedDecision) -> dict[str, JsonValue]:
    return {
        "fallbacks_used": list(decision.fallbacks_used),
        "operation": decision.operation.value,
        "outcome": decision.outcome.value,
        "reasons": list(decision.reasons),
        "statuses": {
            item.dependency.value: item.effective_status.value for item in decision.dependencies
        },
    }


def _project(value: str | Path | None) -> Path:
    return Path(value).resolve() if value is not None else Path.cwd().resolve()


def _catalog_project(value: str | Path | None) -> Path:
    project = _project(value)
    if value is not None and not project.is_dir():
        raise ValueError("project_root must be an existing directory")
    return project


def _select_local_scope(
    project_root: str | Path | None, monitor: LocalCatalogMonitor | None
) -> LocalCatalogMonitor:
    if monitor is None:
        return LocalCatalogMonitor(project=_catalog_project(project_root))
    if project_root is not None and _catalog_project(project_root) != monitor.project:
        raise ValueError("project_root does not match the supplied local monitor")
    return monitor


def _update_manager(
    project_root: str | Path | None,
    monitor: LocalCatalogMonitor | None,
    *,
    verifier: SupplyChainVerifier | None = None,
    material: ArtifactMaterial | None = None,
) -> tuple[LocalCatalogMonitor, StagedUpdateManager]:
    selected = _select_local_scope(project_root, monitor)
    generation = selected.snapshot()
    return selected, StagedUpdateManager(
        registry=generation.registry,
        store=SQLiteUpdateStore(_state_path(selected.project)),
        verifier=verifier,
        artifact_acquirer=(None if material is None else lambda _revision: material),
    )


def _bootstrap_update_pointer(manager: StagedUpdateManager, coordinate: str) -> None:
    state = manager.state(coordinate)
    if state.active_revision is not None or state.staged_revision is not None:
        return
    active = manager.registry.activations.get(coordinate)
    if active is not None:
        manager.bootstrap_active(coordinate, active)


def _local_context(
    granted_permissions: list[str] | None,
    *,
    parameter_authorizer: ParameterAuthorizer | None = None,
    allow_irreversible: bool = False,
    deadline_ms: int = 30_000,
) -> ServiceContext:
    permissions = frozenset(granted_permissions or ())
    if parameter_authorizer is not None:
        permissions = (
            parameter_authorizer.granted_permissions
            if granted_permissions is None
            else permissions.intersection(parameter_authorizer.granted_permissions)
        )
    return ServiceContext(
        "local",
        "operator",
        "cli",
        deadline_ms=deadline_ms,
        granted_permissions=permissions,
        parameter_authorizer=parameter_authorizer,
        allow_irreversible=allow_irreversible,
    )


def _budget_json(snapshot: BudgetSnapshot) -> dict[str, JsonValue]:
    return {
        "limits": dict(snapshot.limits),
        "remaining": dict(snapshot.remaining),
        "reserved": dict(snapshot.reserved),
        "scope": snapshot.scope,
        "used": dict(snapshot.used),
    }


def _persistent_budget(project: Path) -> SqliteBudgetLedger:
    return SqliteBudgetRepository(_state_path(project)).ledger("local-cli", DEFAULT_LOCAL_BUDGETS)


def _context_state(project: Path) -> LocalContextState:
    return LocalContextState(
        project / ".capabilityhub" / "context-state.json",
        max_portable_tokens=DEFAULT_CONTEXT_TOKENS,
    )


def _approval_intent(
    revision: str,
    operation: str,
    arguments: Mapping[str, JsonValue],
    *,
    context: ServiceContext,
    side_effect: str,
) -> ApprovalIntent:
    return ApprovalIntent.from_arguments(
        revision=revision,
        operation=operation,
        arguments=arguments,
        tenant_id=context.tenant_id,
        principal_id=context.principal_id,
        session_id=context.session_id,
        task_id="local-cli",
        side_effect=side_effect,
        policy_revision=LOCAL_POLICY_REVISION,
    )


def _approval_json(record: ApprovalRecord) -> dict[str, JsonValue]:
    return {
        "approval_id": record.approval_id,
        "created_at": record.created_at,
        "decided_at": record.decided_at,
        "decided_by": record.decided_by,
        "expires_at": record.expires_at,
        "operation": record.intent.operation,
        "revision": record.intent.revision,
        "side_effect": record.intent.side_effect,
        "status": record.status.value,
        "task_id": record.intent.task_id,
    }


def _audit_path(project: Path) -> Path:
    return project / ".capabilityhub" / "audit.jsonl"


def _secure_audit_root(project: Path) -> Path:
    return project / ".capabilityhub" / "secure-audit"


def _secure_audit_key(project: Path) -> bytes:
    raw = os.environ.get(SECURE_AUDIT_KEY_ENV)
    if raw is None:
        return load_or_create_signing_key(project / ".capabilityhub" / "audit-hmac.key")
    key = raw.encode("utf-8")
    if len(key) < 16:
        raise CapabilityHubError(
            code="secure_audit_key_invalid",
            category=ErrorCategory.INPUT,
            safe_message="The secure audit signing key must contain at least 16 UTF-8 bytes.",
        )
    return key


def _audit_sink(project: Path) -> AuditSink:
    try:
        ledger = SecureAuditLedger(
            _secure_audit_root(project) / "current", signing_key=_secure_audit_key(project)
        )
    except Exception as error:
        code = error.code if isinstance(error, CapabilityHubError) else "secure_audit_unavailable"
        return ResilientAuditSink(None, initial_error=code)
    return ResilientAuditSink(ledger)


def _audit_health(project: Path) -> str:
    try:
        verification = SecureAuditLedger(
            _secure_audit_root(project) / "current", signing_key=_secure_audit_key(project)
        ).verify()
    except Exception:
        return "degraded"
    return "ok" if verification.valid else "degraded"


def _audit_events(project: Path, *, limit: int) -> tuple[AuditEvent, ...]:
    _, records = SecureAuditLedger(
        _secure_audit_root(project) / "current", signing_key=_secure_audit_key(project)
    ).verified_records()
    events: list[AuditEvent] = []
    for record in records[-limit:]:
        event = record["event"]
        assert isinstance(event, dict)
        events.append(
            AuditEvent(
                event_id=str(event["event_id"]),
                sequence=int(event["sequence"]),
                task_id=str(event["task_id"]),
                event_type=str(event["event_type"]),
                capability_revision=(
                    str(event["capability_revision"])
                    if event["capability_revision"] is not None
                    else None
                ),
                outcome=str(event["outcome"]),
                portable_tokens=int(event["portable_tokens"]),
                payload_bytes=int(event["payload_bytes"]),
                reason_codes=tuple(str(code) for code in event["reason_codes"]),
                metadata=cast(dict[str, JsonValue], event["metadata"]),
            )
        )
    return tuple(events)


def _runtime_observability(project: Path) -> InMemoryObservability:
    return InMemoryObservability(
        allowed_error_codes=("other_error",),
        span_limit=1_000,
        metric_series_limit=2_000,
        persistent_metrics=SqliteMetricStore(_state_path(project)),
    )


def _state_path(project: Path) -> Path:
    return project / ".capabilityhub" / "state.sqlite3"


def _local_idempotency_store(
    project: Path, *, persist_results: bool = True, scope_key: bytes | None = None
) -> SqliteIdempotencyStore:
    # SQLite WAL initialization takes a transient exclusive lock on first use.
    with _LOCAL_STORE_INIT_LOCK:
        return SqliteIdempotencyStore(
            _state_path(project),
            persist_results=persist_results,
            result_ttl_seconds=300,
            max_result_bytes=1_000_000,
            scope_key=scope_key,
        )


def _budget_hmac_key_path(project: Path) -> Path:
    return project / ".capabilityhub" / "budget-hmac.key"


def _tenant_scope_key(project: Path) -> bytes:
    return load_or_create_hmac_key(project / ".capabilityhub" / "tenant-scope-hmac.key")


def _scoped_state(project: Path) -> SqliteScopedState:
    return SqliteScopedState(_state_path(project), scope_key=_tenant_scope_key(project))


def _tenant_scope(identity: AuthIdentity, task_id: str) -> TenantScope:
    return TenantScope(identity.tenant_id, identity.principal_id, identity.session_id, task_id)


def _scope_from_context(context: ServiceContext, task_id: str) -> TenantScope:
    return TenantScope(context.tenant_id, context.principal_id, context.session_id, task_id)


def _admin_payload(
    payload: Mapping[str, JsonValue],
    *,
    required: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
) -> None:
    keys = set(payload)
    if not set(required) <= keys or not keys <= set((*required, *optional)):
        raise ValueError("administration payload fields are invalid")


def _admin_text(
    payload: Mapping[str, JsonValue], key: str, *, default: str | None = None
) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{key} must be bounded text")
    return value


def _admin_int(payload: Mapping[str, JsonValue], key: str, *, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
        raise ValueError(f"{key} must be from 1 to 500")
    return value


def _jsonable(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    raise TypeError(f"Unsupported benchmark value: {type(value).__name__}")
