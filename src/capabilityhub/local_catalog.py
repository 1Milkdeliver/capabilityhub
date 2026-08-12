"""Read-only discovery for the local CapSift MCP runtime."""

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
from capabilityhub.projections import ProjectionPolicy, ProjectionResolution, resolve_projections
from capabilityhub.provider_config import project_providers
from capabilityhub.providers.base import CapabilityProvider
from capabilityhub.providers.skill import SkillProvider
from capabilityhub.state import global_config_path, lifecycle_states, project_config_path
from capabilityhub.update_store import SQLiteUpdateStore

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class LocalCatalog:
    """Discovered manifests plus explicitly configured local providers."""

    manifests: tuple[CapabilityManifest, ...]
    skill_providers: tuple[SkillProvider, ...]
    configured_providers: tuple[CapabilityProvider, ...] = ()
    inactive_coordinates: frozenset[str] = frozenset()
    controlled_disabled_coordinates: frozenset[str] = frozenset()
    quarantined_coordinates: frozenset[str] = frozenset()
    invalid_count: int = 0
    skipped_count: int = 0
    duplicate_count: int = 0
    conflict_count: int = 0
    projection_resolution: ProjectionResolution | None = None


@dataclass(frozen=True, slots=True)
class _SkillRoot:
    namespace: str
    path: Path


def discover_local_catalog(
    *,
    home: Path | None = None,
    project: Path | None = None,
    projection_policy: ProjectionPolicy | None = None,
) -> LocalCatalog:
    """Discover safe local metadata without executing capability code."""

    home_dir = (home or Path.home()).resolve()
    project_dir = (project or Path.cwd()).resolve()
    providers: list[SkillProvider] = []
    manifests: list[CapabilityManifest] = []
    invalid_count = 0
    skipped_count = 0
    duplicate_count = 0
    conflict_count = 0
    skill_manifests: list[CapabilityManifest] = []
    for root in _skill_roots(home_dir, project_dir):
        provider = SkillProvider(
            [root.path],
            namespace=root.namespace,
            name=f"skill-{root.namespace}",
            skip_invalid=True,
        )
        report = provider.discover_report()
        invalid_count += report.invalid_count
        skipped_count += report.skipped_count
        duplicate_count += report.duplicate_count
        if report.manifests:
            providers.append(provider)
            skill_manifests.extend(report.manifests)

    name_digests: dict[str, str] = {}
    for manifest in skill_manifests:
        raw_digest = manifest.metadata.get("content_digest")
        digest = raw_digest if isinstance(raw_digest, str) else manifest.identity.digest
        normalized_name = manifest.identity.name.casefold()
        known = name_digests.get(normalized_name)
        if known is not None:
            if known == digest:
                duplicate_count += 1
            else:
                conflict_count += 1
            continue
        name_digests[normalized_name] = digest
        manifests.append(manifest)

    mcp_manifests, inactive_coordinates = _configured_mcp_manifests(home_dir)
    manifests.extend(mcp_manifests)
    manifests.append(_capabilityhub_cli_manifest())
    project_manifests, project_invalid = _project_manifests(project_dir)
    invalid_count += project_invalid
    manifests.extend(project_manifests)
    configured_providers, provider_invalid = project_providers(project_manifests, project_dir)
    invalid_count += provider_invalid
    states = lifecycle_states(home=home_dir, project=project_dir)
    controlled_disabled = frozenset(
        coordinate for coordinate, state in states.items() if state == "disabled"
    )
    quarantined = frozenset(
        coordinate for coordinate, state in states.items() if state == "quarantined"
    )
    selected_policy = projection_policy or ProjectionPolicy("isolate")
    projection_resolution = resolve_projections(manifests, selected_policy)
    projection_excluded = projection_resolution.excluded_coordinates
    if projection_excluded:
        manifests = [
            manifest
            for manifest in manifests
            if manifest.identity.coordinate not in projection_excluded
        ]
        inactive_coordinates = inactive_coordinates | projection_excluded
    return LocalCatalog(
        tuple(manifests),
        tuple(providers),
        configured_providers=tuple(configured_providers),
        inactive_coordinates=frozenset(inactive_coordinates) | controlled_disabled | quarantined,
        controlled_disabled_coordinates=controlled_disabled,
        quarantined_coordinates=quarantined,
        invalid_count=invalid_count,
        skipped_count=skipped_count,
        duplicate_count=duplicate_count,
        conflict_count=conflict_count,
        projection_resolution=projection_resolution,
    )


