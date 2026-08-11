from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from capabilityhub.compatibility import FeatureHandshake
from capabilityhub.http_control import LoopbackHttpControl
from capabilityhub.protocol import (
    AdapterKind,
    ConformanceFixture,
    JsonValue,
    RequestEnvelope,
    protocol_handshake,
    run_conformance_suite,
)


@dataclass
class ThreeToolAdapter:
    kind: AdapterKind = AdapterKind.HTTP
    handshake: FeatureHandshake = field(default_factory=protocol_handshake)
    calls: list[str] = field(default_factory=list)

    def dispatch(self, request: RequestEnvelope) -> JsonValue:
        self.calls.append(request.operation)
        return {"operation": request.operation, "payload": dict(request.payload)}

    def cancel(self, correlation_id: str) -> bool:
        return False


def _envelope(operation: str = "capability.search") -> dict[str, object]:
    handshake = protocol_handshake()
    return {
        "request_id": "request-1",
        "correlation_id": "correlation-1",
        "operation": operation,
        "payload": {"query": "pdf"},
        "handshake": {
            "api_versions": list(handshake.api_versions),
            "supported_features": list(handshake.supported_features),
            "required_features": list(handshake.required_features),
        },
    }


def _request(
    url: str,
    token: str,
    *,
    body: bytes | None = None,
    method: str = "POST",
    content_type: str = "application/json",
    origin: str | None = None,
) -> tuple[int, dict[str, object], dict[str, str]]:
    request = Request(
        url,
        data=json.dumps(_envelope()).encode() if body is None else body,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
    )
    if origin is not None:
        request.add_header("Origin", origin)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        payload = json.loads(response.read()) if method != "HEAD" else {}
        return response.status, payload, dict(response.headers)


@pytest.mark.parametrize(
    "operation",
    ["capability.search", "capability.load", "capability.execute"],
)
def test_real_http_roundtrip_uses_shared_envelopes(operation: str) -> None:
    adapter = ThreeToolAdapter()
    control = LoopbackHttpControl(adapter)
    access = control.start()
    try:
        body = json.dumps(_envelope(operation)).encode()
        status, response, headers = _request(access.url, access.bearer_token, body=body)
    finally:
        control.close()

    assert status == 200
    assert response == {
        "correlation_id": "correlation-1",
        "ok": True,
        "request_id": "request-1",
        "result": {"operation": operation, "payload": {"query": "pdf"}},
        "sequence": 0,
        "terminal": True,
    }
    assert headers["X-Correlation-ID"] == "correlation-1"


def test_adapter_fixture_passes_conformance_and_http_roundtrip() -> None:
    adapter = ThreeToolAdapter()
    fixture = ConformanceFixture(
        "search",
        "capability.search",
        {"query": "pdf"},
        {"operation": "capability.search", "payload": {"query": "pdf"}},
    )
    assert run_conformance_suite(
        adapter,
        (fixture,),
        server_handshake=adapter.handshake,
    )[0].passed

    control = LoopbackHttpControl(adapter)
    access = control.start()
    try:
        status, response, _ = _request(access.url, access.bearer_token)
    finally:
        control.close()
    assert status == 200
    assert response["result"] == fixture.expected


def test_token_is_high_entropy_private_and_compared_without_echo() -> None:
    control = LoopbackHttpControl(ThreeToolAdapter())
    access = control.start()
    try:
        assert len(access.bearer_token) >= 43
        assert access.bearer_token not in repr(access)
        assert access.bearer_token not in repr(control.status())
        status, response, _ = _request(access.url, "wrong-token")
    finally:
        control.close()

    assert status == 401
    assert response["error"]["code"] == "invalid_bearer_token"  # type: ignore[index]
    assert access.bearer_token not in repr(response)


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "192.168.1.2", "::"])
def test_non_loopback_or_dns_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValueError, match=r"127\.0\.0\.1 or ::1"):
        LoopbackHttpControl(ThreeToolAdapter(), host=host)


def test_method_content_type_origin_and_body_limits_are_enforced() -> None:
    origin = "http://127.0.0.1:4200"
    control = LoopbackHttpControl(
        ThreeToolAdapter(),
        max_body_bytes=256,
        allowed_origins=(origin,),
    )
    access = control.start()
    try:
        method_status, _, _ = _request(access.url, access.bearer_token, method="GET")
        type_status, _, _ = _request(
            access.url,
            access.bearer_token,
            content_type="text/plain",
        )
        origin_status, _, _ = _request(
            access.url,
            access.bearer_token,
            origin="http://127.0.0.1:9999",
        )
        body_status, _, _ = _request(
            access.url,
            access.bearer_token,
            body=b"{" + b"x" * 300 + b"}",
        )
    finally:
        control.close()

    assert (method_status, type_status, origin_status, body_status) == (405, 415, 403, 413)


def test_concurrent_dispatch_is_safe_and_close_is_idempotent() -> None:
    adapter = ThreeToolAdapter()
    control = LoopbackHttpControl(adapter)
    access = control.start()

    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(
            pool.map(
                lambda _: _request(access.url, access.bearer_token)[0],
                range(30),
            )
        )
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: control.close(), range(8)))

    assert statuses == [200] * 30
    assert len(adapter.calls) == 30
    assert not control.status().running
    control.close()
