"""Deterministic, read-only activation locks for portable registry validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from capabilityhub.compatibility import V1ALPHA1
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import canonical_json
from capabilityhub.models import CapabilityManifest, JsonValue
from capabilityhub.registry import CapabilityRegistry

LOCK_KIND = "CapabilityActivationLock"
MAX_LOCK_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class ActivationLockValidation:
    lock_digest: str
    capability_count: int


def export_activation_lock(registry: CapabilityRegistry) -> dict[str, JsonValue]:
    """Snapshot active coordinates without mutating or executing the registry."""

    active = dict(registry.activations)
    manifests = {coordinate: registry.revision(revision) for coordinate, revision in active.items()}
    capabilities: dict[str, JsonValue] = {}
    for coordinate in sorted(manifests):
        manifest = manifests[coordinate]
        capabilities[coordinate] = _entry(manifest, _dependency_closure(coordinate, manifests))
    unsigned: dict[str, JsonValue] = {
        "apiVersion": V1ALPHA1,
        "kind": LOCK_KIND,
        "capabilities": capabilities,
    }
    return {**unsigned, "lockDigest": _digest(unsigned)}


def export_activation_lock_json(registry: CapabilityRegistry) -> str:
    return canonical_json(export_activation_lock(registry))


def load_activation_lock_json(source: str | bytes) -> dict[str, JsonValue]:
    text = _lock_text(source)
    if len(text.encode("utf-8")) > MAX_LOCK_BYTES:
        raise _lock_input("activation_lock_size_limit", "The activation lock is too large.")
    try:
        loaded = json.loads(text, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise _lock_input("invalid_activation_lock", "The activation lock is invalid.") from exc
    if not isinstance(loaded, dict):
        raise _lock_input("invalid_activation_lock", "The activation lock root must be an object.")
    return cast(dict[str, JsonValue], loaded)


def validate_activation_lock(
    document: Mapping[str, Any], registry: CapabilityRegistry
) -> ActivationLockValidation:
    """Fail closed unless the lock exactly matches the active registry snapshot."""

    imported = _validated_document(document)
    supplied_digest = cast(str, imported["lockDigest"])
    unsigned = {key: value for key, value in imported.items() if key != "lockDigest"}
    if supplied_digest != _digest(unsigned):
        raise _lock_input(
            "activation_lock_digest_mismatch", "The activation lock digest is invalid."
        )

    expected = export_activation_lock(registry)
    locked_capabilities = cast(dict[str, JsonValue], imported["capabilities"])
    active_capabilities = cast(dict[str, JsonValue], expected["capabilities"])
    locked_coordinates = set(locked_capabilities)
    active_coordinates = set(active_capabilities)
    missing = sorted(locked_coordinates - active_coordinates)
    extra = sorted(active_coordinates - locked_coordinates)
    drifted = sorted(
        coordinate
        for coordinate in locked_coordinates & active_coordinates
        if locked_capabilities[coordinate] != active_capabilities[coordinate]
    )
    if missing or extra or drifted:
        raise CapabilityHubError(
            code="activation_lock_mismatch",
            category=ErrorCategory.CONFLICT,
            safe_message="The active registry does not exactly match the activation lock.",
            details={"missing": missing, "extra": extra, "drifted": drifted},
        )
    return ActivationLockValidation(
        lock_digest=supplied_digest,
        capability_count=len(locked_capabilities),
    )


def validate_activation_lock_json(
    source: str | bytes, registry: CapabilityRegistry
) -> ActivationLockValidation:
    return validate_activation_lock(load_activation_lock_json(source), registry)


def _entry(manifest: CapabilityManifest, closure: tuple[str, ...]) -> dict[str, JsonValue]:
    return {
        "revision": manifest.identity.revision,
        "digest": manifest.identity.digest,
        "provider": manifest.provider,
        "source": manifest.source,
        "dependencyClosure": list(closure),
    }


def _dependency_closure(root: str, manifests: Mapping[str, CapabilityManifest]) -> tuple[str, ...]:
    found: set[str] = set()
    pending = [root]
    while pending:
        coordinate = pending.pop()
        for dependency in manifests[coordinate].dependencies:
            target = manifests.get(dependency.coordinate)
            if target is None or dependency.coordinate in found:
                continue
            found.add(dependency.coordinate)
            pending.append(dependency.coordinate)
    found.discard(root)
    return tuple(sorted(manifests[coordinate].identity.revision for coordinate in found))


def _validated_document(document: Mapping[str, Any]) -> dict[str, JsonValue]:
    if not isinstance(document, Mapping) or any(not isinstance(key, str) for key in document):
        raise _lock_input("invalid_activation_lock", "The activation lock is invalid.")
    if set(document) != {"apiVersion", "kind", "capabilities", "lockDigest"}:
        raise _lock_input("invalid_activation_lock", "The activation lock fields are invalid.")
    if document.get("apiVersion") != V1ALPHA1 or document.get("kind") != LOCK_KIND:
        raise _lock_input("unsupported_activation_lock", "The activation lock is unsupported.")
    digest = document.get("lockDigest")
    capabilities = document.get("capabilities")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise _lock_input("invalid_activation_lock", "The activation lock digest is invalid.")
    if not isinstance(capabilities, Mapping) or any(
        not isinstance(key, str) for key in capabilities
    ):
        raise _lock_input("invalid_activation_lock", "Lock capabilities must be an object.")
    normalized_capabilities: dict[str, JsonValue] = {}
    for coordinate, raw_entry in capabilities.items():
        if not coordinate or not isinstance(raw_entry, Mapping):
            raise _lock_input("invalid_activation_lock", "A lock entry is invalid.")
        expected_fields = {
            "revision",
            "digest",
            "provider",
            "source",
            "dependencyClosure",
        }
        if set(raw_entry) != expected_fields:
            raise _lock_input("invalid_activation_lock", "A lock entry has invalid fields.")
        scalar_fields = ("revision", "digest", "provider", "source")
        if any(not isinstance(raw_entry.get(field), str) for field in scalar_fields):
            raise _lock_input("invalid_activation_lock", "A lock entry is invalid.")
        closure = raw_entry.get("dependencyClosure")
        if not isinstance(closure, list) or any(not isinstance(item, str) for item in closure):
            raise _lock_input("invalid_activation_lock", "A dependency closure is invalid.")
        if closure != sorted(set(closure)):
            raise _lock_input(
                "invalid_activation_lock", "A dependency closure must be unique and sorted."
            )
        normalized_capabilities[coordinate] = {
            field: cast(JsonValue, raw_entry[field]) for field in sorted(expected_fields)
        }
    return {
        "apiVersion": V1ALPHA1,
        "kind": LOCK_KIND,
        "capabilities": normalized_capabilities,
        "lockDigest": digest,
    }


def _digest(document: Mapping[str, JsonValue]) -> str:
    encoded = canonical_json(dict(document)).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _lock_text(source: str | bytes) -> str:
    if isinstance(source, str):
        return source
    if not isinstance(source, bytes):
        raise TypeError("activation lock source must be str or bytes")
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _lock_input("invalid_activation_lock", "The activation lock must be UTF-8.") from exc


def _lock_input(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INPUT, safe_message=message)
