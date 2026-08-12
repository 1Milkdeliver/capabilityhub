"""Offline-reproducible certificate and transparency bundle verification.

The module deliberately supports a small, explicit profile: Ed25519 certificate
chains and RFC6962-style SHA-256 inclusion proofs.  All signature operations are
delegated to ``cryptography``; this code only binds CapabilityHub fields and
applies fail-closed policy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from base64 import urlsafe_b64decode
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

from .models import CapabilityManifest
from .supply_chain import ED25519, SupplyChainError, TrustEvidence

if TYPE_CHECKING:
    from cryptography.x509 import Certificate

_ARTIFACT_DOMAIN = b"capabilityhub-sigstore-bundle-artifact-v1\0"
_CHECKPOINT_DOMAIN = b"capabilityhub-transparency-checkpoint-v1\0"


@dataclass(frozen=True, slots=True)
class TrustedCertificateRoot:
    root_id: str
    certificate_pem: bytes = field(repr=False)
    publisher: str
    registry: str
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class TrustedTransparencyLog:
    log_id: str
    public_key: bytes
    not_before: int = 0
    expires_at: int | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        if not self.log_id or len(self.public_key) != 32:
            raise ValueError("log id and 32-byte Ed25519 public key are required")


@dataclass(frozen=True, slots=True)
class SignedCheckpoint:
    log_id: str
    tree_size: int
    root_hash: str
    timestamp: int
    signature: str


@dataclass(frozen=True, slots=True)
class TransparencyInclusionProof:
    checkpoint: SignedCheckpoint
    leaf_index: int
    hashes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SigstoreBundle:
    """Portable evidence; leaf certificate is first and root is last."""

    certificate_chain_pem: tuple[bytes, ...] = field(repr=False)
    artifact_signature: str
    issued_at: int
    inclusion_proof: TransparencyInclusionProof


@dataclass(frozen=True, slots=True)
class BundleTrustPolicy:
    trusted_publishers: frozenset[str]
    trusted_registries: frozenset[str]
    roots: Mapping[str, TrustedCertificateRoot]
    logs: Mapping[str, TrustedTransparencyLog]
    trusted_certificate_issuers: frozenset[str]
    trusted_certificate_subjects: frozenset[str]
    require_online_checkpoint: bool = False
    max_checkpoint_age_seconds: int = 3600

    def __post_init__(self) -> None:
        if not self.trusted_publishers or not self.trusted_registries:
            raise ValueError("publisher and registry trust policies are required")
        if not self.roots or not self.logs:
            raise ValueError("certificate roots and transparency log keys are required")
        if self.max_checkpoint_age_seconds <= 0:
            raise ValueError("checkpoint freshness must be positive")
        object.__setattr__(self, "roots", MappingProxyType(dict(self.roots)))
        object.__setattr__(self, "logs", MappingProxyType(dict(self.logs)))


class CheckpointObserver:
    """Detect rollback/replay and same-size forks without rejecting normal reuse."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[int, str, int]] = {}

    def observe(self, checkpoint: SignedCheckpoint) -> None:
        current = (checkpoint.tree_size, checkpoint.root_hash, checkpoint.timestamp)
        with self._lock:
            previous = self._seen.get(checkpoint.log_id)
            if previous is not None:
                if current[0] < previous[0] or current[2] < previous[2]:
                    raise SupplyChainError("transparency_checkpoint_replayed")
                if current[0] == previous[0] and current[1] != previous[1]:
                    raise SupplyChainError("transparency_log_fork")
            self._seen[checkpoint.log_id] = current


OnlineCheckpoint = Callable[[str], SignedCheckpoint]


