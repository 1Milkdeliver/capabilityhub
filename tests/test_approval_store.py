from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.approval_store import (
    ApprovalIntent,
    ApprovalStatus,
    ApprovalStoreError,
    SqliteApprovalStore,
)


def _intent(**changes: object) -> ApprovalIntent:
    values = {
        "revision": "demo/tool@1#sha256:" + "a" * 64,
        "operation": "publish",
        "arguments": {"document": 7, "secret": "SECRET-CANARY"},
        "tenant_id": "tenant-a",
        "principal_id": "operator-a",
        "session_id": "session-a",
        "task_id": "task-a",
        "side_effect": "irreversible",
        "policy_revision": "policy-v1",
    }
    values.update(changes)
    return ApprovalIntent.from_arguments(**values)  # type: ignore[arg-type]


def test_request_and_list_store_only_exact_intent_digest(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    store = SqliteApprovalStore(path)

    requested = store.request(_intent(), ttl_seconds=60, approval_id="apr_one", now=100)
    listed = store.list(status="pending", now=101)

    assert requested.status is ApprovalStatus.PENDING
    assert listed == (requested,)
    assert requested.intent.arguments_digest != "SECRET-CANARY"
    assert b"SECRET-CANARY" not in path.read_bytes()


def test_approved_intent_survives_restart_and_is_single_use(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    intent = _intent()
    first = SqliteApprovalStore(path)
    first.request(intent, ttl_seconds=60, approval_id="apr_restart", now=100)
    approved = first.approve("apr_restart", decided_by="reviewer-a", now=101)

    restarted = SqliteApprovalStore(path)
    consumed = restarted.consume("apr_restart", intent, now=102)

    assert approved.status is ApprovalStatus.APPROVED
    assert approved.decided_by == "reviewer-a"
    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.consumed_at == 102
    with pytest.raises(ApprovalStoreError) as replay:
        restarted.consume("apr_restart", intent, now=103)
    assert replay.value.code == "approval_already_consumed"


@pytest.mark.parametrize(
    "change",
    [
        {"revision": "demo/tool@2#sha256:" + "b" * 64},
        {"operation": "delete"},
        {"arguments": {"document": 8}},
        {"tenant_id": "tenant-b"},
        {"principal_id": "operator-b"},
        {"session_id": "session-b"},
        {"task_id": "task-b"},
        {"side_effect": "reversible_write"},
        {"policy_revision": "policy-v2"},
    ],
)
def test_consume_rejects_every_changed_bound_field(tmp_path, change) -> None:
    store = SqliteApprovalStore(tmp_path / "state.sqlite3")
    store.request(_intent(), ttl_seconds=60, approval_id="apr_exact", now=100)
    store.approve("apr_exact", decided_by="reviewer", now=101)

    with pytest.raises(ApprovalStoreError) as mismatch:
        store.consume("apr_exact", _intent(**change), now=102)

    assert mismatch.value.code == "approval_intent_mismatch"
    assert store.get("apr_exact", now=102).status is ApprovalStatus.APPROVED


def test_denied_request_cannot_be_approved_or_consumed(tmp_path) -> None:
    store = SqliteApprovalStore(tmp_path / "state.sqlite3")
    intent = _intent()
    store.request(intent, ttl_seconds=60, approval_id="apr_denied", now=100)
    denied = store.deny("apr_denied", decided_by="reviewer", now=101)

    assert denied.status is ApprovalStatus.DENIED
    with pytest.raises(ApprovalStoreError) as transition:
        store.approve("apr_denied", decided_by="other", now=102)
    assert transition.value.code == "approval_invalid_transition"
    with pytest.raises(ApprovalStoreError) as consume:
        store.consume("apr_denied", intent, now=102)
    assert consume.value.code == "approval_not_approved"


@pytest.mark.parametrize("approve_first", [False, True])
def test_pending_and_approved_requests_expire(approve_first, tmp_path) -> None:
    store = SqliteApprovalStore(tmp_path / "state.sqlite3")
    intent = _intent()
    identifier = f"apr_expiry_{approve_first}"
    store.request(intent, ttl_seconds=5, approval_id=identifier, now=100)
    if approve_first:
        store.approve(identifier, decided_by="reviewer", now=101)

    assert store.get(identifier, now=105).status is ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalStoreError) as expired:
        store.consume(identifier, intent, now=106)
    assert expired.value.code == "approval_expired"


def test_concurrent_consumers_cannot_replay_one_approval(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    store = SqliteApprovalStore(path)
    intent = _intent()
    store.request(intent, ttl_seconds=60, approval_id="apr_race", now=100)
    store.approve("apr_race", decided_by="reviewer", now=101)

    def consume_once(_: int) -> str:
        try:
            SqliteApprovalStore(path).consume("apr_race", intent, now=102)
        except ApprovalStoreError as error:
            return error.code
        return "consumed"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(consume_once, range(8)))

    assert outcomes.count("consumed") == 1
    assert outcomes.count("approval_already_consumed") == 7


def test_duplicate_ids_and_invalid_transitions_do_not_replace_records(tmp_path) -> None:
    store = SqliteApprovalStore(tmp_path / "state.sqlite3")
    original = store.request(_intent(), ttl_seconds=60, approval_id="apr_unique", now=100)

    with pytest.raises(ValueError, match="unique"):
        store.request(_intent(operation="other"), ttl_seconds=60, approval_id="apr_unique")
    with pytest.raises(ApprovalStoreError) as pending:
        store.consume("apr_unique", original.intent, now=101)

    assert pending.value.code == "approval_not_approved"
    assert store.get("apr_unique", now=101) == original


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True])
def test_request_rejects_invalid_expiry_values(tmp_path, ttl) -> None:
    store = SqliteApprovalStore(tmp_path / "state.sqlite3")

    with pytest.raises(ValueError, match="positive"):
        store.request(_intent(), ttl_seconds=ttl)
