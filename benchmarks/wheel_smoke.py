"""Offline search/load/execute/RAG smoke for an installed base wheel."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    JsonValue,
    OperationSpec,
    OperationType,
)
from capabilityhub.providers.rag import LocalRagFixture, LocalRagProvider
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext


def run_offline_wheel_smoke() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "guide.txt").write_text(
            "CapabilityHub offline wheel retrieval evidence", encoding="utf-8"
        )
        api = _manifest("api", CapabilityKind.API, "run", OperationType.EXECUTE)
        rag = _manifest("rag", CapabilityKind.RAG, "retrieve", OperationType.RETRIEVE)
        registry = CapabilityRegistry()
        registry.register_many((api, rag))
        registry.activate(api.identity.coordinate, api.identity.revision)
        registry.activate(rag.identity.coordinate, rag.identity.revision)
        service = CapabilityHubService(
            registry=registry,
            providers=(
                StaticProvider(
                    (StaticFixture(api, {"run": {"offline": True}}),),
                    name="static",
                ),
                LocalRagProvider((LocalRagFixture(rag, root),)),
            ),
            references=ReferenceSigner(b"offline-wheel-smoke-reference-key"),
            audit=MemoryAuditSink(),
        )
        context = ServiceContext("wheel", "smoke", "offline")
        api_output = _invoke(service, context, "offline api", "run", {})
        rag_output = _invoke(
            service,
            context,
            "offline rag",
            "retrieve",
            {"query": "offline wheel retrieval", "top_k": 1},
        )
        assert isinstance(api_output, dict) and api_output.get("offline") is True
        assert isinstance(rag_output, dict)
        results = rag_output.get("results")
        assert isinstance(results, list) and len(results) == 1
        citation = results[0].get("citation")
        assert isinstance(citation, dict) and citation.get("path") == "guide.txt"
        return {
            "api_execute": "passed",
            "network_calls": 0,
            "rag_retrieve": "passed",
            "schema": "capabilityhub.clean-wheel-smoke.v1",
        }


def _manifest(
    name: str,
    kind: CapabilityKind,
    operation: str,
    operation_type: OperationType,
) -> CapabilityManifest:
    provider = "local-rag" if kind is CapabilityKind.RAG else "static"
    return CapabilityManifest(
        CapabilityIdentity("wheel", name, "1", "sha256:" + name[0] * 64),
        kind,
        f"Offline {name} wheel fixture.",
        provider,
        (OperationSpec(operation, operation_type),),
    )


def _invoke(
    service: CapabilityHubService,
    context: ServiceContext,
    query: str,
    operation: str,
    arguments: dict[str, JsonValue],
) -> object:
    task = f"wheel-{operation}"
    budget = BudgetLedger(
        task,
        {"bytes": 100_000, "executions": 1, "loads": 1, "portable_tokens": 10_000},
    )
    card = service.search(query, task_id=task, context=context, budget=budget, limit=1).cards[0]
    loaded = service.load(
        card.capability_ref,
        task_id=task,
        context=context,
        budget=budget,
        operation_names=(operation,),
    )
    return service.execute(
        ExecutionRequest(loaded.execution_ref, operation, arguments, task),
        context=context,
        budget=budget,
    ).output


def main() -> int:
    print(json.dumps(run_offline_wheel_smoke(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
