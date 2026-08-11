from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

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
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.http import HttpApiFixture, HttpApiProvider, HttpInvocation
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext


class _Handler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, object]]] = []

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        type(self).requests.append(
            {
                "authorization": self.headers.get("Authorization"),
                "path": parsed.path,
                "query": parse_qs(parsed.query),
            }
        )
        if parsed.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/should-not-follow")
            self.end_headers()
            return
        payload = json.dumps(type(self).requests[-1]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _server() -> Iterator[str]:
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _manifest() -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity("test", "http-api", "1", "sha256:" + "b" * 64),
        kind=CapabilityKind.API,
        summary="Fixed-origin HTTP API fixture",
        provider="http-api",
        operations=(
            OperationSpec("find", OperationType.EXECUTE),
            OperationSpec("redirect", OperationType.EXECUTE),
        ),
    )


def _context(*, max_output_tokens: int = 200) -> ProviderContext:
    return ProviderContext("tenant", "principal", "session", 2_000, max_output_tokens)


def test_http_provider_pins_origin_encodes_arguments_and_gets_headers_out_of_band() -> None:
    with _server() as base_url:
        manifest = _manifest()
        provider = HttpApiProvider(
            [
                HttpApiFixture(
                    manifest,
                    base_url,
                    {"find": HttpInvocation("GET", "/items/{item}", query={"search": "q"})},
                    headers=lambda: {"Authorization": "Bearer SECRET"},
                )
            ]
        )
        request = ExecutionRequest(
            "unused", "find", {"item": "a/b", "search": "hello world"}, "task"
        )

        result = provider.execute(manifest.identity, request, _context())

    assert result.output == {
        "authorization": "Bearer SECRET",
        "path": "/items/a%2Fb",
        "query": {"q": ["hello world"]},
    }
    assert "SECRET" not in result.audit_id


def test_http_provider_rejects_redirects_instead_of_following_them() -> None:
    with _server() as base_url:
        manifest = _manifest()
        provider = HttpApiProvider(
            [
                HttpApiFixture(
                    manifest,
                    base_url,
                    {"redirect": HttpInvocation("GET", "/redirect")},
                )
            ]
        )

        with pytest.raises(CapabilityHubError) as raised:
            provider.execute(
                manifest.identity,
                ExecutionRequest("unused", "redirect", {}, "task"),
                _context(),
            )

    assert raised.value.code == "http_response_error"
    assert raised.value.details == {"status": 302}
    assert [item["path"] for item in _Handler.requests] == ["/redirect"]


def test_http_provider_enforces_response_budget_before_json_parsing() -> None:
    with _server() as base_url:
        manifest = _manifest()
        provider = HttpApiProvider(
            [
                HttpApiFixture(
                    manifest,
                    base_url,
                    {"find": HttpInvocation("GET", "/items/{item}")},
                )
            ]
        )

        with pytest.raises(CapabilityHubError) as raised:
            provider.execute(
                manifest.identity,
                ExecutionRequest("unused", "find", {"item": "x" * 100}, "task"),
                _context(max_output_tokens=2),
            )

    assert raised.value.code == "http_output_budget_exceeded"


def test_http_provider_runs_through_search_load_and_execute_admission() -> None:
    with _server() as base_url:
        manifest = _manifest()
        provider = HttpApiProvider(
            [
                HttpApiFixture(
                    manifest,
                    base_url,
                    {"find": HttpInvocation("GET", "/items/{item}")},
                )
            ]
        )
        registry = CapabilityRegistry()
        registry.register(manifest)
        registry.activate(manifest.identity.coordinate, manifest.identity.revision)
        service = CapabilityHubService(
            registry=registry,
            providers=[provider],
            references=ReferenceSigner(b"http-provider-integration-secret"),
            audit=MemoryAuditSink(),
        )
        context = ServiceContext("tenant", "principal", "session")
        budget = BudgetLedger(
            "task",
            {"bytes": 10_000, "executions": 2, "loads": 2, "portable_tokens": 2_000},
        )

        card = service.search("http api", task_id="task", context=context, budget=budget).cards[0]
        loaded = service.load(card.capability_ref, task_id="task", context=context, budget=budget)
        result = service.execute(
            ExecutionRequest(loaded.execution_ref, "find", {"item": "admitted"}, "task"),
            context=context,
            budget=budget,
            max_output_tokens=200,
        )

    assert result.output["path"] == "/items/admitted"


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "ftp://localhost/data", "https://user:pass@example.com"],
)
def test_http_provider_rejects_unsafe_base_urls(url) -> None:
    with pytest.raises(ValueError):
        HttpApiFixture(_manifest(), url, {"find": HttpInvocation("GET", "/items/{item}")})
