from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.context_removal import CONTEXT_REMOVAL, ContextRemovalCoordinator
from capabilityhub.errors import CapabilityHubError
from capabilityhub.protocol import AdapterKind
from capabilityhub.tenancy import SqliteScopedState, TenantScope

_KEY = b"context-removal-contract-test-key"


def _scope(tenant: str = "tenant") -> TenantScope:
    return TenantScope(tenant, "principal", "session", "task")


def _coordinator(
    path,
    kind: AdapterKind,
    *,
    supported: bool = True,
    tenant: str = "tenant",
) -> ContextRemovalCoordinator:
    return ContextRemovalCoordinator(
        SqliteScopedState(path, scope_key=_KEY),
        _scope(tenant),
        adapter=kind,
        client_features=(CONTEXT_REMOVAL,) if supported else (),
    )


@pytest.mark.parametrize("kind", list(AdapterKind))
def test_all_adapters_use_same_pending_then_acknowledged_contract(tmp_path, kind) -> None:
    coordinator = _coordinator(tmp_path / f"{kind}.sqlite3", kind)

    instruction = coordinator.request(
        "resident-section-handle", idempotency_key="request-one", expected_generation=0
    )
    assert instruction.confirmed is False
    assert coordinator.view()["confirmed"] == 0

    negative = coordinator.acknowledge(
        instruction.instruction_id,
        acknowledgement_id="client-failed",
        removed=False,
        expected_generation=1,
    )
    assert negative.acknowledged is True
    assert negative.confirmed is False

    retry = coordinator.retry(instruction.instruction_id, expected_generation=2)
    assert retry.attempts == 2
    confirmed = coordinator.acknowledge(
        instruction.instruction_id,
        acknowledgement_id="client-removed",
        removed=True,
        expected_generation=3,
    )
    assert confirmed.confirmed is True
    assert coordinator.view()["confirmed"] == 1


@pytest.mark.parametrize("kind", list(AdapterKind))
def test_unsupported_is_explicit_for_every_adapter(tmp_path, kind) -> None:
    coordinator = _coordinator(tmp_path / f"{kind}.sqlite3", kind, supported=False)

    assert coordinator.negotiation.supported is False
    assert coordinator.negotiation.reason_code == "context_removal_unsupported"
    with pytest.raises(CapabilityHubError) as unsupported:
        coordinator.request("handle", idempotency_key="key", expected_generation=0)
    assert unsupported.value.code == "context_removal_unsupported"
    assert coordinator.view()["instructions"] == []


def test_request_and_ack_are_idempotent_but_conflicts_fail_closed(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    coordinator = _coordinator(path, AdapterKind.CLI)

    def request(_: int):
        return _coordinator(path, AdapterKind.CLI).request(
            "handle", idempotency_key="same-key", expected_generation=0
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        instructions = list(pool.map(request, range(20)))
    assert len({item.instruction_id for item in instructions}) == 1
    instruction = instructions[0]
    assert coordinator.view()["generation"] == 1

    duplicate = coordinator.acknowledge(
        instruction.instruction_id,
        acknowledgement_id="same-ack",
        removed=True,
        expected_generation=1,
    )
    again = coordinator.acknowledge(
        instruction.instruction_id,
        acknowledgement_id="same-ack",
        removed=True,
        expected_generation=0,
    )
    assert again == duplicate
    with pytest.raises(CapabilityHubError) as conflict:
        coordinator.acknowledge(
            instruction.instruction_id,
            acknowledgement_id="different-ack",
            removed=True,
            expected_generation=2,
        )
    assert conflict.value.code == "context_removal_ack_conflict"


def test_generation_and_tenant_partition_survive_restart_without_raw_scope(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    first = _coordinator(path, AdapterKind.HTTP, tenant="TENANT-CANARY-A")
    instruction = first.request(
        "handle", idempotency_key="key", expected_generation=0
    )

    restarted = _coordinator(path, AdapterKind.HTTP, tenant="TENANT-CANARY-A")
    assert restarted.view()["generation"] == 1
    outsider = _coordinator(path, AdapterKind.HTTP, tenant="TENANT-CANARY-B")
    assert outsider.view()["instructions"] == []
    with pytest.raises(CapabilityHubError) as stale:
        restarted.retry(instruction.instruction_id, expected_generation=0)
    assert stale.value.code == "context_removal_generation_conflict"
    raw = path.read_bytes()
    assert b"TENANT-CANARY" not in raw
    assert b"principal" not in raw
    assert b"session" not in raw
    assert b"task" not in raw