class SigstoreBundleVerifier:
    """Verify artifact, certificate chain, identity, inclusion, and checkpoint."""

    def __init__(
        self,
        policy: BundleTrustPolicy,
        *,
        clock: Callable[[], float] = time.time,
        online_checkpoint: OnlineCheckpoint | None = None,
        observer: CheckpointObserver | None = None,
    ) -> None:
        self.policy = policy
        self._clock = clock
        self._online_checkpoint = online_checkpoint
        self._observer = observer

    def verify(
        self,
        manifest: CapabilityManifest,
        artifact: bytes,
        *,
        publisher: str,
        registry: str,
        attestation: object | None = None,
        bundle: SigstoreBundle | None = None,
        now: int | None = None,
    ) -> TrustEvidence:
        if attestation is not None:
            raise SupplyChainError("unsupported_legacy_attestation")
        if bundle is None:
            raise SupplyChainError("transparency_bundle_required")
        if publisher not in self.policy.trusted_publishers:
            raise SupplyChainError("publisher_not_trusted")
        if registry not in self.policy.trusted_registries:
            raise SupplyChainError("registry_not_trusted")
        digest = "sha256:" + hashlib.sha256(artifact).hexdigest()
        if not hmac.compare_digest(digest, manifest.identity.digest):
            raise SupplyChainError("artifact_digest_mismatch")
        timestamp = int(self._clock()) if now is None else now

        leaf = self._verify_chain(bundle, publisher, registry)
        payload = _artifact_payload(manifest, digest, publisher, registry, bundle.issued_at)
        self._verify_artifact_signature(leaf, payload, bundle.artifact_signature)
        entry = _log_entry(payload, bundle.artifact_signature, leaf)
        self._verify_transparency(bundle.inclusion_proof, entry, timestamp)
        return TrustEvidence(
            manifest.identity.revision,
            digest,
            publisher,
            registry,
            "production",
            True,
            ED25519,
            _certificate_fingerprint(leaf),
            bundle.inclusion_proof.checkpoint.timestamp,
        )

    def _verify_chain(
        self, bundle: SigstoreBundle, publisher: str, registry: str
    ) -> Certificate:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError as error:
            raise SupplyChainError("public_key_verifier_unavailable") from error
        if not bundle.certificate_chain_pem or len(bundle.certificate_chain_pem) > 6:
            raise SupplyChainError("invalid_certificate_chain")
        try:
            chain = [x509.load_pem_x509_certificate(item) for item in bundle.certificate_chain_pem]
        except (TypeError, ValueError) as error:
            raise SupplyChainError("invalid_certificate_chain") from error
        moment = datetime.fromtimestamp(bundle.issued_at, tz=UTC)
        for index, certificate in enumerate(chain):
            if not (certificate.not_valid_before_utc <= moment < certificate.not_valid_after_utc):
                raise SupplyChainError("certificate_expired")
            issuer = certificate if index == len(chain) - 1 else chain[index + 1]
            if certificate.issuer != issuer.subject:
                raise SupplyChainError("invalid_certificate_chain")
            key = issuer.public_key()
            if not isinstance(key, Ed25519PublicKey):
                raise SupplyChainError("unsupported_certificate_key")
            try:
                key.verify(certificate.signature, certificate.tbs_certificate_bytes)
            except Exception as error:
                raise SupplyChainError("invalid_certificate_chain") from error
            if index > 0:
                try:
                    constraints = certificate.extensions.get_extension_for_class(
                        x509.BasicConstraints
                    ).value
                except x509.ExtensionNotFound as error:
                    raise SupplyChainError("invalid_certificate_chain") from error
                if not constraints.ca:
                    raise SupplyChainError("invalid_certificate_chain")
        root = chain[-1]
        root_id = _certificate_fingerprint(root)
        authority = self.policy.roots.get(root_id)
        if authority is None or authority.revoked:
            raise SupplyChainError("certificate_root_not_trusted")
        if authority.publisher != publisher or authority.registry != registry:
            raise SupplyChainError("certificate_root_scope_mismatch")
        try:
            configured = x509.load_pem_x509_certificate(authority.certificate_pem)
        except ValueError as error:
            raise SupplyChainError("invalid_trust_root") from error
        from cryptography.hazmat.primitives.serialization import Encoding

        if not hmac.compare_digest(
            configured.public_bytes(Encoding.DER), root.public_bytes(Encoding.DER)
        ):
            raise SupplyChainError("certificate_root_not_trusted")
        self._verify_identity(chain[0])
        return chain[0]

    def _verify_identity(self, leaf: Certificate) -> None:
        from cryptography import x509

        issuer = leaf.issuer.rfc4514_string()
        try:
            san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            subjects = {
                *san.get_values_for_type(x509.RFC822Name),
                *san.get_values_for_type(x509.DNSName),
                *san.get_values_for_type(x509.UniformResourceIdentifier),
            }
        except x509.ExtensionNotFound as error:
            raise SupplyChainError("certificate_identity_not_trusted") from error
        if issuer not in self.policy.trusted_certificate_issuers:
            raise SupplyChainError("certificate_identity_not_trusted")
        if not subjects.intersection(self.policy.trusted_certificate_subjects):
            raise SupplyChainError("certificate_identity_not_trusted")

    @staticmethod
    def _verify_artifact_signature(
        leaf: Certificate, payload: bytes, encoded: str
    ) -> None:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError as error:
            raise SupplyChainError("public_key_verifier_unavailable") from error
        key = leaf.public_key()
        if not isinstance(key, Ed25519PublicKey):
            raise SupplyChainError("unsupported_certificate_key")
        try:
            key.verify(_decode(encoded), _ARTIFACT_DOMAIN + payload)
        except (InvalidSignature, TypeError, ValueError) as error:
            raise SupplyChainError("invalid_artifact_signature") from error

    def _verify_transparency(
        self, proof: TransparencyInclusionProof, entry: bytes, now: int
    ) -> None:
        checkpoint = proof.checkpoint
        log = self.policy.logs.get(checkpoint.log_id)
        if log is None or log.revoked:
            raise SupplyChainError("transparency_log_not_trusted")
        if checkpoint.timestamp < log.not_before or (
            log.expires_at is not None and checkpoint.timestamp >= log.expires_at
        ):
            raise SupplyChainError("transparency_log_key_expired")
        if checkpoint.timestamp > now + 60:
            raise SupplyChainError("transparency_checkpoint_not_yet_valid")
        self._verify_checkpoint_signature(log, checkpoint)
        calculated = _inclusion_root(entry, proof.leaf_index, checkpoint.tree_size, proof.hashes)
        if not hmac.compare_digest(calculated, checkpoint.root_hash):
            raise SupplyChainError("invalid_transparency_inclusion_proof")
        if self._observer is not None:
            self._observer.observe(checkpoint)
        if self.policy.require_online_checkpoint:
            if self._online_checkpoint is None:
                raise SupplyChainError("online_transparency_check_required")
            if now - checkpoint.timestamp > self.policy.max_checkpoint_age_seconds:
                raise SupplyChainError("transparency_checkpoint_stale")
            try:
                current = self._online_checkpoint(checkpoint.log_id)
            except Exception as error:
                raise SupplyChainError("online_transparency_check_failed") from error
            self._verify_checkpoint_signature(log, current)
            if current.tree_size != checkpoint.tree_size or not hmac.compare_digest(
                current.root_hash, checkpoint.root_hash
            ):
                raise SupplyChainError("transparency_log_fork")

    @staticmethod
    def _verify_checkpoint_signature(
        log: TrustedTransparencyLog, checkpoint: SignedCheckpoint
    ) -> None:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            key = Ed25519PublicKey.from_public_bytes(log.public_key)
            key.verify(
                _decode(checkpoint.signature),
                _CHECKPOINT_DOMAIN + _checkpoint_payload(checkpoint),
            )
        except ImportError as error:
            raise SupplyChainError("public_key_verifier_unavailable") from error
        except (InvalidSignature, TypeError, ValueError) as error:
            raise SupplyChainError("invalid_transparency_checkpoint") from error


