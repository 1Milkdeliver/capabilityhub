"""Official MCP Python SDK v2 adapter for CapabilityHub's three meta-tools.

This module registers tools on :class:`mcp.server.MCPServer`; protocol framing,
sessions, and every transport remain the SDK's responsibility.
"""

from __future__ import annotations

import hashlib
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Literal, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, TextContent

from capabilityhub.audit import AuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.metering import canonical_json
from capabilityhub.observability import InMemoryObservability, SqliteMetricStore
from capabilityhub.protocol import AdapterKind, JsonValue, in_process_request
from capabilityhub.references import ReferenceSigner
from capabilityhub.secure_audit import (
    ResilientAuditSink,
    SecureAuditLedger,
    load_or_create_signing_key,
)
from capabilityhub.service import CapabilityHubService, ServiceContext
from capabilityhub.service_adapter import (
    BudgetProvider,
    CapabilityHubServiceAdapter,
    ContextProvider,
)

_ResultT = TypeVar("_ResultT", bound=dict[str, JsonValue])
MCP_CORRELATION_META_KEY = "capabilityhub/correlation_id"


@dataclass(frozen=True, slots=True)
class _MCPRuntimeState:
    service: CapabilityHubService
    inventory: dict[str, JsonValue] | None = None
    observability: InMemoryObservability | None = None


RuntimeStateProvider = Callable[[], _MCPRuntimeState]


