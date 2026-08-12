from __future__ import annotations

from pathlib import Path

import pytest

from capabilityhub.approval_store import ApprovalIntent, ScopedApprovalStore
from capabilityhub.errors import CapabilityHubError
from capabilityhub.hierarchical_budget import load_or_create_hmac_key
from capabilityhub.runtime import local_admin_dispatch
from capabilityhub.tenancy import TenantScope


def test_cli_and_dashboard_admin_entries_return_same_lifecycle_contract(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    cli = local_admin_dispatch(
        "lifecycle.list",
        {},
        roles=("lifecycle-operator",),
        source="admin-cli",
        project_root=project,
    )
    dashboard = local_admin_dispatch(
        "lifecycle.list",
        {},
        roles=("lifecycle-operator",),
        source="admin-dashboard",
        project_root=project,
    )

    assert dashboard == cli


def test_distinct_authenticated_approver_can_decide_exact_requester_intent(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    state_root = project / ".capabilityhub"
    state_root.mkdir(parents=True)
    scope_key = load_or_create_hmac_key(state_root / "tenant-scope-hmac.key")
    store = ScopedApprovalStore(state_root / "state.sqlite3", scope_key=scope_key)
    scope = TenantScope("local", "operator", "cli", "local-cli")
    intent = ApprovalIntent(
        revision="demo/tool@1#sha256:" + "1" * 64,
        operation="run",
        arguments_digest="2" * 64,
        tenant_id="local",
        principal_id="operator",
        session_id="cli",
        task_id="local-cli",
        side_effect="reversible_write",
        policy_revision="local-v1",
    )
    record = store.request(scope, intent, ttl_seconds=60, approval_id="same-id")

    approved = local_admin_dispatch(
        "approval.decide",
        {
            "task_id": "local-cli",
            "approval_id": record.approval_id,
            "decision": "approve",
            "requester_principal_id": "operator",
            "requester_session_id": "cli",
        },
        roles=("approver",),
        source="admin-dashboard",
        project_root=project,
        principal_id="other-principal",
    )
    assert approved["status"] == "approved"
    assert approved["decided_by"] == "other-principal"

    with pytest.raises(CapabilityHubError):
        store.approve_as(
            scope,
            TenantScope("other-tenant", "reviewer", "admin", "local-cli"),
            record.approval_id,
        )
