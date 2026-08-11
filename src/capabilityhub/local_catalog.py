"""Read-only discovery for the local CapabilityHub MCP runtime."""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from capabilityhub.errors import CapabilityHubError
from capabilityhub.manifest import load_manifest
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
)
from capabilityhub.providers.skill import SkillProvider

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class LocalCatalog:
    """Discovered manifests and their inert Skill providers."""

    manifests: tuple[CapabilityManifest, ...]
    skill_providers: tuple[SkillProvider, ...]


def discover_local_catalog(
    *,
    home: Path | None = None,
    project: Path | None = None,
) -> LocalCatalog:
    """Discover safe local metadata without executing capability code."""

    home_dir = (home or Path.home()).resolve()
    project_dir = (project or Path.cwd()).resolve()
    providers: list[SkillProvider] = []
    manifests: list[CapabilityManifest] = []
    for index, root in enumerate(_skill_roots(home_dir, project_dir), start=1):
        provider = SkillProvider(
            [root],
            namespace=f"local-skills-{index}",
            name=f"local-skill-{index}",
            skip_invalid=True,
        )
        discovered = provider.discover()
        if discovered:
            providers.append(provider)
            manifests.extend(discovered)

    manifests.extend(_configured_mcp_manifests(home_dir))
    manifests.append(_capabilityhub_cli_manifest())
    manifests.extend(_project_manifests(project_dir))
    return LocalCatalog(tuple(manifests), tuple(providers))


def _skill_roots(home: Path, project: Path) -> tuple[Path, ...]:
    candidates = [
        home / ".codex" / "skills",
        home / ".agents" / "skills",
        project / ".codex" / "skills",
        project / ".agents" / "skills",
    ]
    candidates.extend(_enabled_plugin_skill_roots(home))
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir() or resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return tuple(roots)


def _enabled_plugin_skill_roots(home: Path) -> tuple[Path, ...]:
    payload = _codex_config(home)
    configured = payload.get("plugins")
    if not isinstance(configured, dict):
        return ()
    cache = home / ".codex" / "plugins" / "cache"
    roots: list[Path] = []
    for selector, settings in configured.items():
        if (
            not isinstance(selector, str)
            or "@" not in selector
            or not isinstance(settings, dict)
            or settings.get("enabled") is not True
        ):
            continue
        plugin, marketplace = selector.rsplit("@", 1)
        version_root = cache / marketplace / plugin
        try:
            versions = sorted(
                (item for item in version_root.iterdir() if item.is_dir()),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            continue
        for version in versions:
            skills = version / "skills"
            if skills.is_dir():
                roots.append(skills)
                break
    return tuple(roots)


def _configured_mcp_manifests(home: Path) -> tuple[CapabilityManifest, ...]:
    payload = _codex_config(home)
    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict):
        return ()
    manifests: list[CapabilityManifest] = []
    for raw_name, config in sorted(servers.items()):
        if not isinstance(raw_name, str) or not isinstance(config, dict):
            continue
        name = _identifier(raw_name)
        transport = "http" if isinstance(config.get("url"), str) else "stdio"
        digest = hashlib.sha256(f"{raw_name}\0{transport}".encode()).hexdigest()
        manifests.append(
            CapabilityManifest(
                identity=CapabilityIdentity(
                    "codex-mcp",
                    name,
                    "configured",
                    f"sha256:{digest}",
                ),
                kind=CapabilityKind.MCP,
                summary=f"Configured Codex MCP server ({transport}): {raw_name}",
                provider="codex-config",
                operations=(OperationSpec("inspect", OperationType.EXPAND),),
                source=f"codex://mcp/{name}",
                trust_tier="configured",
            )
        )
    return tuple(manifests)


def _capabilityhub_cli_manifest() -> CapabilityManifest:
    """Describe the CLI shipped with the running CapabilityHub distribution."""

    try:
        version = importlib.metadata.version("capabilityhub")
    except importlib.metadata.PackageNotFoundError:
        version = "source"
    digest = hashlib.sha256(f"capabilityhub-cli\0{version}".encode()).hexdigest()
    return CapabilityManifest(
        identity=CapabilityIdentity(
            "capabilityhub",
            "cli",
            version,
            f"sha256:{digest}",
        ),
        kind=CapabilityKind.CLI,
        summary="Installed CapabilityHub local command-line interface: capabilityhub",
        provider="capabilityhub-runtime",
        operations=(
            OperationSpec("validate", OperationType.EXPAND),
            OperationSpec("discover-skills", OperationType.EXPAND),
            OperationSpec("dashboard", OperationType.EXPAND),
            OperationSpec("mcp-serve", OperationType.EXPAND),
        ),
        source="python://capabilityhub.cli",
        trust_tier="built-in",
    )


def _codex_config(home: Path) -> dict[str, object]:
    path = home / ".codex" / "config.toml"
    try:
        with path.open("rb") as stream:
            payload = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return payload


def _project_manifests(project: Path) -> tuple[CapabilityManifest, ...]:
    root = project / ".capabilityhub" / "manifests"
    if not root.is_dir():
        return ()
    manifests: list[CapabilityManifest] = []
    for path in sorted(root.rglob("*.json"), key=lambda item: item.as_posix()):
        try:
            manifests.append(load_manifest(path))
        except (CapabilityHubError, OSError, ValueError):
            continue
    return tuple(manifests)


def _identifier(value: str) -> str:
    normalized = _SAFE_NAME.sub("-", value).strip("-.")[:128]
    return normalized or hashlib.sha256(value.encode()).hexdigest()[:16]
