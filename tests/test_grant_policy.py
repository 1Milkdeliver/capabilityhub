from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from capabilityhub.auth import AuthIdentity
from capabilityhub.authorization import PermissionConstraint
from capabilityhub.grant_policy import GrantPolicyConflictError, PrincipalGrantPolicy
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
)


def _identity(tenant: str = "tenant-a", source: str = "http-loopback") -> AuthIdentity:
    return AuthIdentity(tenant, "principal", source, "session")


def _manifest(provider: str, permission: str, name: str = "tool") -> CapabilityManifest:
    return CapabilityManifest(
        CapabilityIdentity("demo", name, "1", "sha256:" + "1" * 64),
        CapabilityKind.API,
        "grant fixture",
        provider,
        (OperationSpec("run", OperationType.EXECUTE),),
        permissions=(permission,),
    )


def _network(host: str) -> PermissionConstraint:
    return PermissionConstraint(
        hosts=frozenset((host,)), http_methods=frozenset(("GET",))
    )


def test_policy_persists_cas_revision_and_audit_without_identity_text(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    policy = PrincipalGrantPolicy(path, scope_key=b"k" * 32)
    target = _identity()
    actor = _identity(source="admin-loopback")

    saved = policy.set(
        actor=actor,
        target=target,
        providers={"api-provider": {"network.http": _network("api.example.test")}},
        expected_revision=0,
        now=100,
    )

    restarted = PrincipalGrantPolicy(path, scope_key=b"k" * 32).read(target)
    assert saved.revision == restarted.revision == 1
    assert saved.document_digest == restarted.document_digest
    raw = path.read_bytes()
    assert b"tenant-a" not in raw
    assert b"principal" not in raw
    with pytest.raises(GrantPolicyConflictError):
        policy.set(
            actor=actor,
            target=target,
            providers={},
            expected_revision=0,
        )


def test_policy_is_tenant_provider_and_source_bound_default_deny(tmp_path: Path) -> None:
    policy = PrincipalGrantPolicy(tmp_path / "state.sqlite3", scope_key=b"k" * 32)
    target = _identity()
    policy.set(
        actor=_identity(source="admin-loopback"),
        target=target,
        providers={"api-provider": {"network.http": _network("api.example.test")}},
        expected_revision=0,
    )

    allowed = policy.authorizer(target).eligible(
        _manifest("api-provider", "network.http")
    )
    wrong_provider = policy.authorizer(target).eligible(
        _manifest("other-provider", "network.http")
    )
    wrong_tenant = policy.authorizer(replace(target, tenant_id="tenant-b")).eligible(
        _manifest("api-provider", "network.http")
    )
    wrong_source = policy.authorizer(replace(target, source="local-cli")).eligible(
        _manifest("api-provider", "network.http")
    )

    assert allowed.allowed is True
    assert wrong_provider.reason_codes == ("permission_not_granted",)
    assert wrong_tenant.reason_codes == ("permission_not_granted",)
    assert wrong_source.reason_codes == ("permission_not_granted",)


def test_dependency_constraints_intersect_and_upgrade_needs_new_snapshot(tmp_path: Path) -> None:
    policy = PrincipalGrantPolicy(tmp_path / "state.sqlite3", scope_key=b"k" * 32)
    target = _identity()
    actor = _identity(source="admin-loopback")
    first = policy.set(
        actor=actor,
        target=target,
        providers={"main": {"network.http": _network("api.example.test")}},
        expected_revision=0,
    ).authorizer()
    capability = _manifest("main", "network.http", "main")
    dependency = _manifest("dependency", "network.http", "dependency")
    assert first.eligible(capability, (dependency,)).allowed is False

    policy.set(
        actor=actor,
        target=target,
        providers={
            "main": {"network.http": _network("api.example.test")},
            "dependency": {"network.http": _network("api.example.test")},
        },
        expected_revision=1,
    )

    assert first.eligible(capability, (dependency,)).allowed is False
    current = policy.authorizer(target)
    assert current.eligible(capability, (dependency,)).allowed is True
    denied = current.authorize(
        capability,
        dependencies=(dependency,),
        normalized_arguments={"host": "other.example.test", "http_method": "GET"},
    )
    assert denied.reason_codes == ("host_not_allowed",)


def test_only_same_tenant_admin_identity_can_mutate(tmp_path: Path) -> None:
    policy = PrincipalGrantPolicy(tmp_path / "state.sqlite3", scope_key=b"k" * 32)
    target = _identity()
    for actor in (_identity(source="http-loopback"), _identity("tenant-b", "admin-loopback")):
        with pytest.raises(PermissionError):
            policy.set(actor=actor, target=target, providers={}, expected_revision=0)


def test_remote_mtls_policy_admin_can_mutate_only_same_tenant(tmp_path: Path) -> None:
    policy = PrincipalGrantPolicy(tmp_path / "state.sqlite3", scope_key=b"k" * 32)
    target = _identity()
    remote = _identity(source="remote-mtls-admin")

    assert policy.set(
        actor=remote,
        target=target,
        providers={"api-provider": {"network.http": _network("api.example.test")}},
        expected_revision=0,
    ).revision == 1
    with pytest.raises(PermissionError):
        policy.set(
            actor=remote,
            target=_identity("tenant-b"),
            providers={},
            expected_revision=0,
        )


@pytest.mark.parametrize("kind", tuple(CapabilityKind))
def test_policy_authorizer_covers_every_capability_kind(
    tmp_path: Path, kind: CapabilityKind
) -> None:
    policy = PrincipalGrantPolicy(tmp_path / "state.sqlite3", scope_key=b"k" * 32)
    target = _identity()
    policy.set(
        actor=_identity(source="admin-loopback"),
        target=target,
        providers={kind.value: {"network.http": _network("api.example.test")}},
        expected_revision=0,
    )
    manifest = replace(
        _manifest(kind.value, "network.http", kind.value),
        kind=kind,
    )

    assert policy.authorizer(target).eligible(manifest).allowed is True
