from __future__ import annotations

import hashlib
from base64 import urlsafe_b64encode
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from capabilityhub.lifecycle import StagedUpdateManager
from capabilityhub.models import CapabilityIdentity, CapabilityKind, CapabilityManifest
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.supply_chain import ArtifactMaterial, SupplyChainError, TrustEvidence
from capabilityhub.supply_chain_bundle import (
    BundleTrustPolicy,
    CheckpointObserver,
    SignedCheckpoint,
    SigstoreBundle,
    SigstoreBundleVerifier,
    TransparencyInclusionProof,
    TrustedCertificateRoot,
    TrustedTransparencyLog,
    _artifact_payload,
    _certificate_fingerprint,
    _checkpoint_payload,
    _log_entry,
)
from capabilityhub.update_store import SQLiteUpdateStore

ARTIFACT = b"portable signed bundle"
DIGEST = "sha256:" + hashlib.sha256(ARTIFACT).hexdigest()
NOW = 1_800_000_000


def _b64(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _manifest() -> CapabilityManifest:
    return CapabilityManifest(
        CapabilityIdentity("demo", "bundle", "1.0.0", DIGEST),
        CapabilityKind.SKILL,
        "bundle fixture",
        "fixture",
        (),
    )


def _certificate_chain() -> tuple[
    Ed25519PrivateKey,
    x509.Certificate,
    x509.Certificate,
    tuple[bytes, bytes],
]:
    root_key = Ed25519PrivateKey.generate()
    leaf_key = Ed25519PrivateKey.generate()
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Fulcio")])
    moment = datetime.fromtimestamp(NOW, UTC)
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(1)
        .not_valid_before(moment - timedelta(days=1))
        .not_valid_after(moment + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(root_key, algorithm=None)
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "release")]))
        .issuer_name(root_name)
        .public_key(leaf_key.public_key())
        .serial_number(2)
        .not_valid_before(moment - timedelta(hours=1))
        .not_valid_after(moment + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name("release@example.org")]),
            critical=False,
        )
        .sign(root_key, algorithm=None)
    )
    encoding = serialization.Encoding.PEM
    return leaf_key, leaf, root, (leaf.public_bytes(encoding), root.public_bytes(encoding))


def _fixture(
    *, checkpoint_timestamp: int = NOW - 10
) -> tuple[
    CapabilityManifest,
    SigstoreBundle,
    BundleTrustPolicy,
    Ed25519PrivateKey,
]:
    manifest = _manifest()
    leaf_key, leaf, root, chain = _certificate_chain()
    log_key = Ed25519PrivateKey.generate()
    payload = _artifact_payload(manifest, DIGEST, "publisher", "registry", NOW - 20)
    signature = _b64(leaf_key.sign(b"capabilityhub-sigstore-bundle-artifact-v1\0" + payload))
    entry = _log_entry(payload, signature, leaf)
    root_hash = hashlib.sha256(b"\x00" + entry).hexdigest()
    unsigned_checkpoint = SignedCheckpoint(
        "rekor.example", 1, root_hash, checkpoint_timestamp, ""
    )
    checkpoint = replace(
        unsigned_checkpoint,
        signature=_b64(
            log_key.sign(
                b"capabilityhub-transparency-checkpoint-v1\0"
                + _checkpoint_payload(unsigned_checkpoint)
            )
        ),
    )
    bundle = SigstoreBundle(
        chain,
        signature,
        NOW - 20,
        TransparencyInclusionProof(checkpoint, 0),
    )
    root_id = _certificate_fingerprint(root)
    policy = BundleTrustPolicy(
        frozenset({"publisher"}),
        frozenset({"registry"}),
        {
            root_id: TrustedCertificateRoot(
                root_id, chain[-1], "publisher", "registry"
            )
        },
        {
            "rekor.example": TrustedTransparencyLog(
                "rekor.example",
                log_key.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                ),
            )
        },
        frozenset({"CN=Test Fulcio"}),
        frozenset({"release@example.org"}),
    )
    return manifest, bundle, policy, log_key


def _verify(
    bundle: SigstoreBundle,
    policy: BundleTrustPolicy,
    *,
    online_checkpoint: Callable[[str], SignedCheckpoint] | None = None,
    observer: CheckpointObserver | None = None,
) -> TrustEvidence:
    return SigstoreBundleVerifier(
        policy,
        clock=lambda: NOW,
        online_checkpoint=online_checkpoint,
        observer=observer,
    ).verify(
        _manifest(),
        ARTIFACT,
        publisher="publisher",
        registry="registry",
        attestation=None,
        bundle=bundle,
    )


