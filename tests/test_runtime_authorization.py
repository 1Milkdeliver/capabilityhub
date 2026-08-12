from __future__ import annotations

import json
from pathlib import Path

import pytest

from capabilityhub.authorization import ParameterAuthorizer, PermissionConstraint
from capabilityhub.errors import CapabilityHubError
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.runtime import local_execute_static, local_search


def _catalog(tmp_path) -> tuple[LocalCatalogMonitor, dict[str, str], Path]:
    project = tmp_path / "project"
    allowed_root = project / "allowed"
    allowed_root.mkdir(parents=True)
    manifests = project / ".capabilityhub" / "manifests"
    manifests.mkdir(parents=True)
    permissions = {
        "skill": [],
        "cli": ["process.execute"],
        "api": ["network.http"],
        "rag": ["filesystem.read"],
        "mcp": ["secret.use"],
    }
    revisions: dict[str, str] = {}
    for index, kind in enumerate(("skill", "cli", "mcp", "api", "rag"), start=1):
        digest = f"sha256:{index:064x}"
        name = f"{kind}-tool"
        operation = {
            "name": "run",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "host": {"type": "string"},
                    "http_method": {"type": "string"},
                    "command": {"type": "string"},
                    "profile": {"type": "string"},
                    "secret_alias": {"type": "string"},
                    "token": {"type": "string"},
                },
            },
        }
        spec: dict[str, object] = {
            "type": kind,
            "summary": f"Authorization fixture for {kind}",
            "provider": "matrix-provider",
            "permissions": permissions[kind],
            "operations": [operation],
        }
        if kind == "api":
            spec["dependencies"] = [{"coordinate": "demo/mcp-tool"}]
        document = {
            "apiVersion": "capabilityhub.io/v1alpha1",
            "kind": "Capability",
            "metadata": {
                "namespace": "demo",
                "name": name,
                "version": "1",
                "digest": digest,
            },
            "spec": spec,
        }
        (manifests / f"{kind}.json").write_text(json.dumps(document), encoding="utf-8")
        revisions[kind] = f"demo/{name}@1#{digest}"
    return (
        LocalCatalogMonitor(
            home=tmp_path / "home",
            project=project,
            refresh_interval_seconds=0,
        ),
        revisions,
        allowed_root,
    )


def _authorizer(root: Path, *, include_secret: bool = True) -> ParameterAuthorizer:
    grants = {
        "filesystem.read": PermissionConstraint(path_roots=(root,)),
        "network.http": PermissionConstraint(
            hosts=frozenset({"api.example.test"}),
            http_methods=frozenset({"GET"}),
        ),
        "process.execute": PermissionConstraint(
            commands=frozenset({"tool"}),
            profiles=frozenset({"readonly"}),
        ),
    }
    if include_secret:
        grants["secret.use"] = PermissionConstraint(
            secret_aliases=frozenset({"service-read"})
        )
    return ParameterAuthorizer(grants)


def test_search_filters_all_five_kinds_and_dependency_permission(tmp_path) -> None:
    monitor, _revisions, root = _catalog(tmp_path)

    constrained = local_search(
        "authorization fixture",
        parameter_authorizer=_authorizer(root, include_secret=False),
        monitor=monitor,
    )
    complete = local_search(
        "authorization fixture",
        parameter_authorizer=_authorizer(root),
        monitor=monitor,
    )

    assert {item["kind"] for item in constrained["results"]} == {"skill", "cli", "rag"}
    assert {item["kind"] for item in complete["results"]} == {
        "skill",
        "cli",
        "api",
        "rag",
        "mcp",
    }


@pytest.mark.parametrize(
    ("kind", "arguments", "reason"),
    (
        ("rag", {"path": "{outside_path}"}, "path_outside_allowed_roots"),
        (
            "api",
            {"host": "blocked.example.test", "http_method": "GET"},
            "host_not_allowed",
        ),
        (
            "api",
            {"host": "api.example.test", "http_method": "POST"},
            "http_method_not_allowed",
        ),
        ("cli", {"command": "admin", "profile": "readonly"}, "command_not_allowed"),
        ("cli", {"command": "tool", "profile": "admin"}, "profile_not_allowed"),
        ("mcp", {"secret_alias": "service-write"}, "secret_alias_not_allowed"),
    ),
)
def test_execute_rejects_constrained_arguments_before_provider(
    tmp_path,
    kind: str,
    arguments: dict[str, str],
    reason: str,
) -> None:
    monitor, revisions, root = _catalog(tmp_path)
    selected = dict(arguments)
    if selected.get("path") == "{outside_path}":
        selected["path"] = str(tmp_path / "outside" / "private.txt")

    with pytest.raises(CapabilityHubError) as denied:
        local_execute_static(
            revisions[kind],
            "run",
            selected,
            {"provider_was_called": True},
            parameter_authorizer=_authorizer(root),
            monitor=monitor,
        )

    assert denied.value.code == "argument_authorization_denied"
    assert reason in denied.value.details["reason_codes"]
    assert "provider_was_called" not in str(denied.value.as_dict())


@pytest.mark.parametrize(
    ("kind", "arguments"),
    (
        ("rag", {"path": "{allowed_path}"}),
        (
            "api",
            {
                "host": "api.example.test",
                "http_method": "GET",
                "secret_alias": "service-read",
            },
        ),
        ("cli", {"command": "tool", "profile": "readonly"}),
        ("mcp", {"secret_alias": "service-read"}),
    ),
)
def test_authorized_constraints_reach_each_executable_provider_kind(
    tmp_path,
    kind: str,
    arguments: dict[str, str],
) -> None:
    monitor, revisions, root = _catalog(tmp_path)
    selected = dict(arguments)
    if selected.get("path") == "{allowed_path}":
        selected["path"] = str(root / "document.txt")

    result = local_execute_static(
        revisions[kind],
        "run",
        selected,
        {"ok": kind},
        parameter_authorizer=_authorizer(root),
        monitor=monitor,
    )

    assert result["output"] == {"ok": kind}


def test_raw_secret_and_alias_never_leak_to_error_or_audit(tmp_path) -> None:
    monitor, revisions, root = _catalog(tmp_path)
    canary = "SECRET-CANARY-RAW-VALUE"

    with pytest.raises(CapabilityHubError) as denied:
        local_execute_static(
            revisions["mcp"],
            "run",
            {"secret_alias": "blocked-alias", "token": canary},
            {"private": canary},
            parameter_authorizer=_authorizer(root),
            monitor=monitor,
        )

    assert denied.value.details["reason_codes"] == (
        "secret_value_not_allowed",
        "secret_alias_not_allowed",
    )
    assert canary not in str(denied.value.as_dict())
    audit = monitor.project / ".capabilityhub" / "audit.jsonl"
    assert canary not in audit.read_text(encoding="utf-8")
