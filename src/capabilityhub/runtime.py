"""Small local runtime helpers used by the command-line interface."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import cast

from capabilityhub.manifest import load_manifest
from capabilityhub.models import CapabilityManifest
from capabilityhub.providers.skill import SkillProvider
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
    project = Path(project_root).resolve() if project_root is not None else None
    return typed_serve(project=project)