def test_offline_bundle_verifies_chain_signature_and_inclusion_proof() -> None:
    _, bundle, policy, _ = _fixture()
    evidence = _verify(bundle, policy)
    assert evidence.algorithm == "ed25519"
    assert evidence.environment == "production"
    assert evidence.key_id is not None


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda bundle: replace(bundle, artifact_signature="tampered"),
            "invalid_artifact_signature",
        ),
        (
            lambda bundle: replace(
                bundle,
                inclusion_proof=replace(
                    bundle.inclusion_proof,
                    checkpoint=replace(
                        bundle.inclusion_proof.checkpoint, root_hash="0" * 64
                    ),
                ),
            ),
            "invalid_transparency_checkpoint",
        ),
    ],
)
def test_bundle_tamper_fails_closed(
    mutate: Callable[[SigstoreBundle], SigstoreBundle], code: str
) -> None:
    _, bundle, policy, _ = _fixture()
    with pytest.raises(SupplyChainError) as raised:
        _verify(mutate(bundle), policy)
    assert raised.value.code == code
    assert raised.value.details == {}


def test_revoked_distributed_root_and_log_keys_fail_closed() -> None:
    _, bundle, policy, _ = _fixture()
    root_id, root = next(iter(policy.roots.items()))
    with pytest.raises(SupplyChainError) as revoked_root:
        _verify(bundle, replace(policy, roots={root_id: replace(root, revoked=True)}))
    assert revoked_root.value.code == "certificate_root_not_trusted"

    log_id, log = next(iter(policy.logs.items()))
    with pytest.raises(SupplyChainError) as revoked_log:
        _verify(bundle, replace(policy, logs={log_id: replace(log, revoked=True)}))
    assert revoked_log.value.code == "transparency_log_not_trusted"


def test_production_requires_bundle_and_online_freshness_when_configured() -> None:
    _, bundle, policy, _ = _fixture(checkpoint_timestamp=NOW - 5000)
    required = replace(policy, require_online_checkpoint=True, max_checkpoint_age_seconds=60)
    with pytest.raises(SupplyChainError) as missing:
        SigstoreBundleVerifier(required, clock=lambda: NOW).verify(
            _manifest(),
            ARTIFACT,
            publisher="publisher",
            registry="registry",
            attestation=None,
            bundle=None,
        )
    assert missing.value.code == "transparency_bundle_required"
    with pytest.raises(SupplyChainError) as stale:
        _verify(
            bundle,
            required,
            online_checkpoint=lambda _log_id: bundle.inclusion_proof.checkpoint,
        )
    assert stale.value.code == "transparency_checkpoint_stale"


def test_online_checkpoint_and_observer_detect_replay_and_log_fork() -> None:
    _, bundle, policy, log_key = _fixture()
    policy = replace(policy, require_online_checkpoint=True)
    checkpoint = bundle.inclusion_proof.checkpoint
    _verify(bundle, policy, online_checkpoint=lambda _log_id: checkpoint)

    observer = CheckpointObserver()
    observer.observe(replace(checkpoint, tree_size=2, timestamp=NOW))
    with pytest.raises(SupplyChainError) as replayed:
        _verify(bundle, replace(policy, require_online_checkpoint=False), observer=observer)
    assert replayed.value.code == "transparency_checkpoint_replayed"

    fork = replace(checkpoint, root_hash="f" * 64, signature="")
    fork = replace(
        fork,
        signature=_b64(
            log_key.sign(
                b"capabilityhub-transparency-checkpoint-v1\0"
                + _checkpoint_payload(fork)
            )
        ),
    )
    with pytest.raises(SupplyChainError) as forked:
        _verify(bundle, policy, online_checkpoint=lambda _log_id: fork)
    assert forked.value.code == "transparency_log_fork"


def test_staged_update_reverifies_bundle_before_activation(tmp_path: Path) -> None:
    manifest, bundle, policy, _ = _fixture()
    registry = CapabilityRegistry()
    registry.register(manifest)
    supplied = [bundle]
    manager = StagedUpdateManager(
        registry=registry,
        store=SQLiteUpdateStore(tmp_path / "bundle-updates.sqlite3"),
        verifier=SigstoreBundleVerifier(policy, clock=lambda: NOW),
        artifact_acquirer=lambda _revision: ArtifactMaterial(
            ARTIFACT,
            "publisher",
            "registry",
            bundle=supplied[0],
        ),
    )
    revision = manifest.identity.revision
    manager.stage(revision, expected_active_revision=None)
    manager.record_health(revision, passed=True)
    supplied[0] = replace(bundle, artifact_signature="tampered")
    with pytest.raises(SupplyChainError) as raised:
        manager.activate(revision, expected_active_revision=None)
    assert raised.value.code == "invalid_artifact_signature"
    assert manager.state(manifest.identity.coordinate).active_revision is None
