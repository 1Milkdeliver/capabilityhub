"""Persistent principal grants and immutable authorization snapshots."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from capabilityhub.auth import AuthIdentity
from capabilityhub.authorization import (
    AuthorizationDecision,
    ParameterAuthorizer,
    PermissionConstraint,
)
from capabilityhub.models import CapabilityManifest, JsonValue

_SCHEMA = """
CREATE TABLE IF NOT EXISTS principal_grant_policies (
  identity_digest TEXT PRIMARY KEY, revision INTEGER NOT NULL, document_json TEXT NOT NULL,
  document_digest TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS principal_grant_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, identity_digest TEXT NOT NULL,
  revision INTEGER NOT NULL, document_digest TEXT NOT NULL, actor_digest TEXT NOT NULL,
  actor_source TEXT NOT NULL, created_at REAL NOT NULL
);
"""


class GrantPolicyConflictError(RuntimeError):
    """CAS revision did not match the persisted policy."""


@dataclass(frozen=True, slots=True)
class PrincipalGrantSnapshot:
    revision: int
    providers: Mapping[str, Mapping[str, PermissionConstraint]]
    document_digest: str

    def authorizer(self) -> PrincipalPolicyAuthorizer:
        return PrincipalPolicyAuthorizer(self)


class PrincipalGrantPolicy:
    """SQLite CAS policy keyed by opaque tenant/principal/source identity."""

    def __init__(self, path: str | Path, *, scope_key: bytes) -> None:
        if len(scope_key) < 16:
            raise ValueError("scope_key must contain at least 16 bytes")
        self._path = Path(path).resolve()
        self._key = scope_key
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def read(self, identity: AuthIdentity) -> PrincipalGrantSnapshot:
        digest = self._identity_digest(identity)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revision, document_json, document_digest "
                "FROM principal_grant_policies WHERE identity_digest = ?",
                (digest,),
            ).fetchone()
        if row is None:
            return PrincipalGrantSnapshot(0, {}, _document_digest({}))
        document = json.loads(str(row[1]))
        return PrincipalGrantSnapshot(int(row[0]), _decode_document(document), str(row[2]))

    def set(
        self,
        *,
        actor: AuthIdentity,
        target: AuthIdentity,
        providers: Mapping[str, Mapping[str, PermissionConstraint]],
        expected_revision: int,
        now: float | None = None,
    ) -> PrincipalGrantSnapshot:
        if actor.source not in {"admin-loopback", "remote-mtls-admin"} or (
            actor.tenant_id != target.tenant_id
        ):
            raise PermissionError("grant policy mutation requires same-tenant policy admin")
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        encoded = _encode_document(providers)
        serialized = json.dumps(encoded, separators=(",", ":"), sort_keys=True)
        document_digest = _document_digest(encoded)
        target_digest = self._identity_digest(target)
        actor_digest = self._identity_digest(actor)
        timestamp = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM principal_grant_policies WHERE identity_digest = ?",
                (target_digest,),
            ).fetchone()
            current = 0 if row is None else int(row[0])
            if current != expected_revision:
                raise GrantPolicyConflictError("grant policy revision mismatch")
            revision = current + 1
            connection.execute(
                "INSERT INTO principal_grant_policies VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(identity_digest) DO UPDATE SET revision=excluded.revision, "
                "document_json=excluded.document_json, document_digest=excluded.document_digest, "
                "updated_at=excluded.updated_at",
                (target_digest, revision, serialized, document_digest, timestamp),
            )
            connection.execute(
                "INSERT INTO principal_grant_audit "
                "(identity_digest, revision, document_digest, actor_digest, "
                "actor_source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (target_digest, revision, document_digest, actor_digest, actor.source, timestamp),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return PrincipalGrantSnapshot(revision, _decode_document(encoded), document_digest)

    def authorizer(self, identity: AuthIdentity) -> PrincipalPolicyAuthorizer:
        return self.read(identity).authorizer()

    def _identity_digest(self, identity: AuthIdentity) -> str:
        payload = json.dumps(
            [identity.tenant_id, identity.principal_id, identity.source],
            separators=(",", ":"),
        ).encode()
        return hmac.new(self._key, b"grant-policy-v1\0" + payload, hashlib.sha256).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


class PrincipalPolicyAuthorizer(ParameterAuthorizer):
    """Apply one immutable policy revision to capability plus dependency constraints."""

    def __init__(self, snapshot: PrincipalGrantSnapshot) -> None:
        super().__init__({})
        self.snapshot = snapshot

    @property
    def granted_permissions(self) -> frozenset[str]:
        return frozenset(
            permission
            for grants in self.snapshot.providers.values()
            for permission in grants
        )

    def eligible(
        self, manifest: CapabilityManifest, dependencies: Iterable[CapabilityManifest] = ()
    ) -> AuthorizationDecision:
        manifests = (manifest, *dependencies)
        authorizer = self._for(manifests)
        return authorizer.eligible(manifest, manifests[1:])

    def authorize(
        self,
        manifest: CapabilityManifest,
        *,
        dependencies: Iterable[CapabilityManifest] = (),
        normalized_arguments: Mapping[str, JsonValue],
    ) -> AuthorizationDecision:
        manifests = (manifest, *dependencies)
        return self._for(manifests).authorize(
            manifest,
            dependencies=manifests[1:],
            normalized_arguments=normalized_arguments,
        )

    def _for(self, manifests: tuple[CapabilityManifest, ...]) -> ParameterAuthorizer:
        constraints: dict[str, list[PermissionConstraint]] = {}
        missing: set[str] = set()
        for manifest in manifests:
            provider = self.snapshot.providers.get(manifest.provider, {})
            for permission in manifest.permissions:
                constraint = provider.get(permission)
                if constraint is None:
                    missing.add(permission)
                else:
                    constraints.setdefault(permission, []).append(constraint)
        grants: dict[str, PermissionConstraint] = {}
        for permission, items in constraints.items():
            combined = _intersect(permission, items)
            if permission not in missing and combined is not None:
                grants[permission] = combined
        return ParameterAuthorizer(grants)


def _encode_document(
    providers: Mapping[str, Mapping[str, PermissionConstraint]],
) -> dict[str, dict[str, JsonValue]]:
    encoded: dict[str, dict[str, JsonValue]] = {}
    for provider, grants in sorted(providers.items()):
        if not provider:
            raise ValueError("provider must be non-empty")
        encoded[provider] = {}
        for permission, constraint in sorted(grants.items()):
            normalized = ParameterAuthorizer({permission: constraint})
            value = asdict(constraint)
            value["path_roots"] = [str(item) for item in value["path_roots"]]
            for key in ("hosts", "http_methods", "commands", "profiles", "secret_aliases"):
                value[key] = sorted(value[key])
            encoded[provider][permission] = value
            assert permission in normalized.granted_permissions
    return encoded


def parse_grant_document(value: JsonValue) -> dict[str, dict[str, PermissionConstraint]]:
    """Validate the bounded JSON shape accepted by the policy-admin plane."""

    return _decode_document(value)


def _decode_document(value: object) -> dict[str, dict[str, PermissionConstraint]]:
    if not isinstance(value, dict):
        raise ValueError("grant policy document is invalid")
    decoded: dict[str, dict[str, PermissionConstraint]] = {}
    for provider, grants in value.items():
        if not isinstance(provider, str) or not provider or not isinstance(grants, dict):
            raise ValueError("grant policy document is invalid")
        decoded[provider] = {}
        for permission, raw in grants.items():
            if not isinstance(permission, str) or not isinstance(raw, dict):
                raise ValueError("grant policy document is invalid")
            expected = {
                "path_roots", "hosts", "http_methods", "commands", "profiles",
                "secret_aliases",
            }
            if set(raw) != expected or any(
                not isinstance(raw[name], list)
                or any(not isinstance(item, str) for item in raw[name])
                for name in expected
            ):
                raise ValueError("grant policy document is invalid")
            constraint = PermissionConstraint(
                path_roots=tuple(raw["path_roots"]),
                hosts=frozenset(raw["hosts"]),
                http_methods=frozenset(raw["http_methods"]),
                commands=frozenset(raw["commands"]),
                profiles=frozenset(raw["profiles"]),
                secret_aliases=frozenset(raw["secret_aliases"]),
            )
            ParameterAuthorizer({permission: constraint})
            decoded[provider][permission] = constraint
    return decoded


def _intersect(
    permission: str, items: list[PermissionConstraint]
) -> PermissionConstraint | None:
    if not items:
        return None
    roots = tuple(
        root
        for root in items[0].path_roots
        if all(
            any(Path(root).is_relative_to(Path(candidate)) for candidate in item.path_roots)
            for item in items[1:]
        )
    )

    def common(name: str) -> frozenset[str]:
        values = [getattr(item, name) for item in items]
        return frozenset.intersection(*values) if values else frozenset()
    constraint = PermissionConstraint(
        path_roots=roots,
        hosts=common("hosts"),
        http_methods=common("http_methods"),
        commands=common("commands"),
        profiles=common("profiles"),
        secret_aliases=common("secret_aliases"),
    )
    try:
        ParameterAuthorizer({permission: constraint})
    except ValueError:
        return None
    return constraint


def _document_digest(value: object) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
