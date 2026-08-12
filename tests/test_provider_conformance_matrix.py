from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar, Protocol

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    LoadedCapability,
    OperationSpec,
    OperationType,
    SideEffect,
)
from capabilityhub.protocol import AdapterKind, RequestEnvelope, parse_request
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.cli import CliInvocation, CliProcessFixture, CliProcessProvider
from capabilityhub.providers.http import HttpApiFixture, HttpApiProvider, HttpInvocation
from capabilityhub.providers.rag import LocalRagFixture, LocalRagProvider
from capabilityhub.providers.skill import SkillProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext
from capabilityhub.service_adapter import CapabilityHubServiceAdapter

CANARY = "SECRET-CONFORMANCE-CANARY-7f31"
CASE_NAMES = ("skill", "cli", "api", "rag", "mcp")


class MatrixProvider(Protocol):
    @property
    def name(self) -> str: ...

    def discover(self) -> tuple[CapabilityManifest, ...]: ...

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult: ...


@dataclass(frozen=True, slots=True)
class ProviderCase:
    name: str
    kind: CapabilityKind
    provider: MatrixProvider
    manifest: CapabilityManifest
    support: str
    success_request: ExecutionRequest | None
    unsupported_code: str
    failure_request: ExecutionRequest
    failure_code: str
    budget_request: ExecutionRequest
    budget_mode: str
    budget_code: str | None
    revision_mismatch_code: str | None


class _ApiHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).requests.append(self.path)
        if self.path == "/failure":
            self._send(500, {"detail": CANARY})
        elif self.path == "/large":
            self._send(200, {"value": "x" * 2_000})
        else:
            self._send(200, {"value": "ok"})

    def _send(self, status: int, value: object) -> None:
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _api_server() -> Iterator[str]:
    _ApiHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(scope="module")
def provider_matrix(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, ProviderCase]]:
    root = tmp_path_factory.mktemp("provider-matrix")
    with _api_server() as base_url:
        cases = {
            "skill": _skill_case(root / "skill-root"),
            "cli": _cli_case(),
            "api": _api_case(base_url),
            "rag": _rag_case(root / "rag-root", root / "rag-secret.txt"),
        }
        if importlib.util.find_spec("mcp") is not None:
            cases["mcp"] = _mcp_case(root / "mcp-root")
        yield cases


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_matrix_discovers_and_loads_exact_revision(
    provider_matrix: dict[str, ProviderCase], case_name: str
) -> None:
    case = _case(provider_matrix, case_name)

    assert case.provider.discover() == (case.manifest,)
    loaded = _load(case)

    assert loaded.revision == case.manifest.identity.revision
    assert tuple(operation.name for operation in loaded.operations) == tuple(
        operation.name for operation in case.manifest.operations
    )
    assert bool(loaded.execution_ref) is (case.kind is not CapabilityKind.SKILL)


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_matrix_success_or_explicit_load_only_behavior(
    provider_matrix: dict[str, ProviderCase], case_name: str
) -> None:
    case = _case(provider_matrix, case_name)
    if case.success_request is None:
        error = _provider_error(case, case.failure_request)
        assert case.support == "load-only; execute=unsupported"
        assert error.code == "skill_execution_not_supported"
        return

    result = case.provider.execute(case.manifest.identity, case.success_request, _context())

    assert result.capability_revision == case.manifest.identity.revision
    assert result.provider == case.provider.name
    if case.kind is CapabilityKind.RAG:
        assert isinstance(result.output, dict)
        citations = result.output["results"]
        assert isinstance(citations, list) and citations
        first = citations[0]
        assert isinstance(first, dict)
        citation = first["citation"]
        assert isinstance(citation, dict)
        assert citation["path"] == "guide.md"


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_matrix_denies_unadvertised_operation(
    provider_matrix: dict[str, ProviderCase], case_name: str
) -> None:
    case = _case(provider_matrix, case_name)
    request = ExecutionRequest("unused", "not-allowlisted", {}, "task")

    error = _provider_error(case, request)

    assert error.code == case.unsupported_code


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_matrix_normalizes_provider_failure_without_secret_leak(
    provider_matrix: dict[str, ProviderCase], case_name: str
) -> None:
    case = _case(provider_matrix, case_name)

    error = _provider_error(case, case.failure_request)

    assert error.code == case.failure_code
    assert CANARY not in str(error.as_dict())


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_matrix_revision_mismatch_is_denied_or_explicitly_not_applicable(
    provider_matrix: dict[str, ProviderCase], case_name: str
) -> None:
    case = _case(provider_matrix, case_name)
    if case.revision_mismatch_code is None:
        assert case.kind is CapabilityKind.SKILL
        assert case.support == "load-only; execute=unsupported"
        return
    changed = replace(case.manifest.identity, digest="sha256:" + "9" * 64)
    request = case.success_request or case.failure_request

    error = _provider_error(case, request, identity=changed)

    assert error.code == case.revision_mismatch_code


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_matrix_budget_is_hard_denial_or_bounded_truncation(
    provider_matrix: dict[str, ProviderCase], case_name: str
) -> None:
    case = _case(provider_matrix, case_name)
    context = _context(max_output_tokens=20 if case.budget_mode == "truncate" else 2)
    if case.budget_mode == "truncate":
        result = case.provider.execute(case.manifest.identity, case.budget_request, context)
        assert result.portable_tokens <= context.max_output_tokens
        assert isinstance(result.output, dict)
        assert result.output["truncated"] is True
        return

    error = _provider_error(case, case.budget_request, context=context)
    assert error.code == case.budget_code
    assert CANARY not in str(error.as_dict())


