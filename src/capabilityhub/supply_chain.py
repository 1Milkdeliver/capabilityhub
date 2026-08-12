"""Fail-closed artifact provenance checks using HMAC or Ed25519 evidence.

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
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from .errors import CapabilityHubError, ErrorCategory
from .models import CapabilityManifest

if TYPE_CHECKING:
    from .supply_chain_bundle import SigstoreBundle

Environment = Literal["development", "production"]
HMAC_SHA256 = "hmac-sha256"
ED25519 = "ed25519"
_DOMAIN = b"capabilityhub-local-artifact-attestation-v1\0"
_PUBLIC_DOMAIN = b"capabilityhub-public-artifact-attestation-v1\0"


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
class TrustedEd25519Key:
    """Pinned offline Ed25519 authority; raw public bytes are never secret."""

    key_id: str
    publisher: str
    registry: str
    public_key: bytes
    issuer: str | None = None
    subject: str | None = None
    not_before: int = 0
    expires_at: int | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        if not self.key_id or not self.publisher or not self.registry:
            raise ValueError("key_id, publisher, and registry must be non-empty")
        if not isinstance(self.public_key, bytes) or len(self.public_key) != 32:
            raise ValueError("Ed25519 public_key must contain 32 raw bytes")
        if (self.issuer is None) != (self.subject is None):
            raise ValueError("issuer and subject must be supplied together")
        if self.expires_at is not None and self.expires_at <= self.not_before:
            raise ValueError("key expiry must be after not_before")


@dataclass(frozen=True, slots=True)
class SupplyChainPolicy:
    environment: Environment
    trusted_publishers: frozenset[str]
    trusted_registries: frozenset[str]
    keys: Mapping[str, TrustedHMACKey] = field(default_factory=dict)
    ed25519_keys: Mapping[str, TrustedEd25519Key] = field(default_factory=dict)
    revoked_key_ids: frozenset[str] = frozenset()
    revoked_publishers: frozenset[str] = frozenset()
    allow_unsigned_development: bool = True
    trusted_certificate_issuers: frozenset[str] = frozenset()
    trusted_certificate_subjects: frozenset[str] = frozenset()
    require_transparency: bool = False

    def __post_init__(self) -> None:
        if self.environment not in ("development", "production"):
            raise ValueError("environment must be development or production")
        if not self.trusted_publishers or not self.trusted_registries:
            raise ValueError("trusted publisher and registry policies must be non-empty")
        normalized = dict(self.keys)
        if any(key_id != key.key_id for key_id, key in normalized.items()):
            raise ValueError("key mapping identifiers must match key_id")
        object.__setattr__(self, "keys", MappingProxyType(normalized))
        public_keys = dict(self.ed25519_keys)
        if any(key_id != key.key_id for key_id, key in public_keys.items()):
            raise ValueError("Ed25519 key mapping identifiers must match key_id")
        if set(normalized) & set(public_keys):
            raise ValueError("signing key identifiers must be unique across algorithms")
        object.__setattr__(self, "ed25519_keys", MappingProxyType(public_keys))
        object.__setattr__(self, "trusted_publishers", frozenset(self.trusted_publishers))
        object.__setattr__(self, "trusted_registries", frozenset(self.trusted_registries))
        object.__setattr__(self, "revoked_key_ids", frozenset(self.revoked_key_ids))
        object.__setattr__(self, "revoked_publishers", frozenset(self.revoked_publishers))
        object.__setattr__(
            self, "trusted_certificate_issuers", frozenset(self.trusted_certificate_issuers)
        )
        object.__setattr__(
            self, "trusted_certificate_subjects", frozenset(self.trusted_certificate_subjects)
        )


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
    certificate_issuer: str | None = None
    certificate_subject: str | None = None
    transparency_log_id: str | None = None
    transparency_entry_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactMaterial:
    """Locally acquired bytes plus provenance claims supplied to a verifier."""

    artifact: bytes = field(repr=False)
    publisher: str
    registry: str
    attestation: ArtifactAttestation | None = None
    bundle: SigstoreBundle | None = None

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


def create_ed25519_attestation(
    *,
    revision: str,
    artifact_digest: str,
    key: TrustedEd25519Key,
    private_key: object,
    issued_at: int,
    expires_at: int,
    transparency_log_id: str | None = None,
    transparency_entry_digest: str | None = None,
) -> ArtifactAttestation:
    """Create test/offline evidence using cryptography's Ed25519 implementation."""

    if expires_at <= issued_at:
        raise ValueError("attestation expiry must be after issuance")
    unsigned = ArtifactAttestation(
        revision,
        artifact_digest,
        key.publisher,
        key.registry,
        key.key_id,
        ED25519,
        issued_at,
        expires_at,
        "",
        key.issuer,
        key.subject,
        transparency_log_id,
        transparency_entry_digest,
    )
    sign = getattr(private_key, "sign", None)
    if not callable(sign):
        raise TypeError("private_key must be a cryptography Ed25519 private key")
    signature = sign(_PUBLIC_DOMAIN + _payload(unsigned))
    if not isinstance(signature, bytes):
        raise TypeError("Ed25519 signer returned invalid signature bytes")
    return ArtifactAttestation(
        unsigned.revision,
        unsigned.artifact_digest,
        unsigned.publisher,
        unsigned.registry,
        unsigned.key_id,
        unsigned.algorithm,
        unsigned.issued_at,
        unsigned.expires_at,
        _b64(signature),
        unsigned.certificate_issuer,
        unsigned.certificate_subject,
        unsigned.transparency_log_id,
        unsigned.transparency_entry_digest,
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
        bundle: object | None = None,
        now: int | None = None,
    ) -> TrustEvidence:
        """Return compact evidence or reject without exposing supplied trust material."""

        if bundle is not None:
            raise SupplyChainError("unsupported_transparency_bundle")
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
        if attestation.algorithm == ED25519:
            return self._verify_ed25519(
                manifest,
                attestation,
                digest=actual_digest,
                publisher=publisher,
                registry=registry,
                timestamp=timestamp,
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

    def _verify_ed25519(
        self,
        manifest: CapabilityManifest,
        attestation: ArtifactAttestation,
        *,
        digest: str,
        publisher: str,
        registry: str,
        timestamp: int,
    ) -> TrustEvidence:
        key = self.policy.ed25519_keys.get(attestation.key_id)
        if key is None:
            raise SupplyChainError("unknown_signing_key")
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
        self._verify_certificate_identity(key, attestation)
        self._verify_transparency(attestation)
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError as error:
            raise SupplyChainError("public_key_verifier_unavailable") from error
        try:
            signature = _unb64(attestation.signature)
            Ed25519PublicKey.from_public_bytes(key.public_key).verify(
                signature, _PUBLIC_DOMAIN + _payload(attestation)
            )
        except (InvalidSignature, TypeError, ValueError) as error:
            raise SupplyChainError("invalid_artifact_signature") from error
        return TrustEvidence(
            manifest.identity.revision,
            digest,
            publisher,
            registry,
            self.policy.environment,
            True,
            ED25519,
            attestation.key_id,
            attestation.expires_at,
        )

    def _verify_certificate_identity(
        self, key: TrustedEd25519Key, attestation: ArtifactAttestation
    ) -> None:
        supplied = (attestation.certificate_issuer, attestation.certificate_subject)
        if (supplied[0] is None) != (supplied[1] is None):
            raise SupplyChainError("invalid_certificate_identity")
        if key.issuer is None:
            if any(value is not None for value in supplied):
                raise SupplyChainError("certificate_identity_mismatch")
            return
        if supplied != (key.issuer, key.subject):
            raise SupplyChainError("certificate_identity_mismatch")
        if (
            key.issuer not in self.policy.trusted_certificate_issuers
            or key.subject not in self.policy.trusted_certificate_subjects
        ):
            raise SupplyChainError("certificate_identity_not_trusted")

    def _verify_transparency(self, attestation: ArtifactAttestation) -> None:
        supplied = (attestation.transparency_log_id, attestation.transparency_entry_digest)
        if self.policy.require_transparency and any(value is None for value in supplied):
            raise SupplyChainError("transparency_evidence_required")
        if any(value is not None for value in supplied):
            if not all(isinstance(value, str) and value for value in supplied):
                raise SupplyChainError("invalid_transparency_evidence")
            digest = attestation.transparency_entry_digest or ""
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise SupplyChainError("invalid_transparency_evidence")

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
            "certificate_issuer": attestation.certificate_issuer,
            "certificate_subject": attestation.certificate_subject,
            "expires_at": attestation.expires_at,
            "issued_at": attestation.issued_at,
            "key_id": attestation.key_id,
            "publisher": attestation.publisher,
            "registry": attestation.registry,
            "revision": attestation.revision,
            "transparency_entry_digest": attestation.transparency_entry_digest,
            "transparency_log_id": attestation.transparency_log_id,
            "version": 1,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _b64(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded
