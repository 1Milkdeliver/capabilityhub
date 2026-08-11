from __future__ import annotations

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.manifest import parse_manifest
from capabilityhub.migration import migrate_manifest


def _legacy_document() -> dict[str, object]:
    return {
        "api_version": "capabilityhub.io/v1alpha0",
        "manifestKind": "capability",
        "metadata": {
            "namespaceName": "demo",
            "packageName": "legacy",
            "version": "1",
            "contentDigest": "sha256:" + "b" * 64,
            "x-owner": "community",
        },
        "spec": {
            "capabilityType": "api",
            "description": "Migrated manifest.",
            "providerName": "static",
            "operations": [
                {
                    "name": "read",
                    "operation_type": "execute",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "side_effect": "read",
                    "requires_approval": False,
                }
            ],
            "sections": {
                "contract": {
                    "content": "contract",
                    "media_type": "text/plain",
                    "portable_tokens": 2,
                }
            },
            "dependencies": [
                {
                    "coordinate": "demo/auth",
                    "version_constraint": "^1",
                    "optional": True,
                }
            ],
            "compatibility": {
                "required_features": ["security.future-required"],
                "optional_features": ["runtime.future-optional"],
            },
            "x-driver-config": {"mode": "safe"},
        },
        "x-envelope": {"source": "legacy-import"},
    }


def test_migration_normalizes_explicit_aliases_and_produces_report() -> None:
    result = migrate_manifest(_legacy_document())
    manifest = parse_manifest(result.document)

    assert manifest.identity.coordinate == "demo/legacy"
    assert manifest.summary == "Migrated manifest."
    assert manifest.operation("read").side_effect.value == "read"  # type: ignore[union-attr]
    assert manifest.dependencies[0].version_constraint == "^1"
    assert result.report.source_version == "capabilityhub.io/v1alpha0"
    assert result.report.target_version == "capabilityhub.io/v1alpha1"
    assert result.report.changed is True
    assert "/spec/operations/0/requires_approval->requiresApproval" in result.report.changes
    assert result.report.preserved_extensions == (
        "/metadata/x-owner",
        "/spec/x-driver-config",
        "/x-envelope",
    )


def test_migration_is_idempotent_and_preserves_extensions_exactly() -> None:
    first = migrate_manifest(_legacy_document())
    second = migrate_manifest(first.document)

    assert second.document == first.document
    assert second.report.changed is False
    assert second.report.changes == ()
    assert second.report.source_version == "capabilityhub.io/v1alpha1"
    assert second.document["x-envelope"] == {"source": "legacy-import"}
    spec = second.document["spec"]
    assert isinstance(spec, dict)
    assert spec["x-driver-config"] == {"mode": "safe"}
    assert spec["compatibility"] == {
        "requiredFeatures": ["security.future-required"],
        "optionalFeatures": ["runtime.future-optional"],
    }


def test_conflicting_alias_and_current_field_fail_closed() -> None:
    document = _legacy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    spec["summary"] = "A conflicting current value."

    with pytest.raises(CapabilityHubError) as conflict:
        migrate_manifest(document)

    assert conflict.value.code == "migration_alias_conflict"
    assert conflict.value.details == {
        "path": "/spec",
        "legacy": "description",
        "current": "summary",
    }


def test_equal_alias_is_removed_once_without_data_loss() -> None:
    document = _legacy_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    spec["summary"] = spec["description"]

    first = migrate_manifest(document)
    second = migrate_manifest(first.document)

    migrated_spec = first.document["spec"]
    assert isinstance(migrated_spec, dict)
    assert "description" not in migrated_spec
    assert migrated_spec["summary"] == "Migrated manifest."
    assert second.report.changed is False


@pytest.mark.parametrize("version", ["v1", "capabilityhub.io/v2"])
def test_unsupported_source_version_is_rejected(version) -> None:
    document = _legacy_document()
    document["api_version"] = version

    with pytest.raises(CapabilityHubError) as unsupported:
        migrate_manifest(document)

    assert unsupported.value.code == "migration_unsupported_version"


def test_migration_rejects_non_json_extension_values() -> None:
    document = _legacy_document()
    document["x-not-json"] = {1, 2, 3}

    with pytest.raises(CapabilityHubError) as invalid:
        migrate_manifest(document)

    assert invalid.value.code == "migration_invalid_json"
