"""Official MCP Python SDK v2 adapter for CapabilityHub's three meta-tools.

This module registers tools on :class:`mcp.server.MCPServer`; protocol framing,
sessions, and every transport remain the SDK's responsibility.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any, Literal, TypeVar

from mcp.server import MCPServer
from mcp_types import CallToolResult, TextContent

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.local_catalog import discover_local_catalog
from capabilityhub.metering import canonical_json
from capabilityhub.models import ExecutionRequest, JsonValue, LoadedCapability, SearchCard
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.search import SearchResponse
from capabilityhub.service import CapabilityHubService, ServiceContext

ContextProvider = Callable[[], ServiceContext]
BudgetProvider = Callable[[str], BudgetLedger]
_ResultT = TypeVar("_ResultT", bound=dict[str, JsonValue])


def create_mcp_server(
    service: CapabilityHubService,
    *,
    context_provider: ContextProvider,
    budget_provider: BudgetProvider,
    name: str = "CapabilityHub",
) -> MCPServer:
    """Create an SDK-owned server exposing exactly search, load, and execute."""

    if not name:
        raise ValueError("name must be non-empty")
    server = MCPServer(
        name,
        description="Budgeted discovery, loading, and execution of governed capabilities.",
    )

    @server.tool(
        name="capability.search",
        description="Find active capabilities and return compact, scoped load references.",
    )
    def search(
        query: str,
        task_id: str,
        kinds: list[str] | None = None,
        limit: int = 8,
        max_output_tokens: int = 900,
    ) -> CallToolResult:
        """Search active capabilities within a hard output budget."""

        return _safe(
            lambda: _search(
                service,
                context_provider,
                budget_provider,
                query=query,
                task_id=_task_id(task_id),
                kinds=kinds,
                limit=limit,
                max_output_tokens=max_output_tokens,
            )
        )

    @server.tool(
        name="capability.load",
        description="Load only selected capability sections and operation contracts.",
    )
    def load(
        capability_ref: str,
        task_id: str,
        section_names: list[str] | None = None,
        operation_names: list[str] | None = None,
        max_output_tokens: int = 2_000,
    ) -> CallToolResult:
        """Resolve a scoped load reference and progressively disclose content."""

        return _safe(
            lambda: _load(
                service,
                context_provider,
                budget_provider,
                capability_ref=capability_ref,
                task_id=_task_id(task_id),
                section_names=section_names,
                operation_names=operation_names,
                max_output_tokens=max_output_tokens,
            )
        )

    @server.tool(
        name="capability.execute",
        description="Execute one loaded operation through its governed named provider.",
    )
    def execute(
        execution_ref: str,
        operation: str,
        arguments: dict[str, Any],
        task_id: str,
        approval_ref: str | None = None,
        idempotency_key: str | None = None,
        max_output_tokens: int | None = None,
    ) -> CallToolResult:
        """Verify, authorize, budget, and execute a loaded capability operation."""

        return _safe(
            lambda: _execute(
                service,
                context_provider,
                budget_provider,
                execution_ref=execution_ref,
                operation=operation,
                arguments=_json_object(arguments),
                task_id=_task_id(task_id),
                approval_ref=approval_ref,
                idempotency_key=idempotency_key,
                max_output_tokens=max_output_tokens,
            )
        )

    return server


def serve(
    service: CapabilityHubService | None = None,
    *,
    context_provider: ContextProvider | None = None,
    budget_provider: BudgetProvider | None = None,
    transport: Literal["stdio", "sse", "streamable-http"] = "stdio",
    name: str = "CapabilityHub",
    **transport_options: Any,
) -> None:
    """Run using an official SDK transport; no transport is implemented here."""

    if service is None:
        if context_provider is not None or budget_provider is not None:
            raise ValueError("context and budget providers require an explicit service")
        server = create_empty_mcp_server(name=name)
    else:
        if context_provider is None or budget_provider is None:
            raise ValueError("an explicit service requires context and budget providers")
        server = create_mcp_server(
            service,
            context_provider=context_provider,
            budget_provider=budget_provider,
            name=name,
        )
    server.run(transport=transport, **transport_options)


def create_empty_mcp_server(
    *,
    name: str = "CapabilityHub",
    home: Path | None = None,
    project: Path | None = None,
) -> MCPServer:
    """Create the safe local-discovery server used by the CLI entry point.

    It discovers inert Skill metadata, configured MCP server names, and project
    manifests. It never imports or executes discovered code. The random reference key
    and all state live only for this process.
    """

    catalog = discover_local_catalog(home=home, project=project)
    registry = CapabilityRegistry()
    registry.register_many(catalog.manifests)
    for manifest in catalog.manifests:
        try:
            registry.activate(manifest.identity.coordinate, manifest.identity.revision)
        except CapabilityHubError:
            # Invalid dependency/conflict closures stay installed but inactive.
            continue
    service = CapabilityHubService(
        registry=registry,
        providers=catalog.skill_providers,
        references=ReferenceSigner(secrets.token_bytes(32)),
        audit=MemoryAuditSink(),
    )
    context = ServiceContext("local", "anonymous", "mcp-stdio")
    ledgers: dict[str, BudgetLedger] = {}
    lock = RLock()

    def budget_provider(task_id: str) -> BudgetLedger:
        with lock:
            return ledgers.setdefault(
                task_id,
                BudgetLedger(
                    f"task:{task_id}",
                    {
                        "bytes": 1_000_000,
                        "executions": 0,
                        "loads": 100,
                        "portable_tokens": 100_000,
                    },
                ),
            )

    return create_mcp_server(
        service,
        context_provider=lambda: context,
        budget_provider=budget_provider,
        name=name,
    )


def _search(
    service: CapabilityHubService,
    context_provider: ContextProvider,
    budget_provider: BudgetProvider,
    *,
    query: str,
    task_id: str,
    kinds: list[str] | None,
    limit: int,
    max_output_tokens: int,
) -> dict[str, JsonValue]:
    response = service.search(
        query,
        task_id=task_id,
        context=context_provider(),
        budget=budget_provider(task_id),
        kinds=kinds,
        limit=limit,
        max_output_tokens=max_output_tokens,
    )
    return _search_dict(response)


def _load(
    service: CapabilityHubService,
    context_provider: ContextProvider,
    budget_provider: BudgetProvider,
    *,
    capability_ref: str,
    task_id: str,
    section_names: list[str] | None,
    operation_names: list[str] | None,
    max_output_tokens: int,
) -> dict[str, JsonValue]:
    loaded = service.load(
        capability_ref,
        task_id=task_id,
        context=context_provider(),
        budget=budget_provider(task_id),
        section_names=section_names,
        operation_names=operation_names,
        max_output_tokens=max_output_tokens,
    )
    return _loaded_dict(loaded)


def _execute(
    service: CapabilityHubService,
    context_provider: ContextProvider,
    budget_provider: BudgetProvider,
    *,
    execution_ref: str,
    operation: str,
    arguments: dict[str, JsonValue],
    task_id: str,
    approval_ref: str | None,
    idempotency_key: str | None,
    max_output_tokens: int | None,
) -> dict[str, JsonValue]:
    result = service.execute(
        ExecutionRequest(
            execution_ref=execution_ref,
            operation=operation,
            arguments=arguments,
            task_id=task_id,
            approval_ref=approval_ref,
            idempotency_key=idempotency_key,
        ),
        context=context_provider(),
        budget=budget_provider(task_id),
        max_output_tokens=max_output_tokens,
    )
    return {
        "audit_id": result.audit_id,
        "capability_revision": result.capability_revision,
        "operation": result.operation,
        "output": result.output,
        "portable_tokens": result.portable_tokens,
        "provider": result.provider,
    }


def _search_dict(response: SearchResponse) -> dict[str, JsonValue]:
    counts: dict[str, JsonValue] = {
        kind: count for kind, count in response.kind_counts.items()
    }
    return {
        "cards": [_card_dict(card) for card in response.cards],
        "kind_counts": counts,
        "payload_bytes": response.payload_bytes,
        "portable_tokens": response.portable_tokens,
        "total_matches": response.total_matches,
        "truncated": response.truncated,
    }


def _card_dict(card: SearchCard) -> dict[str, JsonValue]:
    return {
        "capability_ref": card.capability_ref,
        "estimated_load_tokens": card.estimated_load_tokens,
        "kind": card.kind.value,
        "match_reason": list(card.match_reason),
        "operations": list(card.operations),
        "revision": card.revision,
        "risk": card.risk.value,
        "summary": card.summary,
        "trust_tier": card.trust_tier,
    }


def _loaded_dict(loaded: LoadedCapability) -> dict[str, JsonValue]:
    return {
        "execution_ref": loaded.execution_ref,
        "omitted_sections": list(loaded.omitted_sections),
        "operations": [
            {
                "input_schema": dict(operation.input_schema),
                "name": operation.name,
                "operation_type": operation.operation_type.value,
                "output_schema": dict(operation.output_schema),
                "requires_approval": operation.requires_approval,
                "side_effect": operation.side_effect.value,
            }
            for operation in loaded.operations
        ],
        "permissions": list(loaded.permissions),
        "portable_tokens": loaded.portable_tokens,
        "revision": loaded.revision,
        "sections": [
            {
                "content": section.content,
                "media_type": section.media_type,
                "name": section.name,
                "portable_tokens": section.portable_tokens,
                "sensitive": section.sensitive,
            }
            for section in loaded.sections
        ],
    }


def _safe(call: Callable[[], _ResultT]) -> CallToolResult:
    try:
        result = call()
    except CapabilityHubError as error:
        envelope: dict[str, JsonValue] = {
            "error": {
                "category": error.category.value,
                "code": error.code,
                "retryable": error.retryable,
                "safe_message": error.safe_message,
            }
        }
        return _tool_result(envelope, is_error=True)
    except Exception:
        envelope = {
            "error": {
                "category": ErrorCategory.INTERNAL.value,
                "code": "mcp_adapter_internal_error",
                "retryable": False,
                "safe_message": "CapabilityHub could not complete the request.",
            }
        }
        return _tool_result(envelope, is_error=True)
    return _tool_result(result, is_error=False)


def _tool_result(data: dict[str, JsonValue], *, is_error: bool) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=canonical_json(data))],
        structured_content=data,
        is_error=is_error,
    )


def _task_id(value: str) -> str:
    if not value:
        raise CapabilityHubError(
            code="invalid_task_id",
            category=ErrorCategory.INPUT,
            safe_message="task_id must be non-empty.",
        )
    return value


def _json_object(value: dict[str, Any]) -> dict[str, JsonValue]:
    return {key: _json_value(item, depth=0) for key, item in value.items()}


def _json_value(value: Any, *, depth: int) -> JsonValue:
    if depth > 64:
        raise CapabilityHubError(
            code="invalid_arguments",
            category=ErrorCategory.INPUT,
            safe_message="Execution arguments exceed the supported nesting depth.",
        )
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise CapabilityHubError(
            code="invalid_arguments",
            category=ErrorCategory.INPUT,
            safe_message="Execution arguments must contain finite JSON numbers.",
        )
    if isinstance(value, list):
        return [_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item, depth=depth + 1) for key, item in value.items()}
    raise CapabilityHubError(
        code="invalid_arguments",
        category=ErrorCategory.INPUT,
        safe_message="Execution arguments must be JSON-compatible.",
    )
