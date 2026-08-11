"""Explicit, idempotent migration of legacy JSON manifest aliases to v1alpha1."""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import cast

from capabilityhub.compatibility import V1ALPHA1
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import JsonValue

_LEGACY_VERSION = "capabilityhub.io/v1alpha0"
_ROOT_ALIASES = {"api_version": "apiVersion", "manifestKind": "kind"}
_METADATA_ALIASES = {
    "namespaceName": "namespace",
    "packageName": "name",
    "contentDigest": "digest",
}
_SPEC_ALIASES = {
    "capabilityType": "type",
    "description": "summary",
    "providerName": "provider",
    "trust_tier": "trustTier",
}
_OPERATION_ALIASES = {
    "operation_type": "operationType",
    "input_schema": "inputSchema",
    "output_schema": "outputSchema",
    "side_effect": "sideEffect",
    "requires_approval": "requiresApproval",
}
_SECTION_ALIASES = {"media_type": "mediaType", "portable_tokens": "portableTokens"}
_DEPENDENCY_ALIASES = {"version_constraint": "versionConstraint"}
_COMPATIBILITY_ALIASES = {
    "required_features": "requiredFeatures",
    "optional_features": "optionalFeatures",
}
_ROOT_FIELDS = {"apiVersion", "kind", "metadata", "spec"}
_METADATA_FIELDS = {"namespace", "name", "version", "digest", "tags"}
_SPEC_FIELDS = {
    "type",
    "summary",
    "provider",
    "driver",
    "operations",
    "sections",
    "permissions",
    "dependencies",
    "conflicts",
    "tags",
    "trust",
    "trustTier",
    "source",
    "compatibility",
}


@dataclass(frozen=True, slots=True)
class MigrationReport:
    source_version: str
    target_version: str
    changed: bool
    changes: tuple[str, ...]
    preserved_extensions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MigrationResult:
    document: dict[str, JsonValue]
    report: MigrationReport


def migrate_manifest(document: Mapping[str, object]) -> MigrationResult:
    """Normalize supported legacy aliases without interpreting provider material."""

    root = _json_copy(document)
    changes: list[str] = []
    _aliases(root, _ROOT_ALIASES, "", changes)
    raw_version = root.get("apiVersion")
    if not isinstance(raw_version, str):
        raise _migration_error(
            "migration_missing_version", "The manifest has no supported API version."
        )
    source_version = raw_version
    if source_version == _LEGACY_VERSION:
        root["apiVersion"] = V1ALPHA1
        changes.append(f"/apiVersion:{_LEGACY_VERSION}->{V1ALPHA1}")
    elif source_version != V1ALPHA1:
        raise _migration_error(
            "migration_unsupported_version",
            "The manifest API version cannot be migrated by this release.",
        )
    if root.get("kind") == "capability":
        root["kind"] = "Capability"
        changes.append("/kind:capability->Capability")

    metadata = _child_mapping(root, "metadata", "/metadata")
    spec = _child_mapping(root, "spec", "/spec")
    _aliases(metadata, _METADATA_ALIASES, "/metadata", changes)
    _aliases(spec, _SPEC_ALIASES, "/spec", changes)
    _migrate_sequence(spec.get("operations"), "/spec/operations", _OPERATION_ALIASES, changes)
    _migrate_sections(spec.get("sections"), changes)
    _migrate_sequence(spec.get("dependencies"), "/spec/dependencies", _DEPENDENCY_ALIASES, changes)
    compatibility = spec.get("compatibility")
    if compatibility is not None:
        _aliases(
            _as_mapping(compatibility, "/spec/compatibility"),
            _COMPATIBILITY_ALIASES,
            "/spec/compatibility",
            changes,
        )

    extensions = _extension_paths(root, metadata, spec)
    return MigrationResult(
        document=root,
        report=MigrationReport(
            source_version=source_version,
            target_version=V1ALPHA1,
            changed=bool(changes),
            changes=tuple(changes),
            preserved_extensions=extensions,
        ),
    )


def _aliases(
    target: MutableMapping[str, JsonValue],
    aliases: Mapping[str, str],
    path: str,
    changes: list[str],
) -> None:
    for legacy, current in aliases.items():
        if legacy not in target:
            continue
        value = target[legacy]
        if current in target and target[current] != value:
            raise _migration_error(
                "migration_alias_conflict",
                "A legacy alias conflicts with its current field.",
                path=path or "/",
                legacy=legacy,
                current=current,
            )
        target[current] = value
        del target[legacy]
        changes.append(f"{path}/{legacy}->{current}")


def _migrate_sequence(
    value: JsonValue | None,
    path: str,
    aliases: Mapping[str, str],
    changes: list[str],
) -> None:
    if value is None:
        return
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _migration_error("migration_invalid_shape", f"{path} must be an array.")
    for index, item in enumerate(value):
        _aliases(_as_mapping(item, f"{path}/{index}"), aliases, f"{path}/{index}", changes)


def _migrate_sections(value: JsonValue | None, changes: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, Mapping):
        for name, section in value.items():
            path = f"/spec/sections/{name}"
            _aliases(_as_mapping(section, path), _SECTION_ALIASES, path, changes)
        return
    _migrate_sequence(value, "/spec/sections", _SECTION_ALIASES, changes)


def _extension_paths(
    root: Mapping[str, JsonValue],
    metadata: Mapping[str, JsonValue],
    spec: Mapping[str, JsonValue],
) -> tuple[str, ...]:
    paths = [f"/{key}" for key in root if key not in _ROOT_FIELDS]
    paths.extend(f"/metadata/{key}" for key in metadata if key not in _METADATA_FIELDS)
    paths.extend(f"/spec/{key}" for key in spec if key not in _SPEC_FIELDS)
    return tuple(sorted(paths))


def _json_copy(document: Mapping[str, object]) -> dict[str, JsonValue]:
    try:
        encoded = json.dumps(
            dict(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise _migration_error(
            "migration_invalid_json", "The migration input must contain JSON data only."
        ) from error
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise _migration_error("migration_invalid_shape", "The manifest must be an object.")
    return cast(dict[str, JsonValue], decoded)


def _child_mapping(
    parent: MutableMapping[str, JsonValue], key: str, path: str
) -> MutableMapping[str, JsonValue]:
    return _as_mapping(parent.get(key), path)


def _as_mapping(value: object, path: str) -> MutableMapping[str, JsonValue]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _migration_error("migration_invalid_shape", f"{path} must be an object.")
    return cast(MutableMapping[str, JsonValue], value)


def _migration_error(code: str, message: str, **details: object) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.INPUT,
        safe_message=message,
        details=details,
    )
