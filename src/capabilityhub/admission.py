"""Fail-closed, inert validation before registry mutation."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePath
from urllib.parse import urlsplit

from capabilityhub.compatibility import V1ALPHA1_FEATURES
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import CapabilityKind, CapabilityManifest, OperationType
from capabilityhub.registry import CapabilityRegistry

_PERMISSION = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$")
_ROOTS = frozenset({"content", "filesystem", "network", "process", "secret", "system"})
_DRIVERS = {
    CapabilityKind.API: "http-api",
    CapabilityKind.CLI: "cli-process",
    CapabilityKind.MCP: "mcp-stdio",
    CapabilityKind.RAG: "local-rag",
    CapabilityKind.SKILL: "skill",
}


def validate_for_admission(
    manifests: Iterable[CapabilityManifest], *, project: str | Path = "."
) -> tuple[CapabilityManifest, ...]:
    """Validate a complete transaction without executing drivers or mutating a registry."""

    selected = tuple(manifests)
    root = Path(project).resolve()
    for manifest in selected:
        validate_manifest_semantics(manifest, project=root)
    candidate = CapabilityRegistry()
    candidate.register_many(selected)
    candidate.validate_staged()
    return selected


def install_validated(
    current: CapabilityRegistry,
    manifests: Iterable[CapabilityManifest],
    *,
    project: str | Path = ".",
    activate: Iterable[str] = (),
) -> CapabilityRegistry:
    """Return a replacement registry only after every install check succeeds."""

    additions = validate_for_admission(manifests, project=project)
    candidate = CapabilityRegistry()
    candidate.register_many((*current.revisions.values(), *additions))
    for coordinate, revision in current.activations.items():
        candidate.activate(coordinate, revision)
    for revision in tuple(activate):
        manifest = candidate.revision(revision)
        candidate.activate(manifest.identity.coordinate, revision)
    candidate.validate_active()
    return candidate


def validate_manifest_semantics(
    manifest: CapabilityManifest, *, project: str | Path = "."
) -> None:
    """Validate one inert manifest without requiring its dependency graph yet."""

    _project = Path(project).resolve()
    if any(
        not _PERMISSION.fullmatch(item) or item.split(".", 1)[0] not in _ROOTS
        for item in manifest.permissions
    ):
        raise _invalid("unsupported_permission")
    extensions = _mapping(manifest.metadata.get("extensions", {}))
    required = extensions.get("requiredFeatures", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise _invalid("invalid_required_features")
    if set(required) - set(V1ALPHA1_FEATURES):
        raise _invalid("unsupported_required_feature")
    raw_driver = manifest.metadata.get("driver")
    if raw_driver is None and all(
        operation.operation_type is OperationType.EXPAND for operation in manifest.operations
    ):
        # Metadata-only inspection/expansion capabilities have no data-plane
        # driver to initialize. Any executable/retrieval surface still requires
        # a kind-specific driver below.
        return
    if raw_driver is None and manifest.provider == "static":
        # Static manifests are inert, side-effect-free fixtures. They have no
        # external driver configuration to validate or initialize.
        return
    if manifest.kind is CapabilityKind.SKILL and raw_driver is None:
        if manifest.provider != "skill":
            raise _invalid("invalid_skill_driver")
        return
    driver = _mapping(raw_driver)
    expected_driver = _DRIVERS[manifest.kind]
    if _text(driver, "name") != expected_driver or manifest.provider != expected_driver:
        raise _invalid("driver_kind_mismatch")
    config = _mapping(driver.get("config"))
    if manifest.kind is CapabilityKind.CLI:
        _absolute(config, "executable")
        _operation_map(config, manifest, "invalid_cli_driver")
    elif manifest.kind is CapabilityKind.MCP:
        _absolute(config, "command")
        tools = _mapping(config.get("tools"))
        if set(tools) != {item.name for item in manifest.operations} or not all(
            isinstance(value, str) and value for value in tools.values()
        ):
            raise _invalid("invalid_mcp_driver")
    elif manifest.kind is CapabilityKind.API:
        parsed = urlsplit(_text(config, "baseUrl"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise _invalid("invalid_api_driver")
        for item in _operation_map(config, manifest, "invalid_api_driver").values():
            path = item.get("path")
            if item.get("method") not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                raise _invalid("invalid_api_driver")
            if not isinstance(path, str) or not path.startswith("/"):
                raise _invalid("invalid_api_driver")
    elif manifest.kind is CapabilityKind.RAG:
        path = PurePath(_text(config, "root"))
        if path.is_absolute() or ".." in path.parts:
            raise _invalid("invalid_rag_driver")
    elif config:
        raise _invalid("invalid_skill_driver")


def _operation_map(
    config: Mapping[str, object], manifest: CapabilityManifest, code: str
) -> Mapping[str, Mapping[str, object]]:
    operations = _mapping(config.get("operations"))
    if set(operations) != {item.name for item in manifest.operations}:
        raise _invalid(code)
    return {name: _mapping(value) for name, value in operations.items()}


def _absolute(config: Mapping[str, object], field: str) -> None:
    if not Path(_text(config, field)).is_absolute():
        raise _invalid("driver_path_not_absolute")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _invalid("invalid_driver_config")
    return value


def _text(value: Mapping[str, object], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise _invalid("invalid_driver_config")
    return raw


def _invalid(code: str) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.INPUT,
        safe_message="Capability admission validation failed.",
    )
