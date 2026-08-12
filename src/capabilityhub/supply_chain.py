"""Fail-closed artifact provenance checks using local HMAC evidence.

HMAC-SHA256 is intentionally limited to deployments where the verifier and trusted
publisher share a local secret. It provides integrity and key-id rotation evidence,
not public publisher identity or third-party non-repudiation. A future public supply
chain profile should use a standard Ed25519/Sigstore implementation instead.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from .errors import CapabilityHubError, ErrorCategory
from .models import CapabilityManifest

Environment = Literal["development", "production"]
HMAC_SHA256 = "hmac-sha256"
_DOMAIN = b"capabilityhub-local-artifact-attestation-v1\0"


class SupplyChainError(CapabilityHubError):
    """A model-safe trust rejection with no attacker-controlled details."""

    def __init__(self, code: str) -> None:
        super().__init__(
            code=code,
            category=ErrorCategory.POLICY,
            safe_message="Artifact trust verification failed.",
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class TrustedHMACKey:
    key_id: str
    publisher: str
    registry: str
    secret: bytes = field(repr=False)
    algorithm: str = HMAC_SHA256
    not_before: int = 0
    expires_at: int | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        if not self.key_id or not self.publisher or not self.registry:
            raise ValueError("key_id, publisher, and registry must be non-empty")
        if not isinstance(self.secret, bytes) or not self.secret:
            raise ValueError("HMAC secret must be non-empty bytes")
        if self.algorithm != HMAC_SHA256:
            raise ValueError("trusted key algorithm must be hmac-sha256")
        if self.expires_at is not None and self.expires_at <= self.not_before:
            raise ValueError("key expiry must be after not_before")


@dataclass(frozen=True, slots=True)
class SupplyChainPolicy:
    environment: Environment
    trusted_publishers: frozenset[str]
    trusted_registries: frozenset[str]
    keys: Mapping[str, TrustedHMACKey] = field(default_factory=dict)
    revoked_key_ids: frozenset[str] = frozenset()
    revoked_publishers: frozenset[str] = frozenset()
    allow_unsigned_development: bool = True

    def __post_init__(self) -> None:
        if self.environment not in ("development", "production"):
            raise ValueError("environment must be development or production")
        if not self.trusted_publishers or not self.trusted_registries:
            raise ValueError("trusted publisher and registry policies must be non-empty")
        normalized = dict(self.keys)
        if any(key_id != key.key_id for key_id, key in normalized.items()):
            raise ValueError("key mapping identifiers must match key_id")
        object.__setattr__(self, "keys", MappingProxyType(normalized))
        object.__setattr__(self, "trusted_publishers", frozenset(self.trusted_publishers))
        object.__setattr__(self, "trusted_registries", frozenset(self.trusted_registries))
        object.__setattr__(self, "revoked_key_ids", frozenset(self.revoked_key_ids))
        object.__setattr__(self, "revoked_publishers", frozenset(self.revoked_publishers))


@dataclass(frozen=True, slots=True)
class ArtifactAttestation:
    revision: str
    artifact_digest: str
    publisher: str
    registry: str
    key_id: str
    algorithm: str
    issued_at: int
    expires_at: int
    signature: str


@dataclass(frozen=True, slots=True)
class ArtifactMaterial:
    """Locally acquired bytes plus provenance claims supplied to a verifier."""

    artifact: bytes = field(repr=False)
    publisher: str
    registry: str
    attestation: ArtifactAttestation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, bytes):
            raise TypeError("artifact must be bytes")
        if not self.publisher or not self.registry:
            raise ValueError("publisher and registry must be non-empty")


@dataclass(frozen=True, slots=True)
class TrustEvidence:
    revision: str
    artifact_digest: str
    publisher: str
    registry: str
    environment: Environment
    signed: bool
    algorithm: str | None
    key_id: str | None
    expires_at: int | None


def create_local_hmac_attestation(
    *,
    revision: str,
    artifact_digest: str,
    key: TrustedHMACKey,
    issued_at: int,
    expires_at: int,
) -> ArtifactAttestation:
    """Create local shared-secret evidence; this is not a public-key signature."""

    if not revision or not artifact_digest:
        raise ValueError("revision and artifact_digest must be non-empty")
    if expires_at <= issued_at:
        raise ValueError("attestation expiry must be after issuance")
    unsigned = ArtifactAttestation(
        revision=revision,
        artifact_digest=artifact_digest,
        publisher=key.publisher,
        registry=key.registry,
        key_id=key.key_id,
        algorithm=key.algorithm,
        issued_at=issued_at,
        expires_at=expires_at,
        signature="",
    )
    signature = hmac.new(key.secret, _DOMAIN + _payload(unsigned), hashlib.sha256).hexdigest()
    return ArtifactAttestation(
        revision=unsigned.revision,
        artifact_digest=unsigned.artifact_digest,
        publisher=unsigned.publisher,
        registry=unsigned.registry,
        key_id=unsigned.key_id,
        algorithm=unsigned.algorithm,
        issued_at=unsigned.issued_at,
        expires_at=unsigned.expires_at,
        signature=signature,
    )


class SupplyChainVerifier:
    """Verify content digest, provenance policy, validity, revocation, and HMAC."""

    def __init__(
        self,
        policy: SupplyChainPolicy,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy
        self._clock = clock

    def verify(
        self,
        manifest: CapabilityManifest,
        artifact: bytes,
        *,
        publisher: str,
        registry: str,
        attestation: ArtifactAttestation | None,
        now: int | None = None,
    ) -> TrustEvidence:
        """Return compact evidence or reject without exposing supplied trust material."""

        if not isinstance(artifact, bytes):
            raise TypeError("artifact must be bytes")
        actual_digest = "sha256:" + hashlib.sha256(artifact).hexdigest()
        if not hmac.compare_digest(actual_digest, manifest.identity.digest):
            raise SupplyChainError("artifact_digest_mismatch")
        self._verify_source_policy(publisher, registry)
        timestamp = int(self._clock()) if now is None else now

        if attestation is None:
            if (
                self.policy.environment == "production"
                or not self.policy.allow_unsigned_development
            ):
                raise SupplyChainError("artifact_signature_required")
            return TrustEvidence(
                revision=manifest.identity.revision,
                artifact_digest=actual_digest,
                publisher=publisher,
                registry=registry,
                environment=self.policy.environment,
                signed=False,
                algorithm=None,
                key_id=None,
                expires_at=None,
            )

        self._verify_attestation_bindings(
            attestation,
            revision=manifest.identity.revision,
            digest=actual_digest,
            publisher=publisher,
            registry=registry,
        )
        if attestation.algorithm != HMAC_SHA256:
            raise SupplyChainError("unsupported_signature_algorithm")
        key = self.policy.keys.get(attestation.key_id)
        if key is None:
            raise SupplyChainError("unknown_signing_key")
        if key.algorithm != attestation.algorithm:
            raise SupplyChainError("signature_algorithm_mismatch")
        if key.publisher != publisher or key.registry != registry:
            raise SupplyChainError("signing_key_scope_mismatch")
        if (
            key.revoked
            or key.key_id in self.policy.revoked_key_ids
            or publisher in self.policy.revoked_publishers
        ):
            raise SupplyChainError("signing_authority_revoked")
        if timestamp >= attestation.expires_at:
            raise SupplyChainError("attestation_expired")
        if attestation.issued_at > timestamp + 60:
            raise SupplyChainError("attestation_not_yet_valid")
        if attestation.issued_at < key.not_before or (
            key.expires_at is not None
            and (attestation.issued_at >= key.expires_at or timestamp >= key.expires_at)
        ):
            raise SupplyChainError("signing_key_expired")
        try:
            supplied_signature = bytes.fromhex(attestation.signature)
        except ValueError as error:
            raise SupplyChainError("invalid_artifact_signature") from error
        expected_signature = hmac.new(
            key.secret,
            _DOMAIN + _payload(attestation),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise SupplyChainError("invalid_artifact_signature")
        return TrustEvidence(
            revision=manifest.identity.revision,
            artifact_digest=actual_digest,
            publisher=publisher,
            registry=registry,
            environment=self.policy.environment,
            signed=True,
            algorithm=attestation.algorithm,
            key_id=attestation.key_id,
            expires_at=attestation.expires_at,
        )

    def _verify_source_policy(self, publisher: str, registry: str) -> None:
        if not publisher or publisher not in self.policy.trusted_publishers:
            raise SupplyChainError("publisher_not_trusted")
        if not registry or registry not in self.policy.trusted_registries:
            raise SupplyChainError("registry_not_trusted")
        if publisher in self.policy.revoked_publishers:
            raise SupplyChainError("signing_authority_revoked")

    @staticmethod
    def _verify_attestation_bindings(
        attestation: ArtifactAttestation,
        *,
        revision: str,
        digest: str,
        publisher: str,
        registry: str,
    ) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                attestation.revision,
                attestation.artifact_digest,
                attestation.publisher,
                attestation.registry,
                attestation.key_id,
                attestation.algorithm,
                attestation.signature,
            )
        ):
            raise SupplyChainError("invalid_attestation")
        if (
            attestation.revision != revision
            or not hmac.compare_digest(attestation.artifact_digest, digest)
            or attestation.publisher != publisher
            or attestation.registry != registry
        ):
            raise SupplyChainError("attestation_binding_mismatch")
        if (
            isinstance(attestation.issued_at, bool)
            or not isinstance(attestation.issued_at, int)
            or isinstance(attestation.expires_at, bool)
            or not isinstance(attestation.expires_at, int)
            or attestation.expires_at <= attestation.issued_at
        ):
            raise SupplyChainError("invalid_attestation")


def _payload(attestation: ArtifactAttestation) -> bytes:
    return json.dumps(
        {
            "algorithm": attestation.algorithm,
            "artifact_digest": attestation.artifact_digest,
            "expires_at": attestation.expires_at,
            "issued_at": attestation.issued_at,
            "key_id": attestation.key_id,
            "publisher": attestation.publisher,
            "registry": attestation.registry,
            "revision": attestation.revision,
            "version": 1,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
