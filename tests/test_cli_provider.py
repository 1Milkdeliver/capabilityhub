from __future__ import annotations

import sys
from pathlib import Path

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    OperationSpec,
    OperationType,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.cli import CliInvocation, CliProcessFixture, CliProcessProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext


def _manifest() -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity("test", "python-cli", "1", "sha256:" + "a" * 64),
        kind=CapabilityKind.CLI,
        summary="Allowlisted Python CLI fixture",
        provider="cli-process",
        operations=(OperationSpec("echo", OperationType.EXECUTE),),
    )


def _context(*, deadline_ms: int = 2_000, max_output_tokens: int = 200) -> ProviderContext:
    return ProviderContext("tenant", "principal", "session", deadline_ms, max_output_tokens)


def _request(value: str = "hello") -> ExecutionRequest:
    return ExecutionRequest("unused", "echo", {"value": value}, "task")


def test_cli_provider_uses_fixed_argv_without_a_shell() -> None:
    manifest = _manifest()
    code = "import json,sys; print(json.dumps({'value':sys.argv[1]}))"
    provider = CliProcessProvider(
        [
            CliProcessFixture(
                manifest,
                Path(sys.executable),
                {"echo": CliInvocation(("-c", code, "{value}"))},
            )
        ]
    )
    shell_like = "; echo SHOULD-NOT-RUN && $env:SECRET"

    result = provider.execute(manifest.identity, _request(shell_like), _context())

    assert result.output == {"value": shell_like}
    assert result.provider == "cli-process"
    assert provider.discover() == (manifest,)


def test_cli_provider_enforces_deadline() -> None:
    manifest = _manifest()
    provider = CliProcessProvider(
        [
            CliProcessFixture(
                manifest,
                Path(sys.executable),
                {"echo": CliInvocation(("-c", "import time; time.sleep(1)", "{value}"))},
            )
        ]
    )

    with pytest.raises(CapabilityHubError) as raised:
        provider.execute(manifest.identity, _request(), _context(deadline_ms=30))

    assert raised.value.category is ErrorCategory.TIMEOUT
    assert raised.value.code == "cli_deadline_exceeded"


def test_cli_provider_redacts_stderr_and_rejects_nonzero_exit() -> None:
    manifest = _manifest()
    provider = CliProcessProvider(
        [
            CliProcessFixture(
                manifest,
                Path(sys.executable),
                {
                    "echo": CliInvocation(
                        ("-c", "import sys; sys.stderr.write('SECRET'); raise SystemExit(7)")
                    )
                },
            )
        ]
    )

    with pytest.raises(CapabilityHubError) as raised:
        provider.execute(manifest.identity, _request(), _context())

    assert raised.value.details == {"exit_code": 7}
    assert "SECRET" not in str(raised.value.as_dict())


def test_cli_provider_rejects_partial_placeholders_and_missing_scalars() -> None:
    with pytest.raises(ValueError, match="complete argv token"):
        CliInvocation(("--value={value}",))

    manifest = _manifest()
    provider = CliProcessProvider(
        [
            CliProcessFixture(
                manifest,
                Path(sys.executable),
                {"echo": CliInvocation(("-c", "print('{}')", "{value}"))},
            )
        ]
    )
    request = ExecutionRequest("unused", "echo", {}, "task")

    with pytest.raises(CapabilityHubError) as raised:
        provider.execute(manifest.identity, request, _context())

    assert raised.value.code == "cli_argument_missing"


def test_cli_provider_runs_through_search_load_and_execute_admission() -> None:
    manifest = _manifest()
    code = "import json,sys; print(json.dumps({'value':sys.argv[1]}))"
    provider = CliProcessProvider(
        [
            CliProcessFixture(
                manifest,
                Path(sys.executable),
                {"echo": CliInvocation(("-c", code, "{value}"))},
            )
        ]
    )
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    signer = ReferenceSigner(b"cli-provider-integration-secret")
    service = CapabilityHubService(
        registry=registry,
        providers=[provider],
        references=signer,
        audit=MemoryAuditSink(),
    )
    context = ServiceContext("tenant", "principal", "session")
    budget = BudgetLedger(
        "task", {"bytes": 10_000, "executions": 2, "loads": 2, "portable_tokens": 2_000}
    )

    card = service.search("python cli", task_id="task", context=context, budget=budget).cards[0]
    loaded = service.load(card.capability_ref, task_id="task", context=context, budget=budget)
    result = service.execute(
        ExecutionRequest(loaded.execution_ref, "echo", {"value": "admitted"}, "task"),
        context=context,
        budget=budget,
        max_output_tokens=200,
    )

    assert result.output == {"value": "admitted"}
