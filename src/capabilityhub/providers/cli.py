"""Allowlisted, shell-free local CLI provider."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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

_PLACEHOLDER = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_PARTIAL_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


@dataclass(frozen=True, slots=True)
class CliInvocation:
    """One pre-approved argv template; placeholders occupy complete argv tokens."""

    argv: tuple[str, ...]
    output: Literal["json", "text"] = "json"

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("CLI invocation argv must not be empty")
        if self.output not in {"json", "text"}:
            raise ValueError("CLI invocation output must be json or text")
        for token in self.argv:
            if _PARTIAL_PLACEHOLDER.search(token) and _PLACEHOLDER.fullmatch(token) is None:
                raise ValueError("CLI placeholders must occupy a complete argv token")


@dataclass(frozen=True, slots=True)
class CliProcessFixture:
    manifest: CapabilityManifest
    executable: Path
    operations: Mapping[str, CliInvocation] = field(default_factory=dict)
    cwd: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        executable = self.executable.resolve()
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("CLI executable must be an existing absolute file")
        object.__setattr__(self, "executable", executable)
        if self.cwd is not None:
            cwd = self.cwd.resolve()
            if not cwd.is_dir():
                raise ValueError("CLI working directory must be an existing directory")
            object.__setattr__(self, "cwd", cwd)
        declared = {operation.name for operation in self.manifest.operations}
        if not self.operations or not set(self.operations).issubset(declared):
            raise ValueError("CLI operations must be non-empty and declared by the manifest")


class CliProcessProvider:
    """Execute only operator-supplied absolute executables without a command shell."""

    def __init__(
        self,
        fixtures: tuple[CliProcessFixture, ...] | list[CliProcessFixture],
        *,
        name: str = "cli-process",
    ) -> None:
        if not name:
            raise ValueError("CLI provider name must not be empty")
        self._name = name
        values = tuple(fixtures)
        revisions = [fixture.manifest.identity.revision for fixture in values]
        if len(revisions) != len(set(revisions)):
            raise ValueError("CLI fixture revisions must be unique")
        if any(fixture.manifest.provider != name for fixture in values):
            raise ValueError("CLI manifest provider must match the configured provider name")
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
                "cli_capability_not_found",
                ErrorCategory.REFERENCE,
                "The requested CLI capability is not configured.",
            )
        invocation = fixture.operations.get(request.operation)
        if invocation is None:
            raise _error(
                "cli_operation_not_found",
                ErrorCategory.REFERENCE,
                "The requested CLI operation is not allowlisted.",
            )
        argv = [str(fixture.executable), *_render(invocation.argv, request.arguments)]
        try:
            completed = subprocess.run(
                argv,
                cwd=fixture.cwd,
                env=dict(fixture.environment),
                shell=False,
                check=False,
                capture_output=True,
                timeout=context.deadline_ms / 1000,
            )
        except subprocess.TimeoutExpired as error:
            raise _error(
                "cli_deadline_exceeded",
                ErrorCategory.TIMEOUT,
                "The CLI operation exceeded its deadline.",
                retryable=True,
            ) from error
        except OSError as error:
            raise _error(
                "cli_start_failed",
                ErrorCategory.PROVIDER,
                "The CLI process could not be started.",
            ) from error
        if completed.returncode != 0:
            raise _error(
                "cli_nonzero_exit",
                ErrorCategory.PROVIDER,
                "The CLI operation failed.",
                details={"exit_code": completed.returncode},
            )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        measurement = measure_text(stdout)
        if measurement.portable_tokens > context.max_output_tokens:
            raise _error(
                "cli_output_budget_exceeded",
                ErrorCategory.BUDGET,
                "The CLI output exceeded the hard output budget.",
            )
        output = _parse_output(stdout, invocation.output)
        audit_material = canonical_json(
            {
                "arguments": dict(request.arguments),
                "operation": request.operation,
                "revision": identity.revision,
                "task_id": request.task_id,
            }
        ).encode()
        return ExecutionResult(
            capability_revision=identity.revision,
            operation=request.operation,
            output=output,
            provider=self.name,
            portable_tokens=measurement.portable_tokens,
            audit_id=f"cli-{hashlib.sha256(audit_material).hexdigest()[:16]}",
        )


def _render(template: tuple[str, ...], arguments: Mapping[str, JsonValue]) -> tuple[str, ...]:
    rendered: list[str] = []
    for token in template:
        placeholder = _PLACEHOLDER.fullmatch(token)
        if placeholder is not None:
            key = placeholder.group(1)
            value = arguments.get(key)
            if not isinstance(value, (str, int, float, bool)):
                raise _error(
                    "cli_argument_missing",
                    ErrorCategory.INPUT,
                    "A required scalar CLI argument is missing.",
                    details={"argument": key},
                )
            rendered.append(str(value).lower() if isinstance(value, bool) else str(value))
        else:
            rendered.append(token)
    return tuple(rendered)


def _parse_output(stdout: str, output_type: str) -> JsonValue:
    if output_type == "text":
        return {"stdout": stdout}
    try:
        output = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise _error(
            "cli_invalid_json",
            ErrorCategory.PROVIDER,
            "The CLI returned invalid JSON.",
        ) from error
    return output  # type: ignore[no-any-return]


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, object] | None = None,
) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=category,
        safe_message=message,
        retryable=retryable,
        details=details or {},
    )
