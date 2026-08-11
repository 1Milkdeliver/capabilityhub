from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
)
from capabilityhub.supply_chain import (
    ArtifactAttestation,
    SupplyChainError,
    SupplyChainPolicy,
    SupplyChainVerifier,
    TrustedHMACKey,
    create_local_hmac_attestation,
)

ARTIFACT = b"deterministic artifact bytes"
DIGEST = "sha256:" + hashlib.sha256(ARTIFACT).hexdigest()


def _manifest(digest: str = DIGEST) -> CapabilityManifest:
    return CapabilityManifest(
        CapabilityIdentity("demo", "artifact", "1.0.0", digest),
        CapabilityKind.CLI,
        "Supply chain fixture.",
        "fixture",
        (OperationSpec("run", OperationType.EXECUTE),),
    )


def _key(**changes: object) -> TrustedHMACKey:
    values = {
        "key_id": "publisher-key-1",
        "publisher": "trusted-publisher",
        "registry": "trusted-registry",
        "secret": b"local shared secret",
        "not_before": 100,
        "expires_at": 1_000,
    }
    values.update(changes)
    return TrustedHMACKey(**values)  # type: ignore[arg-type]


def _policy(
    *,
    environment: str = "production",
    key: TrustedHMACKey | None = None,
    **changes: object,
) -> SupplyChainPolicy:
    trusted_key = key or _key()
    values = {
        "environment": environment,
        "trusted_publishers": frozenset({"trusted-publisher"}),
        "trusted_registries": frozenset({"trusted-registry"}),
        "keys": {trusted_key.key_id: trusted_key},
    }
    values.update(changes)
    return SupplyChainPolicy(**values)  # type: ignore[arg-type]


def _attestation(key: TrustedHMACKey | None = None) -> ArtifactAttestation:
    return create_local_hmac_attestation(
        revision=_manifest().identity.revision,
        artifact_digest=DIGEST,
        key=key or _key(),
        issued_at=200,
        expires_at=800,
    )


def _verify(
    policy: SupplyChainPolicy,
    attestation: ArtifactAttestation | None,
    *,
    manifest: CapabilityManifest | None = None,
    artifact: bytes = ARTIFACT,
):
    return SupplyChainVerifier(policy, clock=lambda: 500).verify(
        manifest or _manifest(),
        artifact,
        publisher="trusted-publisher",
        registry="trusted-registry",
        attestation=attestation,
    )


def test_production_verifies_digest_and_local_hmac_evidence() -> None:
    evidence = _verify(_policy(), _attestation())

    assert evidence.signed
    assert evidence.artifact_digest == DIGEST
    assert evidence.key_id == "publisher-key-1"
    assert evidence.environment == "production"


def test_artifact_bytes_must_match_manifest_digest() -> None:
    with pytest.raises(SupplyChainError) as raised:
        _verify(_policy(), _attestation(), artifact=b"tampered")

    assert raised.value.code == "artifact_digest_mismatch"


def test_development_can_allow_unsigned_but_production_fails_closed() -> None:
    development = _verify(_policy(environment="development"), None)
    assert not development.signed

    with pytest.raises(SupplyChainError) as raised:
        _verify(_policy(environment="production"), None)
    assert raised.value.code == "artifact_signature_required"


@pytest.mark.parametrize(
    ("attestation", "policy", "code"),
    [
        (
            replace(_attestation(), algorithm="unknown"),
            _policy(),
            "unsupported_signature_algorithm",
        ),
        (replace(_attestation(), key_id="missing"), _policy(), "unknown_signing_key"),
        (replace(_attestation(), signature="00" * 32), _policy(), "invalid_artifact_signature"),
        (
            _attestation(),
            _policy(revoked_key_ids=frozenset({"publisher-key-1"})),
            "signing_authority_revoked",
        ),
        (
            _attestation(),
            _policy(revoked_publishers=frozenset({"trusted-publisher"})),
            "signing_authority_revoked",
        ),
        (replace(_attestation(), expires_at=500), _policy(), "attestation_expired"),
        (
            _attestation(_key(expires_at=400)),
            _policy(key=_key(expires_at=400)),
            "signing_key_expired",
        ),
    ],
)
def test_key_algorithm_revocation_and_expiry_fail_closed(
    attestation: ArtifactAttestation,
    policy: SupplyChainPolicy,
    code: str,
) -> None:
    with pytest.raises(SupplyChainError) as raised:
        _verify(policy, attestation)

    assert raised.value.code == code
    assert raised.value.details == {}
    assert str(raised.value) == "Artifact trust verification failed."


def test_publisher_and_registry_allowlists_are_enforced() -> None:
    for policy, code in (
        (
            _policy(trusted_publishers=frozenset({"someone-else"})),
            "publisher_not_trusted",
        ),
        (
            _policy(trusted_registries=frozenset({"another-registry"})),
            "registry_not_trusted",
        ),
    ):
        with pytest.raises(SupplyChainError) as raised:
            _verify(policy, _attestation())
        assert raised.value.code == code


def test_secret_is_excluded_from_repr_and_safe_errors() -> None:
    secret = b"do-not-leak-this-secret"
    key = _key(secret=secret)
    assert secret.decode() not in repr(key)

    with pytest.raises(SupplyChainError) as raised:
        _verify(_policy(key=key), replace(_attestation(key), signature="not-hex"))

    serialized_error = repr(raised.value.as_dict())
    assert secret.decode() not in serialized_error
    assert "not-hex" not in serialized_error
