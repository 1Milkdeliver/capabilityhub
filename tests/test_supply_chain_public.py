from __future__ import annotations

import builtins
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from capabilityhub.lifecycle import StagedUpdateManager
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
)
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.supply_chain import (
    ArtifactMaterial,
    SupplyChainError,
    SupplyChainPolicy,
    SupplyChainVerifier,
    TrustedEd25519Key,
    create_ed25519_attestation,
)
from capabilityhub.update_store import SQLiteUpdateStore

ARTIFACT = b"signed public artifact"
DIGEST = "sha256:" + hashlib.sha256(ARTIFACT).hexdigest()


def _manifest() -> CapabilityManifest:
    return CapabilityManifest(
        CapabilityIdentity("demo", "public", "1.0.0", DIGEST),
        CapabilityKind.SKILL,
        "Public signed artifact",
        "fixture",
        (),
    )


def _material(
    *,
    issuer: str | None = "https://issuer.example",
    subject: str | None = "release@example.org",
    transparency: bool = True,
):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    key = TrustedEd25519Key(
        "release-key",
        "publisher",
        "registry",
        public,
        issuer=issuer,
        subject=subject,
        expires_at=2_000,
    )
    attestation = create_ed25519_attestation(
        revision=_manifest().identity.revision,
        artifact_digest=DIGEST,
        key=key,
        private_key=private,
        issued_at=900,
        expires_at=1_100,
        transparency_log_id="rekor.example" if transparency else None,
        transparency_entry_digest="a" * 64 if transparency else None,
    )
    return key, attestation


def _policy(key: TrustedEd25519Key, **changes) -> SupplyChainPolicy:
    values = {
        "environment": "production",
        "trusted_publishers": frozenset({"publisher"}),
        "trusted_registries": frozenset({"registry"}),
        "ed25519_keys": {key.key_id: key},
        "trusted_certificate_issuers": frozenset({"https://issuer.example"}),
        "trusted_certificate_subjects": frozenset({"release@example.org"}),
        "require_transparency": True,
    }
    values.update(changes)
    return SupplyChainPolicy(**values)


def _verify(policy: SupplyChainPolicy, attestation, artifact: bytes = ARTIFACT):
    return SupplyChainVerifier(policy, clock=lambda: 1_000).verify(
        _manifest(),
        artifact,
        publisher="publisher",
        registry="registry",
        attestation=attestation,
    )


def test_ed25519_offline_and_sigstore_identity_evidence_verify() -> None:
    key, attestation = _material()
    evidence = _verify(_policy(key), attestation)
    assert evidence.algorithm == "ed25519"
    assert evidence.signed is True

    offline_key, offline = _material(issuer=None, subject=None, transparency=False)
    evidence = _verify(
        _policy(
            offline_key,
            trusted_certificate_issuers=frozenset(),
            trusted_certificate_subjects=frozenset(),
            require_transparency=False,
        ),
        offline,
    )
    assert evidence.key_id == "release-key"


def test_public_verifier_rejects_tampered_artifact_before_signature_work() -> None:
    key, attestation = _material()
    with pytest.raises(SupplyChainError) as raised:
        _verify(_policy(key), attestation, artifact=b"tampered artifact")
    assert raised.value.code == "artifact_digest_mismatch"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            lambda item: replace(
                item,
                signature=("A" if item.signature[0] != "A" else "B")
                + item.signature[1:],
            ),
            "invalid_artifact_signature",
        ),
        (
            lambda item: replace(item, certificate_issuer="https://wrong.example"),
            "certificate_identity_mismatch",
        ),
        (lambda item: replace(item, expires_at=1_000), "attestation_expired"),
        (
            lambda item: replace(item, transparency_entry_digest=None),
            "transparency_evidence_required",
        ),
    ],
)
def test_public_attestation_tamper_identity_expiry_and_transparency_fail_closed(
    change, code: str
) -> None:
    key, attestation = _material()
    with pytest.raises(SupplyChainError) as raised:
        _verify(_policy(key), change(attestation))
    assert raised.value.code == code
    assert raised.value.details == {}


def test_revoked_authority_and_missing_crypto_fail_closed(monkeypatch) -> None:
    key, attestation = _material()
    with pytest.raises(SupplyChainError) as revoked:
        _verify(_policy(key, revoked_key_ids=frozenset({key.key_id})), attestation)
    assert revoked.value.code == "signing_authority_revoked"

    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(SupplyChainError) as unavailable:
        _verify(_policy(key), attestation)
    assert unavailable.value.code == "public_key_verifier_unavailable"


def test_stage_health_activate_reverify_public_evidence_and_preserve_pointer(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    registry = CapabilityRegistry()
    registry.register(manifest)
    key, attestation = _material()
    supplied = [attestation]
    manager = StagedUpdateManager(
        registry=registry,
        store=SQLiteUpdateStore(tmp_path / "updates.sqlite3"),
        verifier=SupplyChainVerifier(_policy(key), clock=lambda: 1_000),
        artifact_acquirer=lambda _revision: ArtifactMaterial(
            ARTIFACT, "publisher", "registry", supplied[0]
        ),
    )
    revision = manifest.identity.revision
    manager.stage(revision, expected_active_revision=None)
    manager.record_health(revision, passed=True)
    supplied[0] = replace(attestation, signature="tampered")
    with pytest.raises(SupplyChainError) as raised:
        manager.activate(revision, expected_active_revision=None)
    assert raised.value.code == "invalid_artifact_signature"
    assert manager.state("demo/public").active_revision is None
