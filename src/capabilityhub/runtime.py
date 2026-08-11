"""Small local runtime helpers used by the command-line interface."""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from importlib import import_module, metadata, util
from pathlib import Path
from secrets import token_bytes
from typing import cast

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger, BudgetSnapshot
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.manifest import load_manifest
from capabilityhub.models import (
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    JsonValue,
)
from capabilityhub.providers.skill import SkillProvider
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.search import LexicalCapabilitySearch
from capabilityhub.service import CapabilityHubService, ServiceContext
from capabilityhub.state import (
    PreferenceScope,
    resolved_preferences,
    set_lifecycle,
    set_locale,
)
from capabilityhub.webui import (
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
}


def validate(paths: list[str | Path]) -> int:
    """Validate manifest files and return their count without executing providers."""
    for path in paths:
        load_manifest(path)
    return len(paths)


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
) -> DashboardServer:
    """Create (but do not implicitly retain) a localhost-only dashboard server."""
    server = DashboardServer(
        snapshot_provider,
        host="127.0.0.1",
        port=port,
        search_provider=search_provider,
        lifecycle_provider=lifecycle_provider,
        language_provider=language_provider,
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
    audit = MemoryAuditSink()
    service = CapabilityHubService(
        registry=generation.registry,
        providers=generation.providers,
        references=signer,
        audit=audit,
    )
    context = _local_context(granted_permissions)
    budget = BudgetLedger("local-cli", DEFAULT_LOCAL_BUDGETS)
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

    selected = _select_local_scope(project_root, monitor)
    generation = selected.snapshot()
    manifest = generation.registry.revision(revision)
    provider = StaticProvider(
        (StaticFixture(manifest, {operation: fixture_output}),),
        name=manifest.provider,
    )
    signer = ReferenceSigner(token_bytes(32))
    audit = MemoryAuditSink()
    service = CapabilityHubService(
        registry=generation.registry,
        providers=(provider,),
        references=signer,
        audit=audit,
    )
    context = _local_context(
        granted_permissions,
        allow_irreversible=allow_irreversible,
    )
    budget = BudgetLedger("local-cli", DEFAULT_LOCAL_BUDGETS)
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
    approval_ref = (
        service.issue_approval(
            revision=revision,
            operation=operation,
            arguments=arguments,
            task_id="local-cli",
            context=context,
            ttl_seconds=60,
        )
        if approved
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


def local_budget_report(
    limits: dict[str, int] | None = None,
) -> dict[str, JsonValue]:
    """Return a fresh local CLI budget envelope without scanning the catalog."""

    return _budget_json(BudgetLedger("local-cli", limits or DEFAULT_LOCAL_BUDGETS).snapshot())


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
        raw_counts = inventory.get("active_by_kind")
        counts = raw_counts if isinstance(raw_counts, dict) else {}
        providers = [
            {
                "name": kind.value.upper(),
                "status": f"{counts.get(kind.value, 0)} active",
            }
            for kind in CapabilityKind
        ]
        return {
            "active_capabilities": [],
            "health": local_health(selected.project),
            "inventory": inventory,
            "connections": local_connections(monitor=selected),
            "loaded_capabilities": [],
            "lifecycle": local_lifecycle(monitor=selected),
            "preferences": {"locale": preferences.get("locale", "auto")},
            "providers": providers,
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

    return dashboard(
        snapshot,
        port=port,
        search_provider=search,
        lifecycle_provider=lifecycle,
        language_provider=language,
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
