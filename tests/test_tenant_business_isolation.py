from __future__ import annotations

import json
import sqlite3
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from capabilityhub.approval_store import (
    ApprovalIntent,
    ApprovalStatus,
    ApprovalStoreError,
    ScopedApprovalStore,
)
from capabilityhub.audit import AuditEvent, ScopedAuditSink, read_scoped_audit
from capabilityhub.auth import AuthIdentity
from capabilityhub.http_control import HttpControlAccess
from capabilityhub.idempotency import SqliteIdempotencyStore
from capabilityhub.protocol import protocol_handshake
from capabilityhub.runtime import (
    local_audit,
    local_budget_report,
    local_http_control,
    local_search,
)
from capabilityhub.tenancy import SqliteScopedState, TenantScope

SCOPE_KEY = b"tenant-business-isolation-key-32b"


def _scope(tenant: str) -> TenantScope:
    return TenantScope(tenant, "same-principal", "same-session", "same-task")


def _intent(tenant: str) -> ApprovalIntent:
    return ApprovalIntent.from_arguments(
        revision="demo/revision",
        operation="write",
        arguments={"value": tenant},
        tenant_id=tenant,
        principal_id="same-principal",
        session_id="same-session",
        task_id="same-task",
        side_effect="reversible_write",
        policy_revision="policy-v1",
    )


def test_same_approval_id_is_isolated_per_scope_under_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = ScopedApprovalStore(path, scope_key=SCOPE_KEY)
    tenants = ("TENANT-CANARY-A", "TENANT-CANARY-B")

    def request(tenant: str) -> str:
        return store.request(
            _scope(tenant),
            _intent(tenant),
            ttl_seconds=60,
            approval_id="same-approval",
            now=100,
        ).intent.tenant_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(request, tenants)) == set(tenants)

    first, second = (_scope(tenant) for tenant in tenants)
    assert [item.intent.tenant_id for item in store.list(first, now=101)] == [tenants[0]]
    assert [item.intent.tenant_id for item in store.list(second, now=101)] == [tenants[1]]
    assert store.approve(first, "same-approval", decided_by="reviewer", now=101).status is (
        ApprovalStatus.APPROVED
    )
    assert store.get(second, "same-approval", now=101).status is ApprovalStatus.PENDING

    outsider = _scope("TENANT-CANARY-C")
    with pytest.raises(ApprovalStoreError) as missing:
        store.get(outsider, "same-approval", now=101)
    assert missing.value.code == "approval_not_found"
    assert store.list(outsider, now=101) == ()
    assert not any(tenant in str(missing.value.as_dict()) for tenant in tenants)

    with sqlite3.connect(path) as connection:
        keys = connection.execute(
            "SELECT scope_digest, approval_digest FROM scoped_approval_records"
        ).fetchall()
    assert len(keys) == 2
    assert all(
        len(scope_digest) == len(approval_digest) == 64
        for scope_digest, approval_digest in keys
    )
    persisted = path.read_bytes()
    assert all(tenant.encode() not in persisted for tenant in tenants)
    assert b"same-principal" not in persisted
    assert b"same-session" not in persisted
    assert b"same-task" not in persisted


