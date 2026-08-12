from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from capabilityhub.lifecycle import StagedUpdateManager
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
)
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.supply_chain import (
    ArtifactMaterial,
    SupplyChainError,
    SupplyChainPolicy,
    SupplyChainVerifier,
    TrustedHMACKey,
    create_local_hmac_attestation,
)
from capabilityhub.update_store import SQLiteUpdateStore


def _manifest(version: str, artifact: bytes) -> CapabilityManifest:
    digest = "sha256:" + hashlib.sha256(artifact).hexdigest()
    return CapabilityManifest(
        CapabilityIdentity("trusted", "tool", version, digest),
        CapabilityKind.CLI,
        "Trusted lifecycle fixture.",
        "fixture",
        (OperationSpec("run", OperationType.EXECUTE),),
    )


def _production_materials(manifests: tuple[CapabilityManifest, ...], artifacts: dict[str, bytes]):
    key = TrustedHMACKey(
        "local-hmac-key",
        "trusted-publisher",
        "trusted-registry",
        b"shared-local-hmac-secret",
        not_before=100,
        expires_at=1_000,
    )
    policy = SupplyChainPolicy(
        environment="production",
        trusted_publishers=frozenset({key.publisher}),
        trusted_registries=frozenset({key.registry}),
        keys={key.key_id: key},
    )
    materials = {
        manifest.identity.revision: ArtifactMaterial(
            artifacts[manifest.identity.version],
            key.publisher,
            key.registry,
            create_local_hmac_attestation(
                revision=manifest.identity.revision,
                artifact_digest=manifest.identity.digest,
                key=key,
                issued_at=200,
                expires_at=800,
            ),
        )
        for manifest in manifests
    }
    return SupplyChainVerifier(policy, clock=lambda: 500), materials


def _registry(*manifests: CapabilityManifest) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register_many(manifests)
    return registry


def test_default_update_manager_fails_closed_before_staging(tmp_path) -> None:
    artifact = b"candidate"
    candidate = _manifest("2", artifact)
    store = SQLiteUpdateStore(tmp_path / "updates.sqlite3")
    manager = StagedUpdateManager(registry=_registry(candidate), store=store)

    with pytest.raises(SupplyChainError) as caught:
        manager.stage(candidate.identity.revision, expected_active_revision=None)

    assert caught.value.code == "artifact_trust_not_configured"
    assert manager.state(candidate.identity.coordinate).active_revision is None
    assert manager.state(candidate.identity.coordinate).staged_revision is None


def test_production_stage_health_activate_reverify_same_artifact(tmp_path) -> None:
    artifact = b"production candidate"
    candidate = _manifest("2", artifact)
    verifier, materials = _production_materials((candidate,), {"2": artifact})
    calls: list[str] = []

    def acquire(revision: str) -> ArtifactMaterial:
        calls.append(revision)
        return materials[revision]

    manager = StagedUpdateManager(
        registry=_registry(candidate),
        store=SQLiteUpdateStore(tmp_path / "updates.sqlite3"),
        verifier=verifier,
        artifact_acquirer=acquire,
    )

    manager.stage(candidate.identity.revision, expected_active_revision=None)
    manager.record_health(candidate.identity.revision, passed=True)
    activated = manager.activate(candidate.identity.revision, expected_active_revision=None)

    assert activated.active_revision == candidate.identity.revision
    assert calls == [candidate.identity.revision] * 3
    assert materials[candidate.identity.revision].attestation is not None
    assert materials[candidate.identity.revision].attestation.algorithm == "hmac-sha256"


def test_digest_failure_before_activation_keeps_active_pointer_and_health(tmp_path) -> None:
    first_artifact = b"first"
    second_artifact = b"second"
    first = _manifest("1", first_artifact)
    second = _manifest("2", second_artifact)
    verifier, materials = _production_materials(
        (first, second), {"1": first_artifact, "2": second_artifact}
    )
    manager = StagedUpdateManager(
        registry=_registry(first, second),
        store=SQLiteUpdateStore(tmp_path / "updates.sqlite3"),
        verifier=verifier,
        artifact_acquirer=lambda revision: materials[revision],
    )
    manager.stage(first.identity.revision, expected_active_revision=None)
    manager.record_health(first.identity.revision, passed=True)
    manager.activate(first.identity.revision, expected_active_revision=None)
    manager.stage(second.identity.revision, expected_active_revision=first.identity.revision)
    manager.record_health(second.identity.revision, passed=True)
    materials[second.identity.revision] = replace(
        materials[second.identity.revision], artifact=b"tampered after health"
    )

    with pytest.raises(SupplyChainError) as caught:
        manager.activate(
            second.identity.revision,
            expected_active_revision=first.identity.revision,
        )

    state = manager.state(second.identity.coordinate)
    assert caught.value.code == "artifact_digest_mismatch"
    assert state.active_revision == first.identity.revision
    assert state.staged_revision == second.identity.revision
    assert state.health_status == "passed"


def test_attestation_policy_failure_does_not_stage_or_replace_pointer(tmp_path) -> None:
    artifact = b"candidate"
    candidate = _manifest("2", artifact)
    verifier, materials = _production_materials((candidate,), {"2": artifact})
    material = materials[candidate.identity.revision]
    assert material.attestation is not None
    materials[candidate.identity.revision] = replace(
        material,
        attestation=replace(material.attestation, publisher="untrusted"),
    )
    manager = StagedUpdateManager(
        registry=_registry(candidate),
        store=SQLiteUpdateStore(tmp_path / "updates.sqlite3"),
        verifier=verifier,
        artifact_acquirer=lambda revision: materials[revision],
    )

    with pytest.raises(SupplyChainError) as caught:
        manager.stage(candidate.identity.revision, expected_active_revision=None)

    assert caught.value.code == "attestation_binding_mismatch"
    assert manager.state(candidate.identity.coordinate).staged_revision is None


def test_explicit_unsigned_development_mode_keeps_rollback_and_pins(tmp_path) -> None:
    first_artifact = b"development-first"
    second_artifact = b"development-second"
    first = _manifest("1", first_artifact)
    second = _manifest("2", second_artifact)
    policy = SupplyChainPolicy(
        environment="development",
        trusted_publishers=frozenset({"local-development"}),
        trusted_registries=frozenset({"local-development"}),
        allow_unsigned_development=True,
    )
    artifacts = {"1": first_artifact, "2": second_artifact}
    manager = StagedUpdateManager(
        registry=_registry(first, second),
        store=SQLiteUpdateStore(tmp_path / "updates.sqlite3"),
        verifier=SupplyChainVerifier(policy, clock=lambda: 500),
        artifact_acquirer=lambda revision: ArtifactMaterial(
            artifacts[manager.registry.revision(revision).identity.version],
            "local-development",
            "local-development",
        ),
    )
    manager.stage(first.identity.revision, expected_active_revision=None)
    manager.record_health(first.identity.revision, passed=True)
    manager.activate(first.identity.revision, expected_active_revision=None)
    pin = manager.pin_active(first.identity.coordinate, "in-flight")
    manager.stage(second.identity.revision, expected_active_revision=first.identity.revision)
    manager.record_health(second.identity.revision, passed=True)
    manager.activate(second.identity.revision, expected_active_revision=first.identity.revision)

    rolled_back = manager.rollback(
        first.identity.coordinate, expected_active_revision=second.identity.revision
    )

    assert rolled_back.active_revision == first.identity.revision
    assert manager.pins(first.identity.coordinate) == (pin,)
