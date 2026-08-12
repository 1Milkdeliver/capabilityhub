"""Local control-plane authentication with server-bound identities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock

from capabilityhub.errors import CapabilityHubError, ErrorCategory


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    tenant_id: str
    principal_id: str
    source: str
    session_id: str

    def __post_init__(self) -> None:
        for value in (self.tenant_id, self.principal_id, self.source, self.session_id):
            if not value or len(value) > 256 or any(character.isspace() for character in value):
                raise ValueError("authentication identity fields must be safe identifiers")


@dataclass(frozen=True, slots=True)
class SessionCredential:
    token: str = field(repr=False)
    identity: AuthIdentity


class LoopbackAuthenticator:
    """Authenticate reusable local sessions and optional one-time signed requests."""

    def __init__(
        self,
        identity: AuthIdentity,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.identity = identity
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._session_digest: bytes | None = None
        self._consumed_nonces: set[str] = set()
        self._lock = RLock()

    def start_session(self) -> SessionCredential:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).digest()
        with self._lock:
            if self._session_digest is not None:
                raise RuntimeError("authentication session is already active")
            self._session_digest = digest
            self._consumed_nonces.clear()
        return SessionCredential(token, self.identity)

    def close(self) -> None:
        with self._lock:
            self._session_digest = None
            self._consumed_nonces.clear()
            self._key = secrets.token_bytes(32)

    def issue_one_time(self, *, ttl_seconds: int = 60) -> str:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        now = self._now()
        with self._lock:
            if self._session_digest is None:
                raise RuntimeError("authentication session is not active")
        payload = {
            "exp": int(now) + ttl_seconds,
            "nonce": secrets.token_urlsafe(18),
            "principal": self.identity.principal_id,
            "session": self.identity.session_id,
            "source": self.identity.source,
            "tenant": self.identity.tenant_id,
        }
        encoded = _encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        signature = _encode(hmac.digest(self._key, encoded.encode(), "sha256"))
        return f"chs1.{encoded}.{signature}"

    def authenticate(self, authorization: str | None) -> AuthIdentity:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise _unauthorized()
        supplied = authorization[7:]
        if supplied.startswith("chs1."):
            return self._authenticate_signed(supplied)
        supplied_digest = hashlib.sha256(supplied.encode()).digest()
        with self._lock:
            expected = self._session_digest or bytes(hashlib.sha256().digest_size)
        if not hmac.compare_digest(supplied_digest, expected):
            raise _unauthorized()
        return self.identity

    def _authenticate_signed(self, token: str) -> AuthIdentity:
        try:
            prefix, encoded, supplied_signature = token.split(".")
            if prefix != "chs1":
                raise ValueError
            expected_signature = _encode(hmac.digest(self._key, encoded.encode(), "sha256"))
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError
            payload = json.loads(_decode(encoded))
            if not isinstance(payload, dict):
                raise ValueError
            identity = AuthIdentity(
                payload["tenant"],
                payload["principal"],
                payload["source"],
                payload["session"],
            )
            expiry = payload["exp"]
            nonce = payload["nonce"]
            if (
                identity != self.identity
                or isinstance(expiry, bool)
                or not isinstance(expiry, int)
                or not isinstance(nonce, str)
                or not nonce
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise _unauthorized() from error
        if self._now() >= expiry:
            raise _auth_error("authentication_expired")
        with self._lock:
            if self._session_digest is None:
                raise _unauthorized()
            if nonce in self._consumed_nonces:
                raise _auth_error("authentication_replayed")
            self._consumed_nonces.add(nonce)
        return identity

    def _now(self) -> float:
        value = self._clock()
        if not math.isfinite(value) or value < 0:
            raise ValueError("authentication clock must return a finite non-negative timestamp")
        return value


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _unauthorized() -> CapabilityHubError:
    return _auth_error("invalid_bearer_token")


def _auth_error(code: str) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.POLICY,
        safe_message="The HTTP control credential was rejected.",
    )
