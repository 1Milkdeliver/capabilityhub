from __future__ import annotations

import os

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    OperationSpec,
    OperationType,
    SideEffect,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.rag import LocalRagFixture, LocalRagProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext


def _manifest() -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity("test", "docs", "1", "sha256:" + "c" * 64),
        kind=CapabilityKind.RAG,
        summary="Local documentation retrieval",
        provider="local-rag",
        operations=(
            OperationSpec(
                "retrieve",
                OperationType.RETRIEVE,
                side_effect=SideEffect.READ,
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                },
            ),
        ),
    )


def _context(*, max_output_tokens: int = 500) -> ProviderContext:
    return ProviderContext("tenant", "principal", "session", 2_000, max_output_tokens)


def test_local_rag_ranks_chunks_and_returns_relative_line_citations(tmp_path) -> None:
    (tmp_path / "guide.md").write_text(
        "intro\nCapabilityHub budget budget controls\nending", encoding="utf-8"
    )
    (tmp_path / "other.txt").write_text("budget overview", encoding="utf-8")
    provider = LocalRagProvider([LocalRagFixture(_manifest(), tmp_path, chunk_lines=1)])

    result = provider.execute(
        _manifest().identity,
        ExecutionRequest("unused", "retrieve", {"query": "budget", "top_k": 2}, "task"),
        _context(),
    )

    items = result.output["results"]
    assert items[0]["score"] == 2
    assert items[0]["citation"] == {"end_line": 2, "path": "guide.md", "start_line": 2}
    assert all(not os.path.isabs(item["citation"]["path"]) for item in items)


def test_local_rag_truncates_results_to_the_output_budget(tmp_path) -> None:
    for index in range(5):
        (tmp_path / f"{index}.txt").write_text("match " * 100, encoding="utf-8")
    provider = LocalRagProvider([LocalRagFixture(_manifest(), tmp_path)])

    result = provider.execute(
        _manifest().identity,
        ExecutionRequest("unused", "retrieve", {"query": "match", "top_k": 5}, "task"),
        _context(max_output_tokens=40),
    )

    assert result.output["truncated"] is True
    assert result.portable_tokens <= 40


def test_local_rag_skips_symlinks_that_escape_the_approved_root(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET-CANARY match", encoding="utf-8")
    link = root / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    provider = LocalRagProvider([LocalRagFixture(_manifest(), root)])

    result = provider.execute(
        _manifest().identity,
        ExecutionRequest("unused", "retrieve", {"query": "match"}, "task"),
        _context(),
    )

    assert result.output["results"] == []
    assert "SECRET-CANARY" not in str(result.output)


def test_local_rag_runs_through_search_load_and_execute_admission(tmp_path) -> None:
    (tmp_path / "guide.md").write_text("CapabilityHub staged retrieval", encoding="utf-8")
    manifest = _manifest()
    provider = LocalRagProvider([LocalRagFixture(manifest, tmp_path)])
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    service = CapabilityHubService(
        registry=registry,
        providers=[provider],
        references=ReferenceSigner(b"rag-provider-integration-secret"),
        audit=MemoryAuditSink(),
    )
    context = ServiceContext("tenant", "principal", "session")
    budget = BudgetLedger(
        "task", {"bytes": 10_000, "executions": 2, "loads": 2, "portable_tokens": 2_000}
    )

    card = service.search("documentation", task_id="task", context=context, budget=budget).cards[0]
    loaded = service.load(card.capability_ref, task_id="task", context=context, budget=budget)
    result = service.execute(
        ExecutionRequest(loaded.execution_ref, "retrieve", {"query": "staged retrieval"}, "task"),
        context=context,
        budget=budget,
        max_output_tokens=200,
    )

    assert result.output["results"][0]["citation"]["path"] == "guide.md"


def test_local_rag_rejects_invalid_query_and_top_k(tmp_path) -> None:
    provider = LocalRagProvider([LocalRagFixture(_manifest(), tmp_path)])

    with pytest.raises(CapabilityHubError, match="non-empty"):
        provider.execute(
            _manifest().identity,
            ExecutionRequest("unused", "retrieve", {"query": ""}, "task"),
            _context(),
        )
    with pytest.raises(CapabilityHubError, match="1 to 20"):
        provider.execute(
            _manifest().identity,
            ExecutionRequest("unused", "retrieve", {"query": "x", "top_k": 21}, "task"),
            _context(),
        )
