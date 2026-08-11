from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

from mcp import Client
from mcp_types import TextContent

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.mcp_server import create_empty_mcp_server, create_mcp_server
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    OperationSpec,
    OperationType,
    SectionDescriptor,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext


def _manifest(*, provider: str = "fixture") -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity("test", "records", "1.0.0", "sha256:records"),
        kind=CapabilityKind.API,
        summary="Search deterministic records.",
        provider=provider,
        operations=(OperationSpec("find", OperationType.EXECUTE),),
        sections=(SectionDescriptor("contract", "text/plain", "contract", 2),),
    )


def _budgets() -> tuple[Callable[[str], BudgetLedger], dict[str, BudgetLedger]]:
    ledgers: dict[str, BudgetLedger] = {}

    def get(task_id: str) -> BudgetLedger:
        return ledgers.setdefault(
            task_id,
            BudgetLedger(
                f"task:{task_id}",
                {
                    "bytes": 50_000,
                    "executions": 10,
                    "loads": 10,
                    "portable_tokens": 10_000,
                },
            ),
        )

    return get, ledgers


def _server(*, exploding: bool = False):  # type: ignore[no-untyped-def]
    manifest = _manifest(provider="explode" if exploding else "fixture")
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    provider = _ExplodingProvider(manifest) if exploding else StaticProvider(
        (StaticFixture(manifest, {"find": {"items": [1]}}),), name="fixture"
    )
    service = CapabilityHubService(
        registry=registry,
        providers=(provider,),
        references=ReferenceSigner(b"mcp-test-key", clock=lambda: 100),
        audit=MemoryAuditSink(),
    )
    budget_provider, ledgers = _budgets()
    context = ServiceContext("tenant", "principal", "session", max_output_tokens=1_000)
    server = create_mcp_server(
        service,
        context_provider=lambda: context,
        budget_provider=budget_provider,
    )
    return server, ledgers


class _ExplodingProvider:
    def __init__(self, manifest: CapabilityManifest) -> None:
        self._manifest = manifest

    @property
    def name(self) -> str:
        return "explode"

    def discover(self) -> tuple[CapabilityManifest, ...]:
        return (self._manifest,)

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        del identity, request, context
        raise RuntimeError("TOP-SECRET database-password")


def test_official_sdk_lists_exactly_the_three_capability_tools() -> None:
    server, _ = _server()

    async def scenario() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "capability.search",
                "capability.load",
                "capability.execute",
            ]

    asyncio.run(scenario())


def test_zero_configuration_cli_server_discovers_local_skills_safely(tmp_path) -> None:
    skill = tmp_path / ".codex" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: Demo skill\n---\nbody", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    server = create_empty_mcp_server(home=tmp_path, project=project)

    async def scenario() -> None:
        async with Client(server) as client:
            result = await client.call_tool(
                "capability.search", {"query": "", "task_id": "local-task"}
            )
            assert not result.is_error
            data = cast(dict[str, object], result.structured_content)
            assert data["total_matches"] == 1
            assert data["kind_counts"] == {
                "api": 0,
                "cli": 0,
                "mcp": 0,
                "rag": 0,
                "skill": 1,
            }

    asyncio.run(scenario())


def test_official_in_memory_client_completes_search_load_execute() -> None:
    server, ledgers = _server()

    async def scenario() -> None:
        async with Client(server) as client:
            searched = await client.call_tool(
                "capability.search",
                {"query": "records", "task_id": "task", "max_output_tokens": 2_000},
            )
            assert not searched.is_error
            search_data = cast(dict[str, object], searched.structured_content)
            cards = cast(list[dict[str, object]], search_data["cards"])

            loaded = await client.call_tool(
                "capability.load",
                {
                    "capability_ref": cards[0]["capability_ref"],
                    "task_id": "task",
                    "section_names": ["contract"],
                    "operation_names": ["find"],
                    "max_output_tokens": 2_000,
                },
            )
            assert not loaded.is_error
            load_data = cast(dict[str, object], loaded.structured_content)
            assert cast(list[dict[str, object]], load_data["sections"])[0]["name"] == "contract"

            executed = await client.call_tool(
                "capability.execute",
                {
                    "execution_ref": load_data["execution_ref"],
                    "operation": "find",
                    "arguments": {"query": "one"},
                    "task_id": "task",
                },
            )
            assert not executed.is_error
            execute_data = cast(dict[str, object], executed.structured_content)
            assert execute_data["output"] == {"items": [1]}

    asyncio.run(scenario())
    snapshot = ledgers["task"].snapshot()
    assert snapshot.used["loads"] == 1
    assert snapshot.used["executions"] == 1


def test_unhandled_provider_error_is_safe_at_the_mcp_boundary() -> None:
    server, _ = _server(exploding=True)

    async def scenario() -> None:
        async with Client(server) as client:
            searched = await client.call_tool(
                "capability.search", {"query": "records", "task_id": "task"}
            )
            search_data = cast(dict[str, object], searched.structured_content)
            cards = cast(list[dict[str, object]], search_data["cards"])
            loaded = await client.call_tool(
                "capability.load",
                {
                    "capability_ref": cards[0]["capability_ref"],
                    "task_id": "task",
                    "operation_names": ["find"],
                },
            )
            load_data = cast(dict[str, object], loaded.structured_content)
            result = await client.call_tool(
                "capability.execute",
                {
                    "execution_ref": load_data["execution_ref"],
                    "operation": "find",
                    "arguments": {},
                    "task_id": "task",
                },
            )
            assert result.is_error
            text = cast(TextContent, result.content[0]).text
            assert "provider_unhandled_error" in text
            assert "TOP-SECRET" not in text
            assert "database-password" not in text

    asyncio.run(scenario())