def test_distinct_approver_is_same_tenant_only_and_scope_remains_opaque(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = ScopedApprovalStore(path, scope_key=SCOPE_KEY)
    requester = TenantScope(
        "TENANT-APPROVAL-CANARY", "REQUESTER-CANARY", "SESSION-CANARY", "TASK-CANARY"
    )
    intent = ApprovalIntent.from_arguments(
        revision="demo/revision",
        operation="write",
        arguments={"value": 1},
        tenant_id=requester.tenant,
        principal_id=requester.principal,
        session_id=requester.session,
        task_id=requester.task,
        side_effect="reversible_write",
        policy_revision="policy-v1",
    )
    store.request(requester, intent, ttl_seconds=60, approval_id="independent", now=100)

    approved = store.approve_as(
        requester,
        TenantScope(requester.tenant, "REVIEWER-CANARY", "admin", requester.task),
        "independent",
        now=101,
    )
    assert approved.status is ApprovalStatus.APPROVED
    assert approved.decided_by == "REVIEWER-CANARY"

    persisted = path.read_bytes()
    for canary in (requester.tenant, requester.principal, requester.session, requester.task):
        assert canary.encode() not in persisted


def test_legacy_scoped_approval_rows_are_migrated_without_raw_scope(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE scoped_approval_records (
                scope_digest TEXT NOT NULL, approval_digest TEXT NOT NULL,
                approval_id TEXT NOT NULL, revision TEXT NOT NULL,
                operation TEXT NOT NULL, arguments_digest TEXT NOT NULL,
                tenant_id TEXT NOT NULL, principal_id TEXT NOT NULL,
                session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                side_effect TEXT NOT NULL, policy_revision TEXT NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                decided_at REAL, decided_by TEXT, consumed_at REAL,
                PRIMARY KEY (scope_digest, approval_digest)
            );
            """
        )
        connection.execute(
            "INSERT INTO scoped_approval_records VALUES "
            "(?, ?, 'legacy-id', 'revision', 'read', ?, ?, ?, ?, ?, "
            "'none', 'policy', 'pending', 100, 200, NULL, NULL, NULL)",
            (
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "LEGACY-TENANT-CANARY",
                "LEGACY-PRINCIPAL-CANARY",
                "LEGACY-SESSION-CANARY",
                "LEGACY-TASK-CANARY",
            ),
        )

    ScopedApprovalStore(path, scope_key=SCOPE_KEY)

    columns = {
        str(row[1])
        for row in sqlite3.connect(path).execute(
            "PRAGMA table_info(scoped_approval_records)"
        )
    }
    assert not {"tenant_id", "principal_id", "session_id", "task_id"} & columns
    raw = path.read_bytes()
    assert b"LEGACY-TENANT-CANARY" not in raw
    assert b"LEGACY-PRINCIPAL-CANARY" not in raw


def test_idempotency_slot_is_keyed_and_same_logical_key_is_tenant_isolated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    store = SqliteIdempotencyStore(path, scope_key=SCOPE_KEY)
    slots = tuple(
        (tenant, "same-task", "same-revision", "write", "same-idempotency")
        for tenant in ("TENANT-CANARY-A", "TENANT-CANARY-B")
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda slot: store.reserve(slot, "same-arguments"), slots)) == [
            None,
            None,
        ]

    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT slot_digest FROM idempotency_records").fetchall()
    assert len(rows) == 2
    assert all(len(str(row[0])) == 64 for row in rows)
    raw = path.read_bytes()
    assert b"TENANT-CANARY-A" not in raw
    assert b"TENANT-CANARY-B" not in raw
    assert b"same-idempotency" not in raw


def test_scoped_audit_content_count_and_existence_are_not_cross_visible(
    tmp_path: Path,
) -> None:
    state = SqliteScopedState(tmp_path / "state.sqlite3", scope_key=SCOPE_KEY)
    sinks = tuple(
        ScopedAuditSink(
            state,
            tenant_id=tenant,
            principal_id="same-principal",
            session_id="same-session",
            identity_source="http-loopback",
        )
        for tenant in ("TENANT-CANARY-A", "TENANT-CANARY-B")
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda pair: pair[0].emit(
                    AuditEvent("same-event", 1, "same-task", "execute", None, pair[1])
                ),
                zip(sinks, ("first", "second"), strict=True),
            )
        )

    assert [event.outcome for event in read_scoped_audit(state, _scope("TENANT-CANARY-A"))] == [
        "first"
    ]
    assert [event.outcome for event in read_scoped_audit(state, _scope("TENANT-CANARY-B"))] == [
        "second"
    ]
    assert read_scoped_audit(state, _scope("TENANT-CANARY-C")) == ()
    raw = state.path.read_bytes()
    assert b"TENANT-CANARY-A" not in raw
    assert b"TENANT-CANARY-B" not in raw
    assert b"same-task" not in raw


def test_authenticated_http_identity_partitions_runtime_audit_query(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    identities = (
        AuthIdentity("tenant-http-a", "same-principal", "http-loopback", "same-session"),
        AuthIdentity("tenant-http-b", "same-principal", "http-loopback", "same-session"),
    )
    responses = []
    for identity in identities:
        control, access = local_http_control(
            project,
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            session_id=identity.session_id,
        )
        try:
            responses.append(_post_search(access))
        finally:
            control.close()
    assert all(response["ok"] is True for response in responses)

    views = [
        local_audit(project, identity=identity, task_id="same-task")
        for identity in identities
    ]
    assert [view["stored"] for view in views] == [1, 1]
    assert all(view["identity_source"] == "http-loopback" for view in views)
    outsider = AuthIdentity(
        "tenant-http-c", "same-principal", "http-loopback", "same-session"
    )
    hidden = local_audit(project, identity=outsider, task_id="same-task")
    assert hidden["events"] == []
    assert hidden["stored"] == 0


def test_cli_uses_restart_safe_opaque_hierarchical_budget(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    before = local_budget_report(project_root=project)

    local_search("", project_root=project)
    after = local_budget_report(project_root=project)
    restarted = local_budget_report(project_root=project)

    assert after["used"]["portable_tokens"] > before["used"]["portable_tokens"]
    assert restarted["used"] == after["used"]
    path = project / ".capabilityhub" / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        scopes = connection.execute(
            "SELECT root_id, scope_id FROM hierarchical_budget_scopes"
        ).fetchall()
        legacy_scopes = connection.execute(
            "SELECT scope FROM budget_scopes"
        ).fetchall()
    assert len(scopes) == 4
    assert all(len(root_id) == len(scope_id) == 64 for root_id, scope_id in scopes)
    raw = path.read_bytes()
    assert b"operator" not in raw
    assert legacy_scopes == [("local-cli",)]  # Explicit backward-compatible local identity.


def _post_search(access: HttpControlAccess) -> dict[str, object]:
    handshake = protocol_handshake()
    body = json.dumps(
        {
            "request_id": "same-request",
            "correlation_id": "same-correlation",
            "operation": "capability.search",
            "payload": {"query": "", "task_id": "same-task", "include_inventory": True},
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
        headers={
            "Authorization": f"Bearer {access.bearer_token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return cast(dict[str, object], json.loads(response.read()))
