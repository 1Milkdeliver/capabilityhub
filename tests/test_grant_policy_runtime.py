from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from urllib.request import Request, urlopen

from capabilityhub.admin_control import AdminControlAccess
from capabilityhub.http_control import HttpControlAccess
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.models import JsonValue
from capabilityhub.protocol import protocol_handshake
from capabilityhub.runtime import local_admin_control, local_http_control, local_search


def _manifest(project: Path) -> None:
    root = project / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    document = {
        "apiVersion": "capabilityhub.io/v1alpha1",
        "kind": "Capability",
        "metadata": {
            "namespace": "demo",
            "name": "network-skill",
            "version": "1",
            "digest": "sha256:" + "3" * 64,
        },
        "spec": {
            "type": "skill",
            "summary": "principal grant runtime fixture",
            "provider": "skill",
            "permissions": ["network.http"],
            "operations": [{"name": "read", "type": "load"}],
        },
    }
    (root / "network-skill.json").write_text(json.dumps(document), encoding="utf-8")


def _constraint() -> dict[str, list[str]]:
    return {
        "path_roots": [],
        "hosts": ["api.example.test"],
        "http_methods": ["GET"],
        "commands": [],
        "profiles": [],
        "secret_aliases": [],
    }


def _post(access: AdminControlAccess, operation: str, payload: object) -> dict[str, object]:
    encoded = json.dumps(
        {"request_id": "grant-policy-test", "operation": operation, "payload": payload}
    ).encode()
    request = Request(
        access.url,
        data=encoded,
        headers={
            "Authorization": f"Bearer {access.bearer_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        decoded = json.loads(response.read())
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)


def _data_search(access: HttpControlAccess) -> list[JsonValue]:
    handshake = protocol_handshake()
    envelope = {
        "request_id": "grant-data-request",
        "correlation_id": "grant-data-correlation",
        "operation": "capability.search",
        "payload": {"query": "principal grant runtime", "task_id": "grant-task"},
        "handshake": {
            "api_versions": list(handshake.api_versions),
            "supported_features": list(handshake.supported_features),
            "required_features": list(handshake.required_features),
        },
    }
    request = Request(
        access.url,
        data=json.dumps(envelope).encode(),
        headers={
            "Authorization": f"Bearer {access.bearer_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        decoded = json.loads(response.read())
    return cast(list[JsonValue], decoded["result"]["cards"])


def test_policy_admin_grant_drives_local_cli_search_after_new_snapshot(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _manifest(project)
    monitor = LocalCatalogMonitor(project=project, refresh_interval_seconds=0)

    generation = monitor.snapshot()
    revisions = tuple(generation.registry.activations.values())
    assert any(generation.registry.revision(item).permissions for item in revisions)
    before = local_search("principal grant runtime", monitor=monitor)["results"]
    assert isinstance(before, list)
    assert all(
        not isinstance(item, dict)
        or not str(item.get("revision", "")).startswith("demo/network-skill@")
        for item in before
    )
    control, access = local_admin_control(
        project,
        roles=("policy-admin",),
        tenant_id="local",
        principal_id="operator",
    )
    try:
        result = _post(
            access,
            "policy.set",
            {
                "task_id": "policy",
                "target_source": "local-cli",
                "target_principal": "operator",
                "expected_revision": 0,
                "providers": {"skill": {"network.http": _constraint()}},
            },
        )
        result_payload = result["result"]
        assert isinstance(result_payload, dict)
        assert result_payload["revision"] == 1
        query = _post(
            AdminControlAccess(access.url, control.issue_request_token()),
            "policy.query",
            {
                "task_id": "policy",
                "target_source": "local-cli",
                "target_principal": "operator",
            },
        )
        assert query["result"] == result_payload
    finally:
        control.close()

    response = local_search("principal grant runtime", monitor=monitor)
    results = cast(list[JsonValue], response["results"])
    assert any(
        str(item.get("revision", "")).startswith("demo/network-skill@")
        for item in results
        if isinstance(item, dict)
    )


def test_http_permission_upgrade_requires_new_authenticated_service_snapshot(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _manifest(project)
    monitor = LocalCatalogMonitor(
        home=tmp_path / "empty-home", project=project, refresh_interval_seconds=0
    )
    data, access = local_http_control(
        project,
        tenant_id="tenant-a",
        principal_id="reader",
        monitor=monitor,
    )
    try:
        assert not any(
            isinstance(item, dict)
            and str(item.get("revision", "")).startswith("demo/network-skill@")
            for item in _data_search(access)
        )
        admin, admin_access = local_admin_control(
            project,
            roles=("policy-admin",),
            tenant_id="tenant-a",
            principal_id="policy-operator",
        )
        try:
            _post(
                admin_access,
                "policy.set",
                {
                    "task_id": "policy",
                    "target_source": "http-loopback",
                    "target_principal": "reader",
                    "expected_revision": 0,
                    "providers": {"skill": {"network.http": _constraint()}},
                },
            )
        finally:
            admin.close()
        assert not any(
            isinstance(item, dict)
            and str(item.get("revision", "")).startswith("demo/network-skill@")
            for item in _data_search(access)
        )
    finally:
        data.close()

    restarted, restarted_access = local_http_control(
        project,
        tenant_id="tenant-a",
        principal_id="reader",
        monitor=monitor,
    )
    try:
        assert any(
            isinstance(item, dict)
            and str(item.get("revision", "")).startswith("demo/network-skill@")
            for item in _data_search(restarted_access)
        )
    finally:
        restarted.close()