def test_matrix_declares_each_kind_and_unsupported_surface(
    provider_matrix: dict[str, ProviderCase],
) -> None:
    if "mcp" not in provider_matrix:
        pytest.skip("official MCP SDK is not installed")
    assert {case.kind for case in provider_matrix.values()} == set(CapabilityKind)
    assert {name: case.support for name, case in provider_matrix.items()} == {
        "skill": "load-only; execute=unsupported",
        "cli": "execute",
        "api": "execute",
        "rag": "retrieve; generic execute=unsupported",
        "mcp": "execute",
    }


def test_one_mcp_meta_tool_chain_loads_all_kinds_and_invokes_supported_operations(
    provider_matrix: dict[str, ProviderCase],
) -> None:
    """Prove the same three meta-tool envelopes cover every real provider kind."""

    if "mcp" not in provider_matrix:
        pytest.skip("official MCP SDK is not installed")
    registry = CapabilityRegistry()
    for case in provider_matrix.values():
        registry.register(case.manifest)
        registry.activate(case.manifest.identity.coordinate, case.manifest.identity.revision)
    signer = ReferenceSigner(b"five-kind-meta-tool-chain")
    service = CapabilityHubService(
        registry=registry,
        providers=tuple(case.provider for case in provider_matrix.values()),
        references=signer,
        audit=MemoryAuditSink(),
    )
    budgets: dict[str, BudgetLedger] = {}
    adapter = CapabilityHubServiceAdapter(
        service,
        kind=AdapterKind.MCP,
        context_provider=lambda: ServiceContext("tenant", "principal", "meta-session"),
        budget_provider=lambda task_id: budgets.setdefault(
            task_id,
            BudgetLedger(
                task_id,
                {
                    "bytes": 500_000,
                    "loads": 5,
                    "executions": 5,
                    "portable_tokens": 100_000,
                },
            ),
        ),
    )

    for case in provider_matrix.values():
        task_id = f"meta-{case.name}"
        searched = adapter.dispatch(
            _adapter_request(
                adapter,
                "capability.search",
                {"query": case.name, "task_id": task_id, "kinds": [case.kind.value]},
            )
        )
        assert isinstance(searched, dict)
        cards = searched["cards"]
        assert isinstance(cards, list) and len(cards) == 1
        card = cards[0]
        assert isinstance(card, dict)
        loaded = adapter.dispatch(
            _adapter_request(
                adapter,
                "capability.load",
                {
                    "capability_ref": card["capability_ref"],
                    "task_id": task_id,
                    "operation_names": [item.name for item in case.manifest.operations],
                },
            )
        )
        assert isinstance(loaded, dict)
        assert loaded["revision"] == case.manifest.identity.revision
        if case.success_request is None:
            assert loaded["execution_ref"] == ""
            continue
        executed = adapter.dispatch(
            _adapter_request(
                adapter,
                "capability.execute",
                {
                    "execution_ref": loaded["execution_ref"],
                    "operation": case.success_request.operation,
                    "arguments": dict(case.success_request.arguments),
                    "task_id": task_id,
                },
            )
        )
        assert isinstance(executed, dict)
        assert executed["capability_revision"] == case.manifest.identity.revision


