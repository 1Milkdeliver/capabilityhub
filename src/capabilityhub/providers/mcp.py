"""Optional upstream MCP stdio provider using the official Python SDK."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import canonical_json, measure_text
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
)
from capabilityhub.providers.base import ProviderContext


@dataclass(frozen=True, slots=True)
class McpStdioFixture:
    manifest: CapabilityManifest
    command: Path
    args: tuple[str, ...]
    tools: Mapping[str, str]
    cwd: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        command = self.command.resolve()
        if not command.is_file():
            raise ValueError("MCP stdio command must be an existing absolute file")
        object.__setattr__(self, "command", command)
        if self.cwd is not None:
            cwd = self.cwd.resolve()
            if not cwd.is_dir():
                raise ValueError("MCP stdio working directory must be an existing directory")
            object.__setattr__(self, "cwd", cwd)
        declared = {operation.name for operation in self.manifest.operations}
        if not self.tools or not set(self.tools).issubset(declared):
            raise ValueError("MCP tools must be non-empty and declared by the manifest")
        if any(not tool for tool in self.tools.values()):
            raise ValueError("MCP upstream tool names must not be empty")


class McpStdioProvider:
    """Start one approved stdio server and invoke one mapped upstream tool per call."""

    def __init__(
        self,
        fixtures: tuple[McpStdioFixture, ...] | list[McpStdioFixture],
        *,
        name: str = "mcp-stdio",
    ) -> None:
        if not name:
            raise ValueError("MCP provider name must not be empty")
        self._name = name
        values = tuple(fixtures)
        revisions = [fixture.manifest.identity.revision for fixture in values]
        if len(revisions) != len(set(revisions)):
            raise ValueError("MCP fixture revisions must be unique")
        if any(fixture.manifest.provider != name for fixture in values):
            raise ValueError("MCP manifest provider must match the configured provider name")
        self._fixtures = values
        self._by_revision = {fixture.manifest.identity.revision: fixture for fixture in values}

    @property
    def name(self) -> str:
        return self._name

    def discover(self) -> tuple[CapabilityManifest, ...]:
        return tuple(fixture.manifest for fixture in self._fixtures)

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        fixture = self._by_revision.get(identity.revision)
        if fixture is None:
            raise _error(
                "mcp_capability_not_found",
                ErrorCategory.REFERENCE,
                "The requested MCP capability is not configured.",
            )
        tool = fixture.tools.get(request.operation)
        if tool is None:
            raise _error(
                "mcp_operation_not_found",
                ErrorCategory.REFERENCE,
                "The requested MCP operation is not allowlisted.",
            )
        try:
            output = anyio.run(
                _call_tool,
                fixture,
                tool,
                dict(request.arguments),
                context.deadline_ms / 1000,
            )
        except CapabilityHubError:
            raise
        except TimeoutError as error:
            raise _error(
                "mcp_deadline_exceeded",
                ErrorCategory.TIMEOUT,
                "The MCP tool call exceeded its deadline.",
                retryable=True,
            ) from error
        except Exception as error:
            nested = _find_hub_error(error)
            if nested is not None:
                raise nested from error
            if _contains_timeout(error):
                raise _error(
                    "mcp_deadline_exceeded",
                    ErrorCategory.TIMEOUT,
                    "The MCP tool call exceeded its deadline.",
                    retryable=True,
                ) from error
            raise _error(
                "mcp_call_failed",
                ErrorCategory.PROVIDER,
                "The MCP server or tool call failed.",
                retryable=True,
            ) from error
        serialized = canonical_json(output)
        measurement = measure_text(serialized)
        if measurement.portable_tokens > context.max_output_tokens:
            raise _error(
                "mcp_output_budget_exceeded",
                ErrorCategory.BUDGET,
                "The MCP result exceeded the hard output budget.",
            )
        audit_material = canonical_json(
            {
                "operation": request.operation,
                "revision": identity.revision,
                "task_id": request.task_id,
                "tool": tool,
            }
        ).encode()
        return ExecutionResult(
            capability_revision=identity.revision,
            operation=request.operation,
            output=output,
            provider=self.name,
            portable_tokens=measurement.portable_tokens,
            audit_id=f"mcp-{hashlib.sha256(audit_material).hexdigest()[:16]}",
        )


async def _call_tool(
    fixture: McpStdioFixture,
    tool: str,
    arguments: dict[str, JsonValue],
    timeout_seconds: float,
) -> JsonValue:
    parameters = StdioServerParameters(
        command=str(fixture.command),
        args=list(fixture.args),
        cwd=fixture.cwd,
        env=dict(fixture.environment),
        encoding_error_handler="replace",
    )
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        with anyio.fail_after(timeout_seconds):
            async with stdio_client(parameters, errlog=errlog) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=timeout_seconds
                ) as session:
                    await session.initialize()
                    advertised = await session.list_tools()
                    if tool not in {item.name for item in advertised.tools}:
                        raise _error(
                            "mcp_tool_not_advertised",
                            ErrorCategory.PROVIDER,
                            "The configured MCP tool was not advertised by the server.",
                        )
                    result = await session.call_tool(
                        tool,
                        cast(dict[str, Any], arguments),
                        read_timeout_seconds=timeout_seconds,
                    )
    if not isinstance(result, CallToolResult):
        raise _error(
            "mcp_result_incomplete",
            ErrorCategory.PROVIDER,
            "The MCP tool did not return a complete result.",
        )
    if result.is_error:
        raise _error(
            "mcp_tool_error",
            ErrorCategory.PROVIDER,
            "The MCP tool reported an error.",
        )
    if result.structured_content is not None:
        return _json_value(result.structured_content)
    return _json_value(
        [item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in result.content]
    )


def _json_value(value: Any) -> JsonValue:
    try:
        return cast(JsonValue, json.loads(json.dumps(value, ensure_ascii=False)))
    except (TypeError, ValueError) as error:
        raise _error(
            "mcp_result_not_json",
            ErrorCategory.PROVIDER,
            "The MCP tool result is not JSON serializable.",
        ) from error


def _find_hub_error(error: BaseException) -> CapabilityHubError | None:
    if isinstance(error, CapabilityHubError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            found = _find_hub_error(nested)
            if found is not None:
                return found
    return None


def _contains_timeout(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    return isinstance(error, BaseExceptionGroup) and any(
        _contains_timeout(nested) for nested in error.exceptions
    )


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    retryable: bool = False,
) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=category,
        safe_message=message,
        retryable=retryable,
    )
