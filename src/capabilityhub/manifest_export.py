"""Deterministic JSON export for immutable CapabilityManifest values."""

from __future__ import annotations

from collections.abc import Mapping

from capabilityhub.compatibility import V1ALPHA1
from capabilityhub.metering import canonical_json
from capabilityhub.models import CapabilityManifest, JsonValue

_INTERNAL_METADATA = {
    "namespace",
    "name",
    "version",
    "digest",
    "tags",
    "driver",
    "extensions",
    "root_extensions",
}


def manifest_to_document(manifest: CapabilityManifest) -> dict[str, JsonValue]:
    """Return a portable v1alpha1 document without invoking provider code."""

    metadata: dict[str, JsonValue] = {
        "digest": manifest.identity.digest,
        "name": manifest.identity.name,
        "namespace": manifest.identity.namespace,
        "version": manifest.identity.version,
    }
    for key, value in manifest.metadata.items():
        if key not in _INTERNAL_METADATA:
            metadata[key] = value

    operations: list[JsonValue] = []
    for operation in manifest.operations:
        exported: dict[str, JsonValue] = {
            "name": operation.name,
            "operationType": operation.operation_type.value,
            "requiresApproval": operation.requires_approval,
            "sideEffect": operation.side_effect.value,
        }
        if operation.input_schema:
            exported["inputSchema"] = dict(operation.input_schema)
        if operation.output_schema:
            exported["outputSchema"] = dict(operation.output_schema)
        operations.append(exported)

    sections: dict[str, JsonValue] = {
        section.name: {
            "content": section.content,
            "mediaType": section.media_type,
            "portableTokens": section.portable_tokens,
            "sensitive": section.sensitive,
        }
        for section in manifest.sections
    }
    spec: dict[str, JsonValue] = {
        "conflicts": [
            {"type": conflict.conflict_type, "value": conflict.value}
            for conflict in manifest.conflicts
        ],
        "dependencies": [
            {
                "coordinate": dependency.coordinate,
                "optional": dependency.optional,
                "versionConstraint": dependency.version_constraint,
            }
            for dependency in manifest.dependencies
        ],
        "operations": operations,
        "permissions": list(manifest.permissions),
        "provider": manifest.provider,
        "sections": sections,
        "summary": manifest.summary,
        "tags": list(manifest.tags),
        "trust": {"source": manifest.source, "tier": manifest.trust_tier},
        "type": manifest.kind.value,
    }
    driver = manifest.metadata.get("driver")
    if isinstance(driver, Mapping):
        spec["driver"] = dict(driver)
    extensions = manifest.metadata.get("extensions")
    if extensions is not None:
        if not isinstance(extensions, Mapping) or not all(
            isinstance(key, str) for key in extensions
        ):
            raise ValueError("manifest metadata extensions must be an object")
        for key, value in extensions.items():
            if key in spec:
                if spec[key] != value:
                    raise ValueError(f"extension conflicts with standard field: {key}")
                continue
            spec[key] = value

    document: dict[str, JsonValue] = {
        "apiVersion": V1ALPHA1,
        "kind": "Capability",
        "metadata": metadata,
        "spec": spec,
    }
    root_extensions = manifest.metadata.get("root_extensions")
    if root_extensions is not None:
        if not isinstance(root_extensions, Mapping) or not all(
            isinstance(key, str) for key in root_extensions
        ):
            raise ValueError("manifest metadata root_extensions must be an object")
        for key, value in root_extensions.items():
            if key in document:
                if document[key] != value:
                    raise ValueError(f"root extension conflicts with standard field: {key}")
                continue
            document[key] = value
    return document


def export_manifest_json(manifest: CapabilityManifest) -> str:
    """Serialize one manifest canonically with UTF-8-safe JSON text."""

    return canonical_json(manifest_to_document(manifest))