def _adapter_request(
    adapter: CapabilityHubServiceAdapter,
    operation: str,
    payload: dict[str, object],
) -> RequestEnvelope:
    handshake = adapter.handshake
    return parse_request(
        AdapterKind.MCP,
        {
            "request_id": f"request-{operation}-{payload['task_id']}",
            "correlation_id": f"correlation-{payload['task_id']}",
            "operation": operation,
            "payload": payload,
            "handshake": {
                "api_versions": list(handshake.api_versions),
                "supported_features": list(handshake.supported_features),
                "required_features": list(handshake.required_features),
            },
        },
        server_handshake=handshake,
    )


def _case(matrix: dict[str, ProviderCase], name: str) -> ProviderCase:
    if name == "mcp" and name not in matrix:
        pytest.skip("official MCP SDK is not installed")
    return matrix[name]


def _load(case: ProviderCase) -> LoadedCapability:
    registry = CapabilityRegistry()
    registry.register(case.manifest)
    registry.activate(case.manifest.identity.coordinate, case.manifest.identity.revision)
    signer = ReferenceSigner(b"provider-conformance-load")
    service = CapabilityHubService(
        registry=registry,
        providers=(case.provider,),
        references=signer,
        audit=MemoryAuditSink(),
    )
    context = ServiceContext("tenant", "principal", "matrix-session")
    reference = signer.issue(
        revision=case.manifest.identity.revision,
        scope=context.reference_scope,
        purpose="load",
        ttl_seconds=300,
    )
    budget = BudgetLedger(
        f"load-{case.name}",
        {"bytes": 100_000, "loads": 2, "portable_tokens": 20_000},
    )
    return service.load(reference, task_id="task", context=context, budget=budget)


def _provider_error(
    case: ProviderCase,
    request: ExecutionRequest,
    *,
    identity: CapabilityIdentity | None = None,
    context: ProviderContext | None = None,
) -> CapabilityHubError:
    with pytest.raises(CapabilityHubError) as caught:
        case.provider.execute(
            identity or case.manifest.identity,
            request,
            context or _context(),
        )
    return caught.value


def _context(*, max_output_tokens: int = 500) -> ProviderContext:
    return ProviderContext("tenant", "principal", "session", 5_000, max_output_tokens)


def _manifest(
    name: str,
    kind: CapabilityKind,
    provider: str,
    operations: tuple[str, ...],
    digest_character: str,
) -> CapabilityManifest:
    operation_type = OperationType.RETRIEVE if kind is CapabilityKind.RAG else OperationType.EXECUTE
    return CapabilityManifest(
        CapabilityIdentity("matrix", name, "1.0.0", "sha256:" + digest_character * 64),
        kind,
        f"Conformance fixture for {kind.value}",
        provider,
        tuple(
            OperationSpec(
                operation,
                operation_type,
                side_effect=SideEffect.READ,
            )
            for operation in operations
        ),
    )


def _skill_case(root: Path) -> ProviderCase:
    package = root / "safe-skill"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: matrix-skill\nversion: 1.0.0\n---\nSafe instructions only.\n",
        encoding="utf-8",
    )
    (package / "script.py").write_text(f"raise RuntimeError({CANARY!r})", encoding="utf-8")
    provider = SkillProvider((root,))
    manifest = provider.discover()[0]
    request = ExecutionRequest("unused", "load", {}, "task")
    return ProviderCase(
        "skill",
        CapabilityKind.SKILL,
        provider,
        manifest,
        "load-only; execute=unsupported",
        None,
        "skill_execution_not_supported",
        request,
        "skill_execution_not_supported",
        request,
        "unsupported",
        "skill_execution_not_supported",
        None,
    )


