from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from capabilityhub.admin_control import (
    AdminControlAccess,
    AdminPrincipal,
    LoopbackAdminControl,
)
from capabilityhub.approval_store import ApprovalIntent, ApprovalStatus, ScopedApprovalStore
from capabilityhub.audit import MemoryAuditSink
from capabilityhub.auth import AuthIdentity, LoopbackAuthenticator
from capabilityhub.hierarchical_budget import load_or_create_hmac_key
from capabilityhub.http_control import HttpControlAccess
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.models import JsonValue
from capabilityhub.protocol import protocol_handshake
from capabilityhub.runtime import local_admin_control, local_http_control
from capabilityhub.tenancy import TenantScope


class _Backend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def dispatch(
        self,
        operation: str,
        payload: Mapping[str, JsonValue],
        identity: AuthIdentity,
    ) -> JsonValue:
        self.calls.append(operation)
        return {"operation": operation, "source": identity.source}


def test_data_and_admin_credentials_are_not_interchangeable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data, data_access = local_http_control(project)
    admin, admin_access = local_admin_control(
        project,
        roles=("policy-admin", "auditor"),
        session_id="admin-session",
    )
    try:
        set_status, set_body = _admin_post(
            admin_access,
            "policy.set",
            {"task_id": "policy-task", "rules": {"deny": ["network"]}},
        )
        assert set_status == 200
        assert set_body["result"] == {"rules": {"deny": ["network"]}}

        admin_token_for_data = admin.issue_request_token()
        data_status, data_body = _data_post(data_access, token=admin_token_for_data)
        assert data_status == 401
        assert data_body["error"]["code"] == "invalid_bearer_token"  # type: ignore[index]

        admin_status, admin_body = _admin_post(
            AdminControlAccess(admin_access.url, data_access.bearer_token),
            "policy.query",
            {"task_id": "policy-task"},
        )
        assert admin_status == 401
        assert admin_body["error"] == {"code": "invalid_bearer_token"}

        execute_status, execute_body = _admin_post(
            AdminControlAccess(admin_access.url, admin.issue_request_token()),
            "capability.execute",
            {},
        )
        assert execute_status == 400
        assert execute_body["error"] == {"code": "admin_operation_unsupported"}
    finally:
        admin.close()
        data.close()


def test_admin_roles_are_minimal_and_denied_before_backend_invocation() -> None:
    backend = _Backend()
    audit = MemoryAuditSink()
    identity = AuthIdentity("tenant", "alice", "admin-loopback", "session")
    control = LoopbackAdminControl(
        backend,
        AdminPrincipal(identity, frozenset(("auditor",))),
        audit=audit,
    )
    access = control.start()
    try:
        status, body = _admin_post(access, "policy.set", {"task_id": "task", "rules": {}})
    finally:
        control.close()

    assert status == 403
    assert body["error"] == {"code": "admin_role_denied"}
    assert backend.calls == []
    assert audit.events == []


def test_admin_tokens_expire_reject_replay_and_do_not_leak() -> None:
    now = [100.0]
    identity = AuthIdentity("tenant", "alice", "admin-loopback", "session")
    authenticator = LoopbackAuthenticator(identity, clock=lambda: now[0])
    backend = _Backend()
    audit = MemoryAuditSink()
    control = LoopbackAdminControl(
        backend,
        AdminPrincipal(identity, frozenset(("auditor",))),
        audit=audit,
        authenticator=authenticator,
        token_ttl_seconds=2,
    )
    access = control.start()
    token = access.bearer_token
    try:
        first_status, _ = _admin_post(access, "audit.query", {})
        replay_status, replay_body = _admin_post(access, "audit.query", {})
        expiring = control.issue_request_token(ttl_seconds=1)
        now[0] = 101
        expired_status, expired_body = _admin_post(
            AdminControlAccess(access.url, expiring), "audit.query", {}
        )
    finally:
        control.close()

    assert first_status == 200
    assert replay_status == 401
    assert replay_body["error"] == {"code": "authentication_replayed"}
    assert expired_status == 401
    assert expired_body["error"] == {"code": "authentication_expired"}
    serialized = json.dumps((replay_body, expired_body))
    assert token not in serialized
    assert token not in repr(access)