def local_catalog_fingerprint(
    *,
    home: Path | None = None,
    project: Path | None = None,
) -> str:
    """Return a cheap, non-reversible fingerprint of local inventory inputs."""

    home_dir = (home or Path.home()).resolve()
    project_dir = (project or Path.cwd()).resolve()
    digest = hashlib.sha256()
    config = home_dir / ".codex" / "config.toml"
    _fingerprint_path(digest, config, include_content=True)
    _fingerprint_path(digest, global_config_path(home_dir), include_content=True)
    _fingerprint_path(digest, project_config_path(project_dir), include_content=True)
    for root in _skill_roots(home_dir, project_dir):
        digest.update(root.namespace.encode())
        digest.update(b"\0")
        digest.update(str(root.path).encode("utf-8", errors="surrogatepass"))
        for path in sorted(root.path.rglob("SKILL.md"), key=lambda item: item.as_posix()):
            _fingerprint_path(digest, path)
    manifest_root = project_dir / ".capabilityhub" / "manifests"
    if manifest_root.is_dir():
        for path in _manifest_paths(manifest_root):
            _fingerprint_path(digest, path)
    state_path = project_dir / ".capabilityhub" / "state.sqlite3"
    if state_path.is_file():
        for coordinate, revision in SQLiteUpdateStore(state_path).active_pointers().items():
            digest.update(coordinate.encode("utf-8"))
            digest.update(b"\0")
            digest.update(revision.encode("utf-8"))
    return digest.hexdigest()


def _skill_roots(home: Path, project: Path) -> tuple[_SkillRoot, ...]:
    candidates = [
        _SkillRoot("codex-project", project / ".codex" / "skills"),
        _SkillRoot("agents-project", project / ".agents" / "skills"),
        _SkillRoot("codex-user", home / ".codex" / "skills"),
        _SkillRoot("agents-user", home / ".agents" / "skills"),
    ]
    candidates.extend(_enabled_plugin_skill_roots(home))
    roots: list[_SkillRoot] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.path.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir() or resolved in seen:
            continue
        seen.add(resolved)
        roots.append(_SkillRoot(candidate.namespace, resolved))
    return tuple(roots)


def _enabled_plugin_skill_roots(home: Path) -> tuple[_SkillRoot, ...]:
    payload = _codex_config(home)
    configured = payload.get("plugins")
    if not isinstance(configured, dict):
        return ()
    cache = home / ".codex" / "plugins" / "cache"
    roots: list[_SkillRoot] = []
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
                key=lambda item: _version_key(item.name),
                reverse=True,
            )
        except OSError:
            continue
        for version in versions:
            skills = version / "skills"
            if skills.is_dir():
                namespace = _identifier(f"plugin-{marketplace}-{plugin}")
                roots.append(_SkillRoot(namespace, skills))
                break
    return tuple(roots)