def _artifact_payload(
    manifest: CapabilityManifest,
    digest: str,
    publisher: str,
    registry: str,
    issued_at: int,
) -> bytes:
    return _canonical(
        {
            "artifact_digest": digest,
            "issued_at": issued_at,
            "publisher": publisher,
            "registry": registry,
            "revision": manifest.identity.revision,
            "version": 1,
        }
    )


def _log_entry(payload: bytes, signature: str, leaf: Certificate) -> bytes:
    return _canonical(
        {
            "artifact_payload": payload.decode("utf-8"),
            "artifact_signature": signature,
            "certificate_fingerprint": _certificate_fingerprint(leaf),
            "version": 1,
        }
    )


def _checkpoint_payload(checkpoint: SignedCheckpoint) -> bytes:
    return _canonical(
        {
            "log_id": checkpoint.log_id,
            "root_hash": checkpoint.root_hash,
            "timestamp": checkpoint.timestamp,
            "tree_size": checkpoint.tree_size,
            "version": 1,
        }
    )


def _inclusion_root(
    entry: bytes, leaf_index: int, tree_size: int, hashes: Sequence[str]
) -> str:
    if tree_size <= 0 or leaf_index < 0 or leaf_index >= tree_size or len(hashes) > 64:
        raise SupplyChainError("invalid_transparency_inclusion_proof")
    node = hashlib.sha256(b"\x00" + entry).digest()
    index = leaf_index
    last = tree_size - 1
    for encoded in hashes:
        try:
            sibling = bytes.fromhex(encoded)
        except ValueError as error:
            raise SupplyChainError("invalid_transparency_inclusion_proof") from error
        if len(sibling) != 32:
            raise SupplyChainError("invalid_transparency_inclusion_proof")
        if index & 1 or index == last:
            node = hashlib.sha256(b"\x01" + sibling + node).digest()
        else:
            node = hashlib.sha256(b"\x01" + node + sibling).digest()
        index //= 2
        last //= 2
    if last != 0:
        raise SupplyChainError("invalid_transparency_inclusion_proof")
    return node.hex()


def _certificate_fingerprint(certificate: Certificate) -> str:
    from cryptography.hazmat.primitives import hashes

    return str(certificate.fingerprint(hashes.SHA256()).hex())


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode(value: str) -> bytes:
    decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    return bytes(decoded)
