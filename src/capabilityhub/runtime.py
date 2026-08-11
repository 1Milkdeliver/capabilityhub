"""Small local runtime helpers used by the command-line interface."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from importlib import import_module, metadata, util
from pathlib import Path
from typing import cast

from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.manifest import load_manifest
from capabilityhub.models import CapabilityKind, CapabilityManifest, JsonValue
from capabilityhub.providers.skill import SkillProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.search import LexicalCapabilitySearch
from capabilityhub.webui import DashboardServer, StatusSnapshot


def validate(paths: list[str | Path]) -> int:
    """Validate manifest files and return their count without executing providers."""
    for path in paths:
        load_manifest(path)
    return len(paths)


def discover_skills(directories: list[str | Path]) -> tuple[CapabilityManifest, ...]:
    """Discover inert Skill manifests from approved local directories."""
    return SkillProvider(directories).discover()


def dashboard(snapshot_provider: Callable[[], StatusSnapshot], *, port: int = 0) -> DashboardServer:
    """Create (but do not implicitly retain) a localhost-only dashboard server."""
    server = DashboardServer(snapshot_provider, host="127.0.0.1", port=port)
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
    overall = (
        "ok"
        if project_ok and assets_ok and config_status != "invalid"
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


def local_dashboard(
    project_root: str | Path | None = None,
    *,
    port: int = 0,
    monitor: LocalCatalogMonitor | None = None,
) -> DashboardServer:
    """Start a live read-only dashboard backed by one local catalog monitor."""

    selected = _select_local_scope(project_root, monitor)

    def snapshot() -> StatusSnapshot:
        inventory = local_inventory(monitor=selected)
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
            "providers": providers,
        }

    return dashboard(snapshot, port=port)


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
