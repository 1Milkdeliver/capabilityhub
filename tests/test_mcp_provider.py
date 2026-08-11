from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

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
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.mcp import McpStdioFixture, McpStdioProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext


def _manifest() -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity("test", "mcp-echo", "1", "sha256:" + "d" * 64),
        kind=CapabilityKind.MCP,
        summary="Official SDK stdio MCP fixture",
        provider="mcp-stdio",
        operations=(OperationSpec("echo", OperationType.EXECUTE),),
    )


def _server_script(tmp_path: Path) -> Path:
    script = tmp_path / "server.py"
    script.write_text(
        """import time
from mcp.server import MCPServer

server = MCPServer("fixture")

@server.tool(name="echo", description="Echo one value")
def echo(value: str) -> dict[str, str]:
    return {"value": value}

@server.tool(name="slow", description="Slow echo")
def slow(value: str) -> dict[str, str]:
    time.sleep(2)
    return {"value": value}

if __name__ == "__main__":
    server.run(transport="stdio")
""",
        encoding="utf-8",
    )
    return script


def _provider(tmp_path: Path, *, tool: str = "echo") -> McpStdioProvider:
    manifest = _manifest()
    return McpStdioProvider(
        [
            McpStdioFixture(
                manifest,
                Path(sys.executable),
                (_server_script(tmp_path).as_posix(),),
                {"echo": tool},
                cwd=tmp_path,
            )
        ]
    )


def test_mcp_stdio_provider_uses_official_client_and_service_admission(tmp_path) -> None:
    manifest = _manifest()
    provider = _provider(tmp_path)
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    service = CapabilityHubService(
        registry=registry,
        providers=[provider],
        references=ReferenceSigner(b"mcp-provider-integration-secret"),
        audit=MemoryAuditSink(),
    )
    context = ServiceContext("tenant", "principal", "session")
    budget = BudgetLedger(
        "task", {"bytes": 10_000, "executions": 2, "loads": 2, "portable_tokens": 2_000}
    )

    card = service.search("mcp echo", task_id="task", context=context, budget=budget).cards[0]
    loaded = service.load(card.capability_ref, task_id="task", context=context, budget=budget)
    result = service.execute(
        ExecutionRequest(loaded.execution_ref, "echo", {"value": "admitted"}, "task"),
        context=context,
        budget=budget,
        max_output_tokens=200,
    )

    assert result.output == {"value": "admitted"}
    assert result.provider == "mcp-stdio"


def test_mcp_stdio_provider_rejects_tool_not_advertised_by_server(tmp_path) -> None:
    provider = _provider(tmp_path, tool="missing")

    with pytest.raises(CapabilityHubError) as raised:
        provider.execute(
            _manifest().identity,
            ExecutionRequest("unused", "echo", {"value": "x"}, "task"),
            ProviderContext("tenant", "principal", "session", 2_000, 200),
        )

    assert raised.value.code == "mcp_tool_not_advertised"


def test_mcp_stdio_fixture_requires_absolute_existing_command(tmp_path) -> None:
    with pytest.raises(ValueError, match="existing absolute"):
        McpStdioFixture(_manifest(), tmp_path / "missing", (), {"echo": "echo"}, cwd=tmp_path)


def test_mcp_stdio_provider_enforces_whole_session_deadline(tmp_path) -> None:
    provider = _provider(tmp_path, tool="slow")

    with pytest.raises(CapabilityHubError) as raised:
        provider.execute(
            _manifest().identity,
            ExecutionRequest("unused", "echo", {"value": "x"}, "task"),
            ProviderContext("tenant", "principal", "session", 500, 200),
        )

    assert raised.value.code == "mcp_deadline_exceeded"
