"""Small local runtime helpers used by the command-line interface."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from importlib import import_module, metadata, util
from pathlib import Path
from secrets import token_bytes
from typing import cast

from capabilityhub.activation_lock import (
    export_activation_lock,
    validate_activation_lock_json,
)
from capabilityhub.approval_store import (
    ApprovalIntent,
    ApprovalRecord,
    ApprovalStatus,
    SqliteApprovalStore,
)
from capabilityhub.audit import AuditEvent, AuditSink, JsonlAuditSink, read_jsonl_audit
from capabilityhub.budget import BudgetSnapshot
from capabilityhub.budget_store import SqliteBudgetLedger, SqliteBudgetRepository
from capabilityhub.compatibility import (
    FeatureHandshake,
    decide_compatibility,
    v1alpha1_handshake,
)
from capabilityhub.context_state import LocalContextState
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.idempotency import SqliteIdempotencyStore
from capabilityhub.insights import loaded_view, providers_view, routing_view
from capabilityhub.lifecycle import StagedUpdateManager
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.manifest import load_manifest
from capabilityhub.manifest_export import manifest_to_document
from capabilityhub.migration import migrate_manifest
from capabilityhub.models import (
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    JsonValue,
    ReasoningTier,
    SideEffect,
)
from capabilityhub.orchestration import ReasoningOrchestrator
from capabilityhub.providers.skill import SkillProvider
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.reasoning import ReasoningRouter
from capabilityhub.reasoning_store import SQLiteReasoningStore
from capabilityhub.references import ReferenceSigner
from capabilityhub.residency import ResidentSection
from capabilityhub.retention import AuditRetentionManager
from capabilityhub.search import LexicalCapabilitySearch
from capabilityhub.secure_audit import SecureAuditLedger
from capabilityhub.service import CapabilityHubService, ServiceContext
from capabilityhub.state import (
    PreferenceScope,
    resolved_preferences,
    set_lifecycle,
    set_locale,
)
from capabilityhub.supervision import ProcessProviderSupervisor
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
LOCAL_POLICY_REVISION = "local-v1"
DEFAULT_CONTEXT_TOKENS = 16_000
SECURE_AUDIT_KEY_ENV = "CAPABILITYHUB_AUDIT_KEY"


def validate(paths: list[str | Path]) -> int:
    """Validate manifest files and return their count without executing providers."""
    for path in paths:
        load_manifest(path)
    return len(paths)


def local_manifest_export(path: str | Path) -> dict[str, JsonValue]:
    """Export one validated manifest deterministically without invoking a Provider."""

    return manifest_to_document(load_manifest(path))


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


def local_search(
    query: str,
    *,
    kinds: list[str] | None = None,
    limit: int = 8,
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Search compact local metadata and omit task-scoped MCP references."""

    selected = _select_local_scope(project_root, monitor)
    snapshot = selected.snapshot()
    response = LexicalCapabilitySearch(
        snapshot.registry,
        ReferenceSigner(b"capabilityhub-local-cli-search"),
    ).search(
        query,
        scope="local-cli",
        kinds=kinds,
        limit=limit,
        max_output_tokens=900,
    )
    results: list[JsonValue] = [
        {
            "estimated_load_tokens": card.estimated_load_tokens,
            "kind": card.kind.value,
            "match_reason": list(card.match_reason),
            "operations": list(card.operations),
            "revision": card.revision,
            "risk": card.risk.value,
            "summary": card.summary,
            "trust_tier": card.trust_tier,
        }
        for card in response.cards
    ]
    counts: dict[str, JsonValue] = dict(response.kind_counts)
    return {
        "kind_counts": counts,
        "payload_bytes": response.payload_bytes,
        "portable_tokens": response.portable_tokens,
        "query": query,
        "results": results,
        "total_matches": response.total_matches,
        "truncated": response.truncated,
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
    overall = "ok" if project_ok and assets_ok and config_status != "invalid" else "degraded"
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
) -> dict[str, JsonValue]:
    """Describe configured local capability connections without dialing providers."""

    selected = _select_local_scope(project_root, monitor)
    snapshot = selected.snapshot()
    active = snapshot.inventory.get("active_by_kind")
    active_counts = active if isinstance(active, dict) else {}
    connections: list[JsonValue] = []
    for kind in CapabilityKind:
        configured = len(snapshot.registry.by_kind(kind))
        if kind is CapabilityKind.SKILL:
            state = "indexed" if configured else "not_found"
        elif kind is CapabilityKind.CLI:
            state = "available" if configured else "not_found"
        else:
            state = "configured_not_probed" if configured else "not_configured"
        connections.append(
            {
                "active": active_counts.get(kind.value, 0),
                "configured": configured,
                "kind": kind.value,
                "state": state,
            }
        )
    return {
        "connections": connections,
        "generation": snapshot.inventory.get("generation"),
        "network_probes_performed": 0,
        "scope": "configuration_only",
    }


