from __future__ import annotations

import json
from dataclasses import replace

import pytest

from capabilityhub.manifest import parse_manifest
from capabilityhub.manifest_export import export_manifest_json, manifest_to_document


def _document() -> dict[str, object]:
    return {
        "apiVersion": "capabilityhub.io/v1alpha1",
        "kind": "Capability",
        "metadata": {
            "namespace": "demo",
            "name": "records",
            "version": "1.2.3",
            "digest": "sha256:" + "a" * 64,
            "x-publisher": "community",
        },
        "spec": {
            "type": "api",
            "summary": "Read one record.",
            "provider": "api-driver",
            "driver": {"name": "api-driver", "profile": "local"},
            "operations": [
                {
                    "name": "read",
                    "operationType": "execute",
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "object"},
                    "sideEffect": "read",
                    "requiresApproval": False,
                }
            ],
            "sections": {
                "contract": {
                    "content": "contract text",
                    "mediaType": "text/plain",
                    "portableTokens": 4,
                    "sensitive": False,
                }
            },
            "permissions": ["network"],
            "dependencies": [
                {"coordinate": "demo/auth", "versionConstraint": "^1", "optional": True}
            ],
            "conflicts": [{"type": "route", "value": "/records"}],
            "tags": ["records"],
            "trust": {"tier": "verified", "source": "registry://demo"},
            "x-runtime-hint": {"pool": "isolated"},
        },
        "x-envelope": {"source": "portable"},
    }


def test_export_is_deterministic_and_round_trips_manifest_semantics() -> None:
    manifest = parse_manifest(_document())

    first = export_manifest_json(manifest)
    second = export_manifest_json(manifest)
    restored = parse_manifest(json.loads(first))

    assert first == second
    assert '": ' not in first
    assert '", ' not in first
    assert restored.identity == manifest.identity
    assert restored.operations == manifest.operations
    assert restored.sections == manifest.sections
    assert restored.dependencies == manifest.dependencies
    assert restored.conflicts == manifest.conflicts
    assert restored.permissions == manifest.permissions
    assert restored.trust_tier == "verified"
    assert restored.source == "registry://demo"
    assert manifest_to_document(restored)["x-envelope"] == {"source": "portable"}


def test_export_preserves_root_and_spec_extensions_as_inert_json() -> None:
    document = manifest_to_document(parse_manifest(_document()))

    assert document["metadata"]["x-publisher"] == "community"  # type: ignore[index]
    assert document["spec"]["x-runtime-hint"] == {"pool": "isolated"}  # type: ignore[index]
    assert document["spec"]["driver"] == {  # type: ignore[index]
        "name": "api-driver",
        "profile": "local",
    }
    assert document["x-envelope"] == {"source": "portable"}


def test_extension_cannot_override_a_standard_export_field() -> None:
    manifest = parse_manifest(_document())
    metadata = dict(manifest.metadata)
    metadata["extensions"] = {"summary": "changed"}

    with pytest.raises(ValueError, match="standard field: summary"):
        manifest_to_document(replace(manifest, metadata=metadata))


def test_export_requires_no_provider_object_or_execution_callback() -> None:
    manifest = parse_manifest(_document())

    exported = manifest_to_document(manifest)

    assert exported["spec"]["provider"] == "api-driver"  # type: ignore[index]
