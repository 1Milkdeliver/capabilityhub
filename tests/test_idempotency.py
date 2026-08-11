from __future__ import annotations

from pathlib import Path

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError
from capabilityhub.idempotency import SqliteIdempotencyStore
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    OperationSpec,
    OperationType,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext


class _CountingProvider(StaticProvider):
    calls = 0

    def execute(self, identity, request, context: ProviderContext):
        self.calls += 1
        return super().execute(identity, request, context)


def _manifest() -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity("test", "idempotent", "1", "sha256:" + "e" * 64),
        kind=CapabilityKind.API,
        summary="Durable idempotency fixture",
        provider="static",
        operations=(OperationSpec("read", OperationType.EXECUTE),),
    )


def _service(path: Path, provider: StaticProvider, *, persist_results: bool):
    manifest = _manifest()
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    return CapabilityHubService(
        registry=registry,
        providers=[provider],
        references=ReferenceSigner(b"durable-idempotency-secret"),
        audit=MemoryAuditSink(),
        idempotency_store=SqliteIdempotencyStore(path, persist_results=persist_results),
    )


def _execute(service: CapabilityHubService):
    context = ServiceContext("tenant", "principal", "session")
    budget = BudgetLedger(
        "task", {"bytes": 10_000, "executions": 2, "loads": 2, "portable_tokens": 2_000}
    )
    card = service.search("idempotent", task_id="task", context=context, budget=budget).cards[0]
    loaded = service.load(card.capability_ref, task_id="task", context=context, budget=budget)
    return service.execute(
        ExecutionRequest(
            loaded.execution_ref,
            "read",
            {"value": 1},
            "task",
            idempotency_key="stable-key",
        ),
        context=context,
        budget=budget,
        max_output_tokens=200,
    )


def test_durable_store_can_replay_opt_in_results_after_service_restart(tmp_path) -> None:
    provider = _CountingProvider([StaticFixture(_manifest(), {"read": {"ok": True}})])
    path = tmp_path / "state.sqlite3"

    first = _execute(_service(path, provider, persist_results=True))
    second = _execute(_service(path, provider, persist_results=True))

    assert first == second
    assert provider.calls == 1


def test_default_durable_store_blocks_duplicate_without_persisting_output(tmp_path) -> None:
    provider = _CountingProvider([StaticFixture(_manifest(), {"read": {"private": True}})])
    path = tmp_path / "state.sqlite3"
    _execute(_service(path, provider, persist_results=False))

    with pytest.raises(CapabilityHubError) as raised:
        _execute(_service(path, provider, persist_results=False))

    assert raised.value.code == "idempotency_result_unavailable"
    assert provider.calls == 1
    assert "private" not in path.read_bytes().decode("latin-1", errors="ignore")


def test_restart_marks_abandoned_in_progress_record_uncertain(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    slot = ("scope", "task", "revision", "operation", "key")
    first = SqliteIdempotencyStore(path)
    assert first.reserve(slot, "arguments") is None

    recovered = SqliteIdempotencyStore(path, recover_abandoned=True)
    record = recovered.reserve(slot, "arguments")

    assert record is not None
    assert record.status == "uncertain"