def local_audit(
    project_root: str | Path | None = None,
    *,
    limit: int = 50,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Return a bounded redacted tail of durable project audit events."""

    selected = _select_local_scope(project_root, monitor)
    events = _audit_events(selected.project, limit=limit)
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
    return {"events": rows, "limit": limit, "scope": "project", "stored": len(rows)}


def local_secure_audit(
    action: str = "verify",
    *,
    source: str = "current",
    destination: str | Path | None = None,
    max_segments: int = 10,
    project_root: str | Path | None = None,
) -> dict[str, JsonValue]:
    """Verify, rotate, list, or export the opt-in HMAC audit ledger."""

    project = _catalog_project(project_root)
    key = _secure_audit_key(required=True)
    assert key is not None
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


def local_loaded(
    project_root: str | Path | None = None,
    *,
    limit: int = 20,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Show recent successful loads without retaining capability bodies in memory."""

    selected = _select_local_scope(project_root, monitor)
    generation = selected.snapshot()
    events = _audit_events(selected.project, limit=500)
    return loaded_view(generation.registry, events, limit=limit)


def local_providers(
    project_root: str | Path | None = None,
    *,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Return real Provider groupings from the current local registry."""

    selected = _select_local_scope(project_root, monitor)
    return providers_view(selected.snapshot().registry)


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
) -> dict[str, JsonValue]:
    """Stage, health-gate, activate, rollback, pin, or release an immutable revision."""

    selected, manager = _update_manager(project_root, monitor)
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
    max_output_tokens: int = 2_000,
    project_root: str | Path | None = None,
    monitor: LocalCatalogMonitor | None = None,
) -> dict[str, JsonValue]:
    """Load selected material from one active revision through the service boundary."""

    selected = _select_local_scope(project_root, monitor)
    generation = selected.snapshot()
    signer = ReferenceSigner(token_bytes(32))
    audit = _audit_sink(selected.project)
    service = CapabilityHubService(
        registry=generation.registry,
        providers=generation.providers,
        references=signer,
        audit=audit,
    )
    context = _local_context(granted_permissions)
    budget = _persistent_budget(selected.project)
    load_ref = signer.issue(
        revision=revision,
        scope=context.reference_scope,
        purpose="load",
        ttl_seconds=60,
    )
    loaded = service.load(
        load_ref,
        task_id="local-cli",
        context=context,
        budget=budget,
        section_names=section_names,
        operation_names=operation_names,
        max_output_tokens=max_output_tokens,
    )
    context_state = _context_state(selected.project)
    context_evictions: list[JsonValue] = []
    for section in loaded.sections:
        evictions = context_state.add(
            ResidentSection(
                key=f"{loaded.revision}::{section.name}",
                revision=loaded.revision,
                section=section.name,
                portable_tokens=section.portable_tokens,
                sensitive=section.sensitive,
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
        "execution_ref": None,
        "execution_requires_same_process_session": bool(loaded.execution_ref),
        "omitted_sections": list(loaded.omitted_sections),
        "operations": [
            {
                "input_schema": dict(operation.input_schema),
                "name": operation.name,
                "operation_type": operation.operation_type.value,
                "output_schema": dict(operation.output_schema),
                "requires_approval": operation.requires_approval,
                "side_effect": operation.side_effect.value,
            }
            for operation in loaded.operations
        ],
        "permissions": list(loaded.permissions),
        "portable_tokens": loaded.portable_tokens,
        "revision": loaded.revision,
        "context": {
            "evictions": context_evictions,
            "generation": resident.generation,
            "used_portable_tokens": resident.used_portable_tokens,
        },
        "sections": [
            {
                "content": section.content,
                "media_type": section.media_type,
                "name": section.name,
                "portable_tokens": section.portable_tokens,
                "sensitive": section.sensitive,
            }
            for section in loaded.sections
        ],
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
    approval_id: str | None = None,
    allow_irreversible: bool = False,
    idempotency_key: str | None = None,
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
        approval_id=approval_id,
        allow_irreversible=allow_irreversible,
        idempotency_key=idempotency_key,
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
    approved: bool = False,
    approval_id: str | None = None,
    allow_irreversible: bool = False,
    idempotency_key: str | None = None,
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
    signer = ReferenceSigner(token_bytes(32))
    audit = _audit_sink(selected.project)
    service = CapabilityHubService(
        registry=generation.registry,
        providers=providers,
        references=signer,
        audit=audit,
        idempotency_store=SqliteIdempotencyStore(_state_path(selected.project)),
        provider_supervisor=ProcessProviderSupervisor(),
    )
    context = _local_context(
        granted_permissions,
        allow_irreversible=allow_irreversible,
    )
    budget = _persistent_budget(selected.project)
    load_ref = signer.issue(
        revision=revision,
        scope=context.reference_scope,
        purpose="load",
        ttl_seconds=60,
    )
    loaded = service.load(
        load_ref,
        task_id="local-cli",
        context=context,
        budget=budget,
        section_names=(),
        operation_names=(operation,),
        max_output_tokens=max_output_tokens,
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
        SqliteApprovalStore(_state_path(selected.project)).consume(approval_id, intent)
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
    result = service.execute(
        ExecutionRequest(
            loaded.execution_ref,
            operation,
            arguments,
            "local-cli",
            approval_ref=approval_ref,
            idempotency_key=idempotency_key,
        ),
        context=context,
        budget=budget,
        max_output_tokens=max_output_tokens,
    )
    return {
        "audit_id": result.audit_id,
        "budget": _budget_json(budget.snapshot()),
        "capability_revision": result.capability_revision,
        "operation": result.operation,
        "output": result.output,
        "portable_tokens": result.portable_tokens,
        "provider": result.provider,
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
    record = SqliteApprovalStore(_state_path(selected.project)).request(
        _approval_intent(
            revision,
            operation,
            arguments,
            context=context,
            side_effect=operation_spec.side_effect.value,
        ),
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
    records = SqliteApprovalStore(_state_path(project)).list(status=status, limit=limit)
    return {"approvals": [_approval_json(record) for record in records], "count": len(records)}


def local_approval_decide(
    approval_id: str,
    decision: str,
    *,
    project_root: str | Path | None = None,
) -> dict[str, JsonValue]:
    """Approve or deny one pending request as the local operator."""

    project = _catalog_project(project_root)
    store = SqliteApprovalStore(_state_path(project))
    if decision == "approve":
        record = store.approve(approval_id, decided_by="local-operator")
    elif decision == "deny":
        record = store.deny(approval_id, decided_by="local-operator")
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
                "configured": _secure_audit_key(required=False) is not None,
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
) -> tuple[LocalCatalogMonitor, StagedUpdateManager]:
    selected = _select_local_scope(project_root, monitor)
    generation = selected.snapshot()
    return selected, StagedUpdateManager(
        registry=generation.registry,
        store=SQLiteUpdateStore(_state_path(selected.project)),
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
    allow_irreversible: bool = False,
) -> ServiceContext:
    return ServiceContext(
        "local",
        "operator",
        "cli",
        granted_permissions=frozenset(granted_permissions or ()),
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


def _secure_audit_key(*, required: bool) -> bytes | None:
    raw = os.environ.get(SECURE_AUDIT_KEY_ENV)
    if raw is None:
        if not required:
            return None
        raise CapabilityHubError(
            code="secure_audit_key_missing",
            category=ErrorCategory.INPUT,
            safe_message=f"Set {SECURE_AUDIT_KEY_ENV} to use secure audit controls.",
        )
    key = raw.encode("utf-8")
    if len(key) < 16:
        raise CapabilityHubError(
            code="secure_audit_key_invalid",
            category=ErrorCategory.INPUT,
            safe_message="The secure audit signing key must contain at least 16 UTF-8 bytes.",
        )
    return key


def _audit_sink(project: Path) -> AuditSink:
    key = _secure_audit_key(required=False)
    if key is None:
        return JsonlAuditSink(_audit_path(project))
    return SecureAuditLedger(_secure_audit_root(project) / "current", signing_key=key)


def _audit_events(project: Path, *, limit: int) -> tuple[AuditEvent, ...]:
    key = _secure_audit_key(required=False)
    if key is None:
        return read_jsonl_audit(_audit_path(project), limit=limit)
    _, records = SecureAuditLedger(
        _secure_audit_root(project) / "current", signing_key=key
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


def _state_path(project: Path) -> Path:
    return project / ".capabilityhub" / "state.sqlite3"


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