def _configured_mcp_manifests(
    home: Path,
) -> tuple[tuple[CapabilityManifest, ...], frozenset[str]]:
    payload = _codex_config(home)
    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict):
        return (), frozenset()
    manifests: list[CapabilityManifest] = []
    inactive: set[str] = set()
    for raw_name, config in sorted(servers.items()):
        if not isinstance(raw_name, str) or not isinstance(config, dict):
            continue
        name = _identifier(raw_name)
        transport = "http" if isinstance(config.get("url"), str) else "stdio"
        digest = hashlib.sha256(f"{raw_name}\0{transport}".encode()).hexdigest()
        manifest = CapabilityManifest(
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
        manifests.append(manifest)
        if config.get("enabled") is False:
            inactive.add(manifest.identity.coordinate)
    return tuple(manifests), frozenset(inactive)


def _capabilityhub_cli_manifest() -> CapabilityManifest:
    """Describe the CLI shipped with the running CapSift distribution."""

    version = "source"
    for distribution in ("capsift", "capabilityhub"):
        try:
            version = importlib.metadata.version(distribution)
            break
        except importlib.metadata.PackageNotFoundError:
            continue
    digest = hashlib.sha256(f"capabilityhub-cli\0{version}".encode()).hexdigest()
    return CapabilityManifest(
        identity=CapabilityIdentity(
            "capabilityhub",
            "cli",
            version,
            f"sha256:{digest}",
        ),
        kind=CapabilityKind.CLI,
        summary="Installed CapSift local command-line interface: capsift",
        provider="capabilityhub-runtime",
        operations=(
            OperationSpec("validate", OperationType.EXPAND),
            OperationSpec("export-manifest", OperationType.EXPAND),
            OperationSpec("import-openapi", OperationType.EXPAND),
            OperationSpec("migrate-manifest", OperationType.EXPAND),
            OperationSpec("compatibility", OperationType.EXPAND),
            OperationSpec("activation-lock", OperationType.EXPAND),
            OperationSpec("discover-skills", OperationType.EXPAND),
            OperationSpec("inventory", OperationType.EXPAND),
            OperationSpec("search", OperationType.EXPAND),
            OperationSpec("health", OperationType.EXPAND),
            OperationSpec("connections", OperationType.EXPAND),
            OperationSpec("loaded", OperationType.EXPAND),
            OperationSpec("providers", OperationType.EXPAND),
            OperationSpec("routing", OperationType.EXPAND),
            OperationSpec("language", OperationType.EXPAND),
            OperationSpec("lifecycle", OperationType.EXPAND),
            OperationSpec("updates", OperationType.EXPAND),
            OperationSpec("audit", OperationType.EXPAND),
            OperationSpec("secure-audit", OperationType.EXPAND),
            OperationSpec("load", OperationType.EXPAND),
            OperationSpec("execute", OperationType.EXPAND),
            OperationSpec("approvals", OperationType.EXPAND),
            OperationSpec("context", OperationType.EXPAND),
            OperationSpec("reasoning", OperationType.EXPAND),
            OperationSpec("budget-report", OperationType.EXPAND),
            OperationSpec("benchmark", OperationType.EXPAND),
            OperationSpec("dashboard", OperationType.EXPAND),
            OperationSpec("http-serve", OperationType.EXPAND),
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


def _project_manifests(project: Path) -> tuple[tuple[CapabilityManifest, ...], int]:
    root = project / ".capabilityhub" / "manifests"
    if not root.is_dir():
        return (), 0
    manifests: list[CapabilityManifest] = []
    invalid_count = 0
    for path in _manifest_paths(root):
        try:
            manifests.append(load_manifest(path))
        except (CapabilityHubError, OSError, ValueError):
            invalid_count += 1
            continue
    return tuple(manifests), invalid_count


def _manifest_paths(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix.casefold() in {".json", ".yaml", ".yml"}
            ),
            key=lambda item: item.as_posix(),
        )
    )


def _fingerprint_path(
    digest: hashlib._Hash,
    path: Path,
    *,
    include_content: bool = False,
) -> None:
    digest.update(str(path).encode("utf-8", errors="surrogatepass"))
    digest.update(b"\0")
    try:
        stat = path.stat()
    except OSError:
        digest.update(b"missing\0")
        return
    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    digest.update(b"\0")
    if include_content:
        try:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            digest.update(b"unreadable")


def _identifier(value: str) -> str:
    normalized = _SAFE_NAME.sub("-", value).strip("-.")[:128]
    return normalized or hashlib.sha256(value.encode()).hexdigest()[:16]


def _version_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )
