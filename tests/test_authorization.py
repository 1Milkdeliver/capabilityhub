from __future__ import annotations

import pytest

from capabilityhub.authorization import ParameterAuthorizer, PermissionConstraint
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
)


def _manifest(name: str, permissions: tuple[str, ...]) -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity("test", name, "1", "sha256:" + "0" * 64),
        kind=CapabilityKind.API,
        summary="Authorization fixture",
        provider="fixture",
        operations=(OperationSpec("run", OperationType.EXECUTE),),
        permissions=permissions,
    )


def test_search_eligibility_intersects_capability_and_dependency_requirements(tmp_path) -> None:
    authorizer = ParameterAuthorizer(
        {
            "filesystem.read": PermissionConstraint(path_roots=(tmp_path,)),
            "secret.use": PermissionConstraint(secret_aliases=frozenset({"docs-token"})),
        }
    )
    capability = _manifest("capability", ("filesystem.read",))
    dependency = _manifest("dependency", ("secret.use",))

    decision = authorizer.eligible(capability, (dependency,))

    assert decision.allowed is True
    assert decision.reason_codes == ("authorization_allow",)
    assert decision.required_permissions == ("filesystem.read", "secret.use")
    assert decision.effective_permissions == decision.required_permissions


def test_missing_dependency_permission_denies_search_and_execute_consistently(tmp_path) -> None:
    authorizer = ParameterAuthorizer(
        {"filesystem.read": PermissionConstraint(path_roots=(tmp_path,))}
    )
    capability = _manifest("capability", ("filesystem.read",))
    dependency = _manifest("dependency", ("secret.use",))

    search = authorizer.eligible(capability, (dependency,))
    execute = authorizer.authorize(
        capability,
        dependencies=(dependency,),
        normalized_arguments={"path": str(tmp_path / "document.txt")},
    )

    assert search.allowed is execute.allowed is False
    assert search.reason_codes == execute.reason_codes == ("permission_not_granted",)
    assert "docs-token" not in repr(execute)


def test_unknown_permissions_and_malformed_grants_fail_closed(tmp_path) -> None:
    decision = ParameterAuthorizer({}).eligible(_manifest("unknown", ("SECRET-CANARY",)))

    assert decision.allowed is False
    assert decision.reason_codes == ("unknown_permission", "permission_not_granted")
    assert "SECRET-CANARY" not in repr(decision)
    with pytest.raises(ValueError, match="unsupported permission"):
        ParameterAuthorizer({"future.power": PermissionConstraint()})
    with pytest.raises(ValueError, match="missing its required constraint"):
        ParameterAuthorizer({"filesystem.read": PermissionConstraint()})
    with pytest.raises(ValueError, match="absolute"):
        ParameterAuthorizer({"filesystem.read": PermissionConstraint(path_roots=("relative",))})


def test_path_must_resolve_inside_every_allowed_root(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    sibling = tmp_path / "allowed-escape"
    sibling.mkdir()
    authorizer = ParameterAuthorizer({"filesystem.read": PermissionConstraint(path_roots=(root,))})
    manifest = _manifest("files", ("filesystem.read",))

    allowed = authorizer.authorize(
        manifest, normalized_arguments={"path": str(root / "document.txt")}
    )
    escaped = authorizer.authorize(
        manifest, normalized_arguments={"path": str(sibling / "document.txt")}
    )
    relative = authorizer.authorize(manifest, normalized_arguments={"path": "document.txt"})

    assert allowed.allowed is True
    assert escaped.reason_codes == ("path_outside_allowed_roots",)
    assert relative.reason_codes == ("arguments_invalid",)


def test_network_constraints_intersect_across_capability_and_dependency() -> None:
    authorizer = ParameterAuthorizer(
        {
            "network": PermissionConstraint(
                hosts=frozenset({"API.Example.com."}),
                http_methods=frozenset({"GET", "POST"}),
            ),
            "network.http": PermissionConstraint(
                hosts=frozenset({"api.example.com", "other.example.com"}),
                http_methods=frozenset({"GET"}),
            ),
        }
    )
    capability = _manifest("network", ("network",))
    dependency = _manifest("http", ("network.http",))

    allowed = authorizer.authorize(
        capability,
        dependencies=(dependency,),
        normalized_arguments={"host": "api.example.com", "http_method": "get"},
    )
    wrong_host = authorizer.authorize(
        capability,
        dependencies=(dependency,),
        normalized_arguments={"host": "other.example.com", "http_method": "GET"},
    )
    wrong_method = authorizer.authorize(
        capability,
        dependencies=(dependency,),
        normalized_arguments={"host": "api.example.com", "http_method": "POST"},
    )
    host_with_port = authorizer.authorize(
        capability,
        dependencies=(dependency,),
        normalized_arguments={"host": "api.example.com:443", "http_method": "GET"},
    )

    assert allowed.allowed is True
    assert wrong_host.reason_codes == ("host_not_allowed",)
    assert wrong_method.reason_codes == ("http_method_not_allowed",)
    assert host_with_port.reason_codes == ("host_not_allowed",)


def test_process_command_and_profile_are_both_exactly_constrained() -> None:
    authorizer = ParameterAuthorizer(
        {
            "process.execute": PermissionConstraint(
                commands=frozenset({"formatter"}),
                profiles=frozenset({"read-only"}),
            )
        }
    )
    manifest = _manifest("process", ("process.execute",))

    allowed = authorizer.authorize(
        manifest,
        normalized_arguments={"command": "formatter", "profile": "read-only"},
    )
    denied = authorizer.authorize(
        manifest,
        normalized_arguments={"command": "shell", "profile": "admin"},
    )

    assert allowed.allowed is True
    assert denied.reason_codes == ("command_not_allowed", "profile_not_allowed")


def test_secret_authorization_matches_alias_only_and_never_returns_values() -> None:
    authorizer = ParameterAuthorizer(
        {"secret.use": PermissionConstraint(secret_aliases=frozenset({"github-read"}))}
    )
    manifest = _manifest("secret", ("secret.use",))

    allowed = authorizer.authorize(manifest, normalized_arguments={"secret_alias": "github-read"})
    denied = authorizer.authorize(
        manifest,
        normalized_arguments={
            "secret_alias": "github-write",
            "token": "SECRET-CANARY",
        },
    )

    assert allowed.allowed is True
    assert denied.reason_codes == (
        "secret_value_not_allowed",
        "secret_alias_not_allowed",
    )
    assert "github-write" not in repr(denied)
    assert "SECRET-CANARY" not in repr(denied)


def test_privileged_argument_without_declared_permission_is_denied() -> None:
    manifest = _manifest("plain", ())

    decision = ParameterAuthorizer({}).authorize(
        manifest,
        normalized_arguments={"host": "api.example.com", "http_method": "GET"},
    )

    assert decision.reason_codes == ("privilege_not_declared",)
