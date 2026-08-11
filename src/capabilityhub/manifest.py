"""Parsing and validation for the CapabilityHub v1alpha1 JSON manifest."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ConflictSpec,
    DependencySpec,
    OperationSpec,
    OperationType,
    SectionDescriptor,
    SideEffect,
)

API_VERSION = "capabilityhub.io/v1alpha1"
MAX_SUMMARY_CHARS = 480
MAX_NAME_CHARS = 63
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62})$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EnumT = TypeVar("_EnumT", bound=StrEnum)


def parse_manifest_json(source: str | bytes | bytearray) -> CapabilityManifest:
    """Parse one JSON manifest without interpreting or executing provider material."""

    try:
        document = json.loads(source, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise _invalid("invalid_json", "Manifest must be valid JSON.") from error
    return parse_manifest(document)


def load_manifest(path: str | Path) -> CapabilityManifest:
    """Load a JSON manifest from disk; YAML is intentionally outside the 0.1 core."""

    try:
        return parse_manifest_json(Path(path).read_bytes())
    except OSError as error:
        raise CapabilityHubError(
            code="manifest_unreadable",
            category=ErrorCategory.INPUT,
            safe_message="Manifest could not be read.",
            details={"path": str(path)},
        ) from error


def parse_manifest(document: object) -> CapabilityManifest:
    """Validate a decoded v1alpha1 manifest and return an immutable domain model."""

    root = _mapping(document, "manifest")
    if root.get("apiVersion") != API_VERSION:
        raise _invalid("unsupported_api_version", f"apiVersion must be {API_VERSION}.")
    if root.get("kind") != "Capability":
        raise _invalid("invalid_manifest_kind", "kind must be Capability.")

    raw_metadata = _mapping(root.get("metadata"), "metadata")
    identity = _parse_identity(raw_metadata)
    spec = _mapping(root.get("spec"), "spec")
    capability_kind = _enum(CapabilityKind, spec.get("type"), "spec.type")
    summary = _summary(spec.get("summary"))
    provider = _provider(spec)
    operations = _operations(spec.get("operations"), capability_kind)
    sections = _sections(spec.get("sections", ()))
    permissions = _permissions(spec.get("permissions", ()))
    dependencies = _dependencies(spec.get("dependencies", ()))
    conflicts = _conflicts(spec.get("conflicts", ()))
    tags = _tags(spec.get("tags", raw_metadata.get("tags", ())))

    # Preserve extensions as inert data. They are never interpreted by this parser.
    metadata: dict[str, Any] = dict(raw_metadata)
    metadata["root_extensions"] = {
        key: value
        for key, value in root.items()
        if key not in {"apiVersion", "kind", "metadata", "spec"}
    }
    driver = spec.get("driver")
    if isinstance(driver, Mapping):
        # Keep driver material inert during parsing. A trusted runtime may later
        # interpret the bounded configuration for an explicitly named adapter.
        metadata["driver"] = dict(driver)
    metadata["extensions"] = {
        key: value
        for key, value in spec.items()
        if key
        not in {
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
            "source",
        }
    }
    trust = _mapping_or_empty(spec.get("trust"), "spec.trust")
    source = _string(
        spec.get("source", trust.get("source", "local")), "spec.source", max_length=2048
    )
    trust_tier = _string(
        spec.get("trustTier", trust.get("tier", "unverified")), "spec.trustTier", max_length=128
    )
    return CapabilityManifest(
        identity=identity,
        kind=capability_kind,
        summary=summary,
        provider=provider,
        operations=operations,
        sections=sections,
        permissions=permissions,
        dependencies=dependencies,
        conflicts=conflicts,
        tags=tags,
        trust_tier=trust_tier,
        source=source,
        metadata=metadata,
    )


def _parse_identity(metadata: Mapping[str, object]) -> CapabilityIdentity:
    namespace = _name(metadata.get("namespace"), "metadata.namespace")
    name = _name(metadata.get("name"), "metadata.name")
    version = _string(metadata.get("version"), "metadata.version", max_length=128)
    if any(character.isspace() for character in version):
        raise _invalid("invalid_version", "metadata.version must not contain whitespace.")
    digest = _string(metadata.get("digest"), "metadata.digest", max_length=80)
    if not _DIGEST_RE.fullmatch(digest):
        raise _invalid("invalid_digest", "metadata.digest must be a lowercase sha256 digest.")
    return CapabilityIdentity(namespace=namespace, name=name, version=version, digest=digest)


def _provider(spec: Mapping[str, object]) -> str:
    direct = spec.get("provider")
    if direct is not None:
        return _string(direct, "spec.provider", max_length=128)
    driver = _mapping(spec.get("driver"), "spec.driver")
    return _string(driver.get("name"), "spec.driver.name", max_length=128)


def _operations(value: object, capability_kind: CapabilityKind) -> tuple[OperationSpec, ...]:
    items = _sequence(value, "spec.operations")
    if not items:
        raise _invalid("missing_operations", "spec.operations must contain at least one operation.")
    operations: list[OperationSpec] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        raw = _mapping(item, f"spec.operations[{index}]")
        name = _name(raw.get("name"), f"spec.operations[{index}].name")
        _unique(name, names, "duplicate_operation", "Operation names must be unique.")
        raw_type = raw.get("operationType", raw.get("operation_type"))
        operation_type = _operation_type(raw_type, capability_kind, name)
        input_schema = _schema(raw, "inputSchema", "input_schema", "inputSchemaRef")
        output_schema = _schema(raw, "outputSchema", "output_schema", "outputSchemaRef")
        _validate_json_schema(input_schema, f"spec.operations[{index}].inputSchema")
        _validate_json_schema(output_schema, f"spec.operations[{index}].outputSchema")
        side_effect = _enum(
            SideEffect, raw.get("sideEffect", raw.get("side_effect", "none")), "sideEffect"
        )
        approval = raw.get("requiresApproval", raw.get("requires_approval", False))
        if not isinstance(approval, bool):
            raise _invalid("invalid_approval", "requiresApproval must be a boolean.")
        operations.append(
            OperationSpec(
                name=name,
                operation_type=operation_type,
                input_schema=input_schema,
                output_schema=output_schema,
                side_effect=side_effect,
                requires_approval=approval,
            )
        )
    return tuple(operations)


def _validate_json_schema(schema: Mapping[str, Any], field: str) -> None:
    if not schema or set(schema) == {"$ref"}:
        return
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise _invalid("invalid_json_schema", f"{field} must be valid JSON Schema.") from error


def _operation_type(value: object, kind: CapabilityKind, name: str) -> OperationType:
    if value is not None:
        return _enum(OperationType, value, "operationType")
    if name == "expand":
        return OperationType.EXPAND
    if kind is CapabilityKind.RAG:
        return OperationType.RETRIEVE
    return OperationType.EXECUTE


def _schema(raw: Mapping[str, object], *keys: str) -> Mapping[str, Any]:
    for key in keys:
        if key in raw:
            value = raw[key]
            if key.endswith("Ref"):
                return {"$ref": _string(value, key, max_length=2048)}
            return dict(_mapping(value, key))
    return {}


def _sections(value: object) -> tuple[SectionDescriptor, ...]:
    items: list[tuple[str, Mapping[str, object]]]
    if isinstance(value, Mapping):
        items = [
            (str(name), _mapping(section, f"spec.sections.{name}"))
            for name, section in value.items()
        ]
    else:
        sequence = _sequence(value, "spec.sections")
        items = []
        for index, section in enumerate(sequence):
            raw = _mapping(section, f"spec.sections[{index}]")
            items.append(
                (
                    _string(
                        raw.get("name"), f"spec.sections[{index}].name", max_length=MAX_NAME_CHARS
                    ),
                    raw,
                )
            )

    result: list[SectionDescriptor] = []
    names: set[str] = set()
    for name, raw in items:
        section_name = _name(name, "section name")
        _unique(section_name, names, "duplicate_section", "Section names must be unique.")
        content = raw.get("content", raw.get("ref"))
        if content is None:
            raise _invalid("missing_section_content", "Each section must declare content or ref.")
        tokens = raw.get("portableTokens", raw.get("portable_tokens", raw.get("tokens", 0)))
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            raise _invalid(
                "invalid_section_tokens", "Section token count must be a non-negative integer."
            )
        sensitive = raw.get("sensitive", False)
        if not isinstance(sensitive, bool):
            raise _invalid("invalid_section_sensitivity", "Section sensitive must be a boolean.")
        result.append(
            SectionDescriptor(
                name=section_name,
                media_type=_string(
                    raw.get("mediaType", raw.get("media_type", "text/plain")),
                    "mediaType",
                    max_length=128,
                ),
                content=_string(content, "section content", max_length=16_384),
                portable_tokens=tokens,
                sensitive=sensitive,
            )
        )
    return tuple(result)


def _permissions(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        values = list(value.keys())
    else:
        values = list(_sequence(value, "spec.permissions"))
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        permission = _name(item, "permission")
        _unique(permission, seen, "duplicate_permission", "Permissions must be unique.")
        result.append(permission)
    return tuple(result)


def _dependencies(value: object) -> tuple[DependencySpec, ...]:
    result: list[DependencySpec] = []
    seen: set[str] = set()
    for index, item in enumerate(_sequence(value, "spec.dependencies")):
        raw = _mapping(item, f"spec.dependencies[{index}]")
        coordinate = _coordinate(raw.get("coordinate"), f"spec.dependencies[{index}].coordinate")
        _unique(
            coordinate, seen, "duplicate_dependency", "Dependencies must be unique by coordinate."
        )
        version = _string(
            raw.get("version", raw.get("versionConstraint", "*")),
            "dependency version",
            max_length=128,
        )
        optional = raw.get("optional", False)
        if not isinstance(optional, bool):
            raise _invalid("invalid_dependency_optional", "Dependency optional must be a boolean.")
        result.append(
            DependencySpec(coordinate=coordinate, version_constraint=version, optional=optional)
        )
    return tuple(result)


def _conflicts(value: object) -> tuple[ConflictSpec, ...]:
    result: list[ConflictSpec] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(_sequence(value, "spec.conflicts")):
        raw = _mapping(item, f"spec.conflicts[{index}]")
        conflict_type = _name(raw.get("type"), f"spec.conflicts[{index}].type")
        conflict_value = _string(
            raw.get("value"), f"spec.conflicts[{index}].value", max_length=2048
        )
        pair = (conflict_type, conflict_value)
        if pair in seen:
            raise _invalid("duplicate_conflict", "Conflicts must be unique by type and value.")
        seen.add(pair)
        result.append(ConflictSpec(conflict_type=conflict_type, value=conflict_value))
    return tuple(result)


def _tags(value: object) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _sequence(value, "tags"):
        tag = _name(item, "tag")
        _unique(tag, seen, "duplicate_tag", "Tags must be unique.")
        result.append(tag)
    return tuple(result)


def _summary(value: object) -> str:
    summary = _string(value, "spec.summary", max_length=MAX_SUMMARY_CHARS)
    if not summary.strip():
        raise _invalid("invalid_summary", "spec.summary must not be blank.")
    return summary


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _invalid("invalid_manifest_field", f"{field} must be an object.")
    return value


def _mapping_or_empty(value: object, field: str) -> Mapping[str, object]:
    if value is None:
        return {}
    return _mapping(value, field)


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _invalid("invalid_manifest_field", f"{field} must be an array.")
    return value


def _string(value: object, field: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise _invalid(
            "invalid_manifest_field", f"{field} must be a non-empty string within its size limit."
        )
    return value


def _name(value: object, field: str) -> str:
    name = _string(value, field, max_length=MAX_NAME_CHARS)
    if not _NAME_RE.fullmatch(name):
        raise _invalid("invalid_name", f"{field} must use lowercase capability-name syntax.")
    return name


def _coordinate(value: object, field: str) -> str:
    coordinate = _string(value, field, max_length=(MAX_NAME_CHARS * 2) + 1)
    parts = coordinate.split("/")
    if len(parts) != 2:
        raise _invalid("invalid_coordinate", f"{field} must be namespace/name.")
    _name(parts[0], field)
    _name(parts[1], field)
    return coordinate


def _enum(enum_type: type[_EnumT], value: object, field: str) -> _EnumT:
    if not isinstance(value, str):
        allowed = ", ".join(member.value for member in enum_type)
        raise _invalid("invalid_enum", f"{field} must be one of: {allowed}.")
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        allowed = ", ".join(member.value for member in enum_type)
        raise _invalid("invalid_enum", f"{field} must be one of: {allowed}.") from error


def _unique(value: str, seen: set[str], code: str, message: str) -> None:
    if value in seen:
        raise _invalid(code, message)
    seen.add(value)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Unsupported JSON constant: {value}")


def _invalid(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INPUT, safe_message=message)
