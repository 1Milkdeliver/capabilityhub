from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

from mcp import Client
from mcp_types import RequestParamsMeta, TextContent
from pytest import MonkeyPatch

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.mcp_server import (
    MCP_CORRELATION_META_KEY,
    create_empty_mcp_server,
    create_mcp_server,
)
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
from capabilityhub.protocol import AdapterKind, JsonValue, RequestEnvelope
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext
from capabilityhub.service_adapter import CapabilityHubServiceAdapter


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
    provider = (
        _ExplodingProvider(manifest)
        if exploding
        else StaticProvider((StaticFixture(manifest, {"find": {"items": [1]}}),), name="fixture")
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
            schemas = {tool.name: tool.input_schema for tool in listed.tools}
            assert set(schemas["capability.search"]["properties"]) == {
                "query",
                "task_id",
                "kinds",
                "limit",
                "max_output_tokens",
                "include_inventory",
                "include_cards",
            }
            assert schemas["capability.search"]["required"] == ["query", "task_id"]
            assert set(schemas["capability.load"]["properties"]) == {
                "capability_ref",
                "task_id",
                "section_names",
                "operation_names",
                "max_output_tokens",
            }
            assert schemas["capability.load"]["required"] == [
                "capability_ref",
                "task_id",
            ]
            assert set(schemas["capability.execute"]["properties"]) == {
                "execution_ref",
                "operation",
                "arguments",
                "task_id",
                "approval_ref",
                "idempotency_key",
                "max_output_tokens",
            }
            assert schemas["capability.execute"]["required"] == [
                "execution_ref",
                "operation",
                "arguments",
                "task_id",
            ]

    asyncio.run(scenario())


def test_sdk_correlation_and_result_match_the_in_memory_service_adapter(
    monkeypatch: MonkeyPatch,
) -> None:
    server, _ = _server()
    captured: list[tuple[RequestEnvelope, JsonValue]] = []
    original_dispatch = CapabilityHubServiceAdapter.dispatch

    def capture_dispatch(
        adapter: CapabilityHubServiceAdapter,
        request: RequestEnvelope,
    ) -> JsonValue:
        result = original_dispatch(adapter, request)
        captured.append((request, result))
        return result

    monkeypatch.setattr(CapabilityHubServiceAdapter, "dispatch", capture_dispatch)

    async def scenario() -> None:
        metadata = {MCP_CORRELATION_META_KEY: "sdk-trace-42"}
        async with Client(server) as client:
            result = await client.call_tool(
                "capability.search",
                {"query": "records", "task_id": "correlated-task"},
                meta=cast(RequestParamsMeta, metadata),
            )
            assert not result.is_error
            assert captured
            request, direct_result = captured[0]
            assert request.adapter is AdapterKind.MCP
            assert request.correlation_id == "sdk-trace-42"
            assert result.structured_content == direct_result

    asyncio.run(scenario())


def test_invalid_sdk_correlation_is_a_safe_typed_error() -> None:
    server, _ = _server()

    async def scenario() -> None:
        metadata = {MCP_CORRELATION_META_KEY: "SECRET invalid correlation"}
        async with Client(server) as client:
            result = await client.call_tool(
                "capability.search",
                {"query": "records", "task_id": "correlated-task"},
                meta=cast(RequestParamsMeta, metadata),
            )
            assert result.is_error
            text = cast(TextContent, result.content[0]).text
            assert "invalid_correlation_id" in text
            assert "SECRET" not in text

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
            assert data["total_matches"] == 2
            assert data["kind_counts"] == {
                "api": 0,
                "cli": 1,
                "mcp": 0,
                "rag": 0,
                "skill": 1,
            }

    asyncio.run(scenario())


def test_local_server_refreshes_inventory_atomically_in_the_same_process(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    server = create_empty_mcp_server(home=home, project=project, refresh_interval_seconds=0)

    async def inventory(client: Client, *, task: str) -> dict[str, object]:
        result = await client.call_tool(
            "capability.search",
            {
                "query": "",
                "task_id": task,
                "include_inventory": True,
                "include_cards": False,
                "max_output_tokens": 2_000,
            },
        )
        assert not result.is_error
        data = cast(dict[str, object], result.structured_content)
        assert data["cards"] == []
        return cast(dict[str, object], data["inventory"])

    async def scenario() -> None:
        async with Client(server) as client:
            first = await inventory(client, task="refresh-1")
            assert first["generation"] == 1
            assert first["active_total"] == 1

            skill = home / ".codex" / "skills" / "demo" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: demo\n---\nfirst body", encoding="utf-8")
            concurrent = await asyncio.gather(
                *(inventory(client, task=f"refresh-2-{index}") for index in range(8))
            )
            assert {item["generation"] for item in concurrent} == {2}
            second = concurrent[0]
            assert cast(dict[str, object], second["active_by_kind"])["skill"] == 1

            unchanged = await inventory(client, task="refresh-3")
            assert unchanged["generation"] == 2

            searched = await client.call_tool(
                "capability.search",
                {"query": "demo", "task_id": "refresh-ref", "max_output_tokens": 2_000},
            )
            cards = cast(
                list[dict[str, object]],
                cast(dict[str, object], searched.structured_content)["cards"],
            )
            old_ref = cast(str, cards[0]["capability_ref"])

            other = home / ".codex" / "skills" / "other" / "SKILL.md"
            other.parent.mkdir(parents=True)
            other.write_text("---\nname: other\n---\nbody", encoding="utf-8")
            third = await inventory(client, task="refresh-4")
            assert third["generation"] == 3
            still_valid = await client.call_tool(
                "capability.load",
                {"capability_ref": old_ref, "task_id": "refresh-ref"},
            )
            assert not still_valid.is_error

            skill.write_text("---\nname: demo\n---\nchanged body is longer", encoding="utf-8")
            fourth = await inventory(client, task="refresh-5")
            assert fourth["generation"] == 4
            stale = await client.call_tool(
                "capability.load",
                {"capability_ref": old_ref, "task_id": "refresh-ref"},
            )
            assert stale.is_error
            assert "stale_revision" in cast(TextContent, stale.content[0]).text

            skill.unlink()
            fifth = await inventory(client, task="refresh-6")
            assert fifth["generation"] == 5
            assert cast(dict[str, object], fifth["active_by_kind"])["skill"] == 1
            assert fifth["active_total"] == sum(
                cast(dict[str, int], fifth["active_by_kind"]).values()
            )

    asyncio.run(scenario())


def test_local_server_keeps_last_complete_snapshot_when_refresh_check_fails(
    tmp_path, monkeypatch
) -> None:
    server = create_empty_mcp_server(
        home=tmp_path,
        project=tmp_path,
        refresh_interval_seconds=0,
    )

    def fail_fingerprint(**_kwargs: object) -> str:
        raise OSError("SECRET-CANARY must not escape")

    monkeypatch.setattr("capabilityhub.local_runtime.local_catalog_fingerprint", fail_fingerprint)

    async def scenario() -> None:
        async with Client(server) as client:
            result = await client.call_tool(
                "capability.search",
                {
                    "query": "",
                    "task_id": "stale-refresh",
                    "include_inventory": True,
                    "include_cards": False,
                    "max_output_tokens": 2_000,
                },
            )
            assert not result.is_error
            data = cast(dict[str, object], result.structured_content)
            inventory = cast(dict[str, object], data["inventory"])
            assert inventory["status"] == "stale"
            assert inventory["last_refresh_error_code"] == "catalog_refresh_failed"
            assert "SECRET-CANARY" not in cast(TextContent, result.content[0]).text

            monkeypatch.undo()
            recovered = await client.call_tool(
                "capability.search",
                {
                    "query": "",
                    "task_id": "recovered-refresh",
                    "include_inventory": True,
                    "include_cards": False,
                    "max_output_tokens": 2_000,
                },
            )
            recovered_data = cast(dict[str, object], recovered.structured_content)
            recovered_inventory = cast(dict[str, object], recovered_data["inventory"])
            assert recovered_inventory["status"] == "fresh"
            assert recovered_inventory["last_refresh_error_code"] is None

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