def _cli_case() -> ProviderCase:
    manifest = _manifest(
        "cli", CapabilityKind.CLI, "cli-process", ("echo", "failure", "large"), "a"
    )
    provider = CliProcessProvider(
        (
            CliProcessFixture(
                manifest,
                Path(sys.executable),
                {
                    "echo": CliInvocation(("-c", "import json;print(json.dumps({'ok':True}))")),
                    "failure": CliInvocation(
                        ("-c", f"import sys;sys.stderr.write({CANARY!r});raise SystemExit(7)")
                    ),
                    "large": CliInvocation(
                        ("-c", "import json;print(json.dumps({'value':'x'*2000}))")
                    ),
                },
            ),
        )
    )
    return ProviderCase(
        "cli",
        CapabilityKind.CLI,
        provider,
        manifest,
        "execute",
        ExecutionRequest("unused", "echo", {}, "task"),
        "cli_operation_not_found",
        ExecutionRequest("unused", "failure", {}, "task"),
        "cli_nonzero_exit",
        ExecutionRequest("unused", "large", {}, "task"),
        "deny",
        "cli_output_budget_exceeded",
        "cli_capability_not_found",
    )


def _api_case(base_url: str) -> ProviderCase:
    manifest = _manifest("api", CapabilityKind.API, "http-api", ("echo", "failure", "large"), "b")
    provider = HttpApiProvider(
        (
            HttpApiFixture(
                manifest,
                base_url,
                {
                    "echo": HttpInvocation("GET", "/echo"),
                    "failure": HttpInvocation("GET", "/failure"),
                    "large": HttpInvocation("GET", "/large"),
                },
            ),
        )
    )
    return ProviderCase(
        "api",
        CapabilityKind.API,
        provider,
        manifest,
        "execute",
        ExecutionRequest("unused", "echo", {}, "task"),
        "http_operation_not_found",
        ExecutionRequest("unused", "failure", {}, "task"),
        "http_response_error",
        ExecutionRequest("unused", "large", {}, "task"),
        "deny",
        "http_output_budget_exceeded",
        "http_capability_not_found",
    )


def _rag_case(root: Path, outside_secret: Path) -> ProviderCase:
    root.mkdir()
    (root / "guide.md").write_text("needle retrieval result\n" * 40, encoding="utf-8")
    outside_secret.write_text(CANARY, encoding="utf-8")
    manifest = _manifest("rag", CapabilityKind.RAG, "local-rag", ("retrieve",), "c")
    provider = LocalRagProvider((LocalRagFixture(manifest, root, chunk_lines=1),))
    return ProviderCase(
        "rag",
        CapabilityKind.RAG,
        provider,
        manifest,
        "retrieve; generic execute=unsupported",
        ExecutionRequest("unused", "retrieve", {"query": "needle", "top_k": 1}, "task"),
        "rag_operation_not_found",
        ExecutionRequest("unused", "retrieve", {"query": ""}, "task"),
        "rag_query_invalid",
        ExecutionRequest("unused", "retrieve", {"query": "needle", "top_k": 20}, "task"),
        "truncate",
        None,
        "rag_capability_not_found",
    )


def _mcp_case(root: Path) -> ProviderCase:
    from capabilityhub.providers.mcp import McpStdioFixture, McpStdioProvider

    root.mkdir()
    script = root / "server.py"
    script.write_text(
        f"""from mcp.server import MCPServer

server = MCPServer("matrix")

@server.tool(name="echo", description="Echo")
def echo() -> dict[str, object]:
    return {{"ok": True}}

@server.tool(name="failure", description="Fail safely")
def failure() -> dict[str, object]:
    raise RuntimeError({CANARY!r})

@server.tool(name="large", description="Large output")
def large() -> dict[str, object]:
    return {{"value": "x" * 2000}}

if __name__ == "__main__":
    server.run(transport="stdio")
""",
        encoding="utf-8",
    )
    manifest = _manifest("mcp", CapabilityKind.MCP, "mcp-stdio", ("echo", "failure", "large"), "d")
    provider = McpStdioProvider(
        (
            McpStdioFixture(
                manifest,
                Path(sys.executable),
                (script.as_posix(),),
                {"echo": "echo", "failure": "failure", "large": "large"},
                cwd=root,
            ),
        )
    )
    return ProviderCase(
        "mcp",
        CapabilityKind.MCP,
        provider,
        manifest,
        "execute",
        ExecutionRequest("unused", "echo", {}, "task"),
        "mcp_operation_not_found",
        ExecutionRequest("unused", "failure", {}, "task"),
        "mcp_tool_error",
        ExecutionRequest("unused", "large", {}, "task"),
        "deny",
        "mcp_output_budget_exceeded",
        "mcp_capability_not_found",
    )