def test_admin_policy_and_audit_are_bound_to_authenticated_scope(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    controls = []
    accesses = []
    for tenant in ("tenant-a", "tenant-b"):
        control, access = local_admin_control(
            project,
            roles=("policy-admin", "auditor"),
            tenant_id=tenant,
            principal_id="same-principal",
            session_id="same-session",
        )
        controls.append(control)
        accesses.append(access)
    try:
        status, _ = _admin_post(
            accesses[0],
            "policy.set",
            {"task_id": "same-task", "rules": {"owner": "first"}},
        )
        assert status == 200
        second_status, second = _admin_post(
            AdminControlAccess(accesses[1].url, controls[1].issue_request_token()),
            "policy.query",
            {"task_id": "same-task"},
        )
        assert second_status == 200
        assert second["result"] == {"rules": {}}

        first_audit_status, first_audit = _admin_post(
            AdminControlAccess(accesses[0].url, controls[0].issue_request_token()),
            "audit.query",
            {"task_id": "admin-control"},
        )
        second_audit_status, second_audit = _admin_post(
            AdminControlAccess(accesses[1].url, controls[1].issue_request_token()),
            "audit.query",
            {"task_id": "admin-control"},
        )
        assert first_audit_status == second_audit_status == 200
        assert first_audit["result"]["stored"] == 1  # type: ignore[index]
        assert second_audit["result"]["stored"] == 1  # type: ignore[index]
    finally:
        for control in controls:
            control.close()


def test_admin_lifecycle_update_and_approval_use_real_runtime_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skill = home / ".codex" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\n---\nbody", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monitor = LocalCatalogMonitor(home=home, project=project, refresh_interval_seconds=0)
    identity = AuthIdentity("local", "operator", "admin-loopback", "admin")
    scope = TenantScope("local", "operator", "admin", "approval-task")
    intent = ApprovalIntent.from_arguments(
        revision="demo/revision",
        operation="write",
        arguments={"value": 1},
        tenant_id=scope.tenant,
        principal_id=scope.principal,
        session_id=scope.session,
        task_id=scope.task,
        side_effect="reversible_write",
        policy_revision="local-v1",
    )
    scope_key = load_or_create_hmac_key(project / ".capabilityhub" / "tenant-scope-hmac.key")
    approvals = ScopedApprovalStore(
        project / ".capabilityhub" / "state.sqlite3", scope_key=scope_key
    )
    approvals.request(scope, intent, ttl_seconds=60, approval_id="same-id")

    control, access = local_admin_control(
        project,
        roles=("lifecycle-operator", "approver"),
        monitor=monitor,
        session_id=identity.session_id,
    )
    try:
        lifecycle_status, lifecycle = _admin_post(
            access,
            "lifecycle.set",
            {"coordinate": "codex-user/demo", "state": "disabled"},
        )
        update_status, updates = _admin_post(
            AdminControlAccess(access.url, control.issue_request_token()),
            "update.list",
            {},
        )
        approval_status, approval = _admin_post(
            AdminControlAccess(access.url, control.issue_request_token()),
            "approval.decide",
            {
                "task_id": "approval-task",
                "approval_id": "same-id",
                "decision": "approve",
            },
        )
    finally:
        control.close()

    assert lifecycle_status == 200
    assert lifecycle["result"]["active"] is False  # type: ignore[index]
    assert update_status == 200
    assert "states" in updates["result"]  # type: ignore[operator]
    assert approval_status == 200
    assert approval["result"]["status"] == "approved"  # type: ignore[index]
    assert approvals.get(scope, "same-id").status is ApprovalStatus.APPROVED


def _admin_post(
    access: AdminControlAccess,
    operation: str,
    payload: dict[str, JsonValue],
) -> tuple[int, dict[str, object]]:
    body = json.dumps(
        {"request_id": "request", "operation": operation, "payload": payload}
    ).encode()
    request = urllib.request.Request(
        access.url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {access.bearer_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, cast(dict[str, object], json.loads(response.read()))
    except urllib.error.HTTPError as error:
        return error.code, cast(dict[str, object], json.loads(error.read()))


def _data_post(
    access: HttpControlAccess, *, token: str
) -> tuple[int, dict[str, object]]:
    handshake = protocol_handshake()
    body = json.dumps(
        {
            "request_id": "request",
            "correlation_id": "correlation",
            "operation": "capability.search",
            "payload": {"query": "", "task_id": "task", "include_inventory": True},
            "handshake": {
                "api_versions": list(handshake.api_versions),
                "supported_features": list(handshake.supported_features),
                "required_features": list(handshake.required_features),
            },
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(
        access.url,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, cast(dict[str, object], json.loads(response.read()))
    except urllib.error.HTTPError as error:
        return error.code, cast(dict[str, object], json.loads(error.read()))