def create_mcp_server(
    service: CapabilityHubService,
    *,
    context_provider: ContextProvider,
    budget_provider: BudgetProvider,
    name: str = "CapabilityHub",
    state_provider: RuntimeStateProvider | None = None,
) -> MCPServer:
    """Create an SDK-owned server exposing exactly search, load, and execute."""

    if not name:
        raise ValueError("name must be non-empty")
    current_state = state_provider or (lambda: _MCPRuntimeState(service))
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
        ctx: Context,
        kinds: list[str] | None = None,
        limit: int = 8,
        max_output_tokens: int = 900,
        include_inventory: bool = False,
        include_cards: bool = True,
    ) -> CallToolResult:
        """Search active capabilities within a hard output budget."""

        return _safe(
            lambda: _dispatch_mcp(
                current_state,
                context_provider,
                budget_provider,
                ctx,
                "capability.search",
                {
                    "query": query,
                    "task_id": _task_id(task_id),
                    "kinds": list(kinds) if kinds is not None else None,
                    "limit": limit,
                    "max_output_tokens": max_output_tokens,
                    "include_inventory": include_inventory,
                    "include_cards": include_cards,
                },
            )
        )

    @server.tool(
        name="capability.load",
        description="Load only selected capability sections and operation contracts.",
    )
    def load(
        capability_ref: str,
        task_id: str,
        ctx: Context,
        section_names: list[str] | None = None,
        operation_names: list[str] | None = None,
        max_output_tokens: int = 2_000,
    ) -> CallToolResult:
        """Resolve a scoped load reference and progressively disclose content."""

        return _safe(
            lambda: _dispatch_mcp(
                current_state,
                context_provider,
                budget_provider,
                ctx,
                "capability.load",
                {
                    "capability_ref": capability_ref,
                    "task_id": _task_id(task_id),
                    "section_names": (list(section_names) if section_names is not None else None),
                    "operation_names": (
                        list(operation_names) if operation_names is not None else None
                    ),
                    "max_output_tokens": max_output_tokens,
                },
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
        ctx: Context,
        approval_ref: str | None = None,
        idempotency_key: str | None = None,
        max_output_tokens: int | None = None,
    ) -> CallToolResult:
        """Verify, authorize, budget, and execute a loaded capability operation."""

        return _safe(
            lambda: _dispatch_mcp(
                current_state,
                context_provider,
                budget_provider,
                ctx,
                "capability.execute",
                {
                    "execution_ref": execution_ref,
                    "operation": operation,
                    "arguments": _json_object(arguments),
                    "task_id": _task_id(task_id),
                    "approval_ref": approval_ref,
                    "idempotency_key": idempotency_key,
                    "max_output_tokens": max_output_tokens,
                },
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
    project: Path | None = None,
    **transport_options: Any,
) -> None:
    """Run using an official SDK transport; no transport is implemented here."""

    if service is None:
        if context_provider is not None or budget_provider is not None:
            raise ValueError("context and budget providers require an explicit service")
        server = create_empty_mcp_server(name=name, project=project)
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


class _LocalRuntime:
    """Atomically refresh a read-only local catalog only when its fingerprint changes."""

    def __init__(
        self,
        *,
        home: Path | None,
        project: Path | None,
        refresh_interval_seconds: float = 0.25,
    ) -> None:
        self._monitor = LocalCatalogMonitor(
            home=home,
            project=project,
            refresh_interval_seconds=refresh_interval_seconds,
        )
        self._references = ReferenceSigner(secrets.token_bytes(32))
        self._audit = _local_audit_sink(self._monitor.project)
        self._observability = InMemoryObservability(
            allowed_error_codes=("other_error",),
            persistent_metrics=SqliteMetricStore(
                self._monitor.project / ".capabilityhub" / "state.sqlite3"
            ),
        )
        self._lock = RLock()
        self._state: _MCPRuntimeState | None = None

    def state(self) -> _MCPRuntimeState:
        with self._lock:
            generation = self._monitor.snapshot()
            current_generation = (
                self._state.inventory.get("generation")
                if self._state is not None and self._state.inventory is not None
                else None
            )
            next_generation = generation.inventory.get("generation")
            if self._state is None:
                service = CapabilityHubService(
                    registry=generation.registry,
                    providers=generation.providers,
                    references=self._references,
                    audit=self._audit,
                )
                self._state = _MCPRuntimeState(
                    service, generation.inventory_json(), self._observability
                )
            elif next_generation != current_generation:
                service = self._state.service.fork_catalog(
                    registry=generation.registry,
                    providers=generation.providers,
                )
                self._state = _MCPRuntimeState(
                    service, generation.inventory_json(), self._observability
                )
            elif generation.inventory != self._state.inventory:
                return _MCPRuntimeState(
                    self._state.service, generation.inventory_json(), self._observability
                )
            return self._state


def _local_audit_sink(project: Path) -> AuditSink:
    try:
        key = load_or_create_signing_key(project / ".capabilityhub" / "audit-hmac.key")
        return ResilientAuditSink(
            SecureAuditLedger(
                project / ".capabilityhub" / "secure-audit" / "current",
                signing_key=key,
            )
        )
    except Exception as error:
        code = error.code if isinstance(error, CapabilityHubError) else "secure_audit_unavailable"
        return ResilientAuditSink(None, initial_error=code)


def create_empty_mcp_server(
    *,
    name: str = "CapabilityHub",
    home: Path | None = None,
    project: Path | None = None,
    refresh_interval_seconds: float = 0.25,
) -> MCPServer:
    """Create the safe local-discovery server used by the CLI entry point.

    It discovers inert Skill metadata, configured MCP server names, and project
    manifests. It never imports or executes discovered code. The random reference key
    and all state live only for this process.
    """

    runtime = _LocalRuntime(
        home=home,
        project=project,
        refresh_interval_seconds=refresh_interval_seconds,
    )
    initial = runtime.state()
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
        initial.service,
        context_provider=lambda: context,
        budget_provider=budget_provider,
        name=name,
        state_provider=runtime.state,
    )


def _dispatch_mcp(
    state_provider: RuntimeStateProvider,
    context_provider: ContextProvider,
    budget_provider: BudgetProvider,
    context: Context,
    operation: str,
    payload: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    state = state_provider()
    adapter = CapabilityHubServiceAdapter(
        state.service,
        kind=AdapterKind.MCP,
        context_provider=context_provider,
        budget_provider=budget_provider,
        inventory_provider=lambda: state.inventory,
        observability=state.observability,
    )
    request_id, correlation_id = _mcp_identifiers(context)
    result = adapter.dispatch(
        in_process_request(
            AdapterKind.MCP,
            operation,
            payload,
            request_id=request_id,
            correlation_id=correlation_id,
            handshake=adapter.handshake,
        )
    )
    if not isinstance(result, dict):
        raise TypeError("service adapter returned a non-object result")
    return result


def _mcp_identifiers(context: Context) -> tuple[str, str]:
    try:
        wire_request_id = context.request_id
    except ValueError:
        wire_request_id = secrets.token_hex(16)
    request_id = "mcp-" + hashlib.sha256(wire_request_id.encode()).hexdigest()[:32]
    try:
        meta = context.request_context.meta
    except ValueError:
        meta = None
    supplied = meta.get(MCP_CORRELATION_META_KEY) if meta is not None else None
    if supplied is None:
        return request_id, request_id
    if (
        not isinstance(supplied, str)
        or not supplied
        or len(supplied) > 256
        or any(character.isspace() for character in supplied)
    ):
        raise CapabilityHubError(
            code="invalid_correlation_id",
            category=ErrorCategory.INPUT,
            safe_message="The MCP correlation identifier is invalid.",
        )
    return request_id, supplied


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
