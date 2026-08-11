"""HMAC-authenticated, revision-bound opaque capability references."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from .errors import CapabilityHubError, ErrorCategory

_PREFIX = "chref1"
_DOMAIN = b"capabilityhub-reference-v1\0"


class InvalidReference(CapabilityHubError):
    """A reference is malformed, unauthentic, or invalid for this request."""

    def __init__(self, code: str = "invalid_reference", message: str | None = None) -> None:
        super().__init__(
            code=code,
            category=ErrorCategory.REFERENCE,
            safe_message=message or "The capability reference is invalid.",
            retryable=False,
        )


class ExpiredReference(InvalidReference):
    """A validly signed reference has passed its expiry."""

    def __init__(self) -> None:
        super().__init__(
            code="reference_expired",
            message="The capability reference has expired; load the capability again.",
        )


@dataclass(frozen=True, slots=True)
class ReferenceClaims:
    """Authenticated claims carried by a reference."""

    revision: str
    scope: str
    purpose: str
    issued_at: int
    expires_at: int


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise InvalidReference() from error


class ReferenceSigner:
    """Issues and verifies self-contained capability references.

    The payload is encoded, not encrypted. It contains only non-secret identifiers.
    Authenticity and binding are provided by HMAC-SHA256.
    """

    def __init__(
        self,
        secret: bytes | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        key = secret.encode("utf-8") if isinstance(secret, str) else secret
        if not isinstance(key, bytes) or not key:
            raise ValueError("secret must be non-empty bytes or text")
        self._key = key
        self._clock = clock

    def issue(
        self,
        *,
        revision: str,
        scope: str,
        ttl_seconds: int,
        purpose: str = "execution",
        now: float | None = None,
    ) -> str:
        """Issue a reference bound to one revision, caller scope, and purpose."""

        if not revision or not scope or not purpose:
            raise ValueError("revision, scope, and purpose must be non-empty")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ValueError("ttl_seconds must be a positive integer")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")

        issued_at = int(self._clock() if now is None else now)
        payload = {
            "exp": issued_at + ttl_seconds,
            "iat": issued_at,
            "purpose": purpose,
            "revision": revision,
            "scope": scope,
            "v": 1,
        }
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        encoded = _encode(serialized)
        signature = self._signature(encoded)
        return f"{_PREFIX}.{encoded}.{_encode(signature)}"

    def verify(
        self,
        reference: str,
        *,
        expected_scope: str,
        expected_revision: str | None = None,
        expected_purpose: str = "execution",
        now: float | None = None,
    ) -> ReferenceClaims:
        """Authenticate and validate all caller-provided bindings."""

        if not isinstance(expected_scope, str) or not expected_scope:
            raise ValueError("expected_scope must be non-empty")
        if not isinstance(expected_purpose, str) or not expected_purpose:
            raise ValueError("expected_purpose must be non-empty")
        if not isinstance(reference, str):
            raise InvalidReference()
        parts = reference.split(".")
        if len(parts) != 3 or parts[0] != _PREFIX:
            raise InvalidReference()
        encoded, encoded_signature = parts[1], parts[2]
        supplied_signature = _decode(encoded_signature)
        expected_signature = self._signature(encoded)
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidReference(code="reference_tampered")

        claims = self._parse_claims(_decode(encoded))
        timestamp = int(self._clock() if now is None else now)
        if timestamp >= claims.expires_at:
            raise ExpiredReference()
        # References dated far in the future are invalid even with a valid signature;
        # this protects key-sharing deployments from clock/configuration mistakes.
        if claims.issued_at > timestamp + 60:
            raise InvalidReference(code="reference_not_yet_valid")
        if not hmac.compare_digest(claims.scope, expected_scope):
            raise InvalidReference(code="reference_scope_mismatch")
        if not hmac.compare_digest(claims.purpose, expected_purpose):
            raise InvalidReference(code="reference_purpose_mismatch")
        if expected_revision is not None and not hmac.compare_digest(
            claims.revision, expected_revision
        ):
            raise InvalidReference(code="reference_revision_mismatch")
        return claims

    resolve = verify

    def _signature(self, encoded_payload: str) -> bytes:
        return hmac.new(
            self._key,
            _DOMAIN + encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()

    @staticmethod
    def _parse_claims(serialized: bytes) -> ReferenceClaims:
        try:
            payload = json.loads(serialized)
            if not isinstance(payload, dict) or set(payload) != {
                "exp",
                "iat",
                "purpose",
                "revision",
                "scope",
                "v",
            }:
                raise ValueError
            if payload["v"] != 1:
                raise ValueError
            if (
                isinstance(payload["iat"], bool)
                or not isinstance(payload["iat"], int)
                or isinstance(payload["exp"], bool)
                or not isinstance(payload["exp"], int)
                or payload["exp"] <= payload["iat"]
            ):
                raise ValueError
            for key in ("purpose", "revision", "scope"):
                if not isinstance(payload[key], str) or not payload[key]:
                    raise ValueError
            return ReferenceClaims(
                revision=payload["revision"],
                scope=payload["scope"],
                purpose=payload["purpose"],
                issued_at=payload["iat"],
                expires_at=payload["exp"],
            )
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise InvalidReference() from error


# Both names describe the same narrow service; keep the manager spelling friendly to
# callers that do not need to know the signing implementation.
ReferenceManager = ReferenceSigner
