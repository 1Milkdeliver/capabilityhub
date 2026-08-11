from __future__ import annotations

import json

import pytest

from capabilityhub.activation_lock import (
    export_activation_lock,
    export_activation_lock_json,
    load_activation_lock_json,
    validate_activation_lock,
    validate_activation_lock_json,
)
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.manifest import parse_manifest
from capabilityhub.registry import CapabilityRegistry


def _manifest(
    name: str,
    digest_character: str,
    *,
    version: str = "1.0.0",
    dependencies: list[dict[str, object]] | None = None,
    provider: str = "fixture",
    source: str = "registry://test",
):
    return parse_manifest(
        {
            "apiVersion": "capabilityhub.io/v1alpha1",
            "kind": "Capability",
            "metadata": {
                "namespace": "core",
                "name": name,
                "version": version,
                "digest": "sha256:" + digest_character * 64,
            },
            "spec": {
                "type": "api",
                "summary": "Test capability.",
                "provider": provider,
                "source": source,
                "operations": [{"name": "run"}],
                "dependencies": dependencies or [],
            },
        }
    )


def _active_registry(*manifests) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register_many(manifests)
    for manifest in manifests:
        registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    return registry


def test_export_is_deterministic_and_contains_exact_dependency_closure() -> None:
    base = _manifest("base", "a")
    middle = _manifest("middle", "b", dependencies=[{"coordinate": "core/base"}])
    top = _manifest("top", "c", dependencies=[{"coordinate": "core/middle"}])
    registry = _active_registry(base, middle, top)

    first = export_activation_lock(registry)
    second = export_activation_lock(registry)
    top_entry = first["capabilities"]["core/top"]  # type: ignore[index]

    assert first == second
    assert export_activation_lock_json(registry) == export_activation_lock_json(registry)
    assert top_entry["dependencyClosure"] == [  # type: ignore[index]
        base.identity.revision,
        middle.identity.revision,
    ]
    assert top_entry["revision"] == top.identity.revision  # type: ignore[index]
    assert top_entry["digest"] == top.identity.digest  # type: ignore[index]
    assert top_entry["provider"] == "fixture"  # type: ignore[index]
    assert top_entry["source"] == "registry://test"  # type: ignore[index]


def test_export_and_validation_are_read_only() -> None:
    capability = _manifest("one", "d")
    registry = _active_registry(capability)
    before_revisions = dict(registry.revisions)
    before_activations = dict(registry.activations)

    document = export_activation_lock(registry)
    result = validate_activation_lock(document, registry)

    assert result.capability_count == 1
    assert result.lock_digest == document["lockDigest"]
    assert dict(registry.revisions) == before_revisions
    assert dict(registry.activations) == before_activations


def test_json_round_trip_validates_exact_snapshot() -> None:
    registry = _active_registry(_manifest("one", "e"))
    serialized = export_activation_lock_json(registry)

    loaded = load_activation_lock_json(serialized)
    result = validate_activation_lock_json(serialized.encode(), registry)

    assert loaded == export_activation_lock(registry)
    assert result.capability_count == 1


@pytest.mark.parametrize("change", ["missing", "extra", "drifted"])
def test_import_fails_closed_for_registry_mismatch(change: str) -> None:
    original = _manifest("one", "f")
    locked_registry = _active_registry(original)
    document = export_activation_lock(locked_registry)
    if change == "missing":
        current = CapabilityRegistry()
    elif change == "extra":
        current = _active_registry(original, _manifest("two", "1"))
    else:
        current = _active_registry(_manifest("one", "2", version="2.0.0"))

    with pytest.raises(CapabilityHubError) as caught:
        validate_activation_lock(document, current)

    assert caught.value.code == "activation_lock_mismatch"
    assert caught.value.category is ErrorCategory.CONFLICT
    expected = ["core/two"] if change == "extra" else ["core/one"]
    assert caught.value.details[change] == expected


def test_tampered_lock_digest_and_duplicate_json_keys_are_rejected() -> None:
    registry = _active_registry(_manifest("one", "3"))
    document = export_activation_lock(registry)
    document["lockDigest"] = "sha256:" + "0" * 64

    with pytest.raises(CapabilityHubError) as digest_error:
        validate_activation_lock(document, registry)
    with pytest.raises(CapabilityHubError) as duplicate_error:
        load_activation_lock_json('{"kind":"one","kind":"two"}')

    assert digest_error.value.code == "activation_lock_digest_mismatch"
    assert duplicate_error.value.code == "invalid_activation_lock"


def test_entry_tampering_cannot_be_hidden_by_reusing_original_digest() -> None:
    registry = _active_registry(_manifest("one", "4"))
    document = export_activation_lock(registry)
    copied = json.loads(json.dumps(document))
    copied["capabilities"]["core/one"]["provider"] = "attacker"

    with pytest.raises(CapabilityHubError) as caught:
        validate_activation_lock(copied, registry)

    assert caught.value.code == "activation_lock_digest_mismatch"
