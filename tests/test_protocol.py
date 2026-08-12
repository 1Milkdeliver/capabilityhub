from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from capabilityhub.compatibility import FeatureHandshake
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.protocol import (
    BASE_PROTOCOL_FEATURES,
    CANCELLATION,
    STREAMING,
    AdapterKind,
    ConformanceFixture,
    JsonValue,
    RequestEnvelope,
    error_response,
    in_process_request,
    parse_request,
    protocol_handshake,
    run_conformance_suite,
    success_response,
)


def _raw_request(handshake: FeatureHandshake, **overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "request_id": "request-1",
        "correlation_id": "trace-user-42",
        "operation": "capability.search",
        "payload": {"query": "pdf"},
        "handshake": {
            "api_versions": list(handshake.api_versions),
            "supported_features": list(handshake.supported_features),
            "required_features": list(handshake.required_features),
        },
    }
    raw.update(overrides)
    return raw


@pytest.mark.parametrize("kind", list(AdapterKind))
def test_all_client_kinds_parse_the_same_request_envelope(kind: AdapterKind) -> None:
    handshake = protocol_handshake(streaming=True, cancellation=True)

    request = parse_request(kind, _raw_request(handshake), server_handshake=handshake)

    assert request.adapter is kind
    assert request.request_id == "request-1"
    assert request.correlation_id == "trace-user-42"
    assert request.payload == {"query": "pdf"}
    assert request.negotiation.streaming is True
    assert request.negotiation.cancellation is True


def test_unknown_required_feature_is_rejected_fail_closed() -> None:
    server = protocol_handshake()
    supported = (*server.supported_features, "security.future-proof-lock")
    client = FeatureHandshake(
        api_versions=server.api_versions,
        supported_features=supported,
        required_features=(*BASE_PROTOCOL_FEATURES, "security.future-proof-lock"),
    )

    with pytest.raises(CapabilityHubError) as caught:
        parse_request(AdapterKind.HTTP, _raw_request(client), server_handshake=server)

    assert caught.value.code == "incompatible_protocol"
    assert caught.value.details["unsupported_client_required"] == ["security.future-proof-lock"]


@pytest.mark.parametrize(
    ("request_field", "expected_code"),
    [
        ({"stream": True}, "streaming_not_negotiated"),
        ({"cancel_target": "trace-old"}, "cancellation_not_negotiated"),
    ],
)
def test_optional_transport_capabilities_must_be_negotiated(
    request_field: dict[str, object], expected_code: str
) -> None:
    client = protocol_handshake(streaming=True, cancellation=True)
    server = protocol_handshake()

    with pytest.raises(CapabilityHubError) as caught:
        parse_request(
            AdapterKind.MCP,
            _raw_request(client, **request_field),
            server_handshake=server,
        )

    assert caught.value.code == expected_code


def test_streaming_response_keeps_correlation_and_sequence() -> None:
    handshake = protocol_handshake(streaming=True)
    request = parse_request(
        AdapterKind.LIBRARY,
        _raw_request(handshake, stream=True),
        server_handshake=handshake,
    )

    first = success_response(request, {"item": 1}, sequence=0, terminal=False)
    last = success_response(request, {"item": 2}, sequence=1, terminal=True)

    assert first.correlation_id == last.correlation_id == "trace-user-42"
    assert first.as_dict()["terminal"] is False
    assert last.as_dict()["sequence"] == 1


def test_error_mapping_preserves_safe_errors_and_redacts_unexpected_errors() -> None:
    safe = error_response(
        request_id="request-1",
        correlation_id="trace-1",
        error=CapabilityHubError(
            code="provider_timeout",
            category=ErrorCategory.TIMEOUT,
            safe_message="The provider timed out.",
            retryable=True,
            details={"provider": "local"},
        ),
    )
    unsafe = error_response(
        request_id="request-2",
        correlation_id="trace-2",
        error=RuntimeError("secret-token=do-not-leak"),
    )

    assert safe.as_dict()["error"] == {
        "code": "provider_timeout",
        "category": "timeout",
        "retryable": True,
        "safe_message": "The provider timed out.",
        "details": {"provider": "local"},
    }
    assert unsafe.error is not None
    assert unsafe.error.code == "internal_error"
    assert "secret-token" not in str(unsafe.as_dict())


@dataclass
class InMemoryAdapter:
    kind: AdapterKind
    handshake: FeatureHandshake
    cancelled: list[str] = field(default_factory=list)

    def dispatch(self, request: RequestEnvelope) -> JsonValue:
        if request.operation == "capability.echo":
            return {"echo": request.payload["value"]}
        raise CapabilityHubError(
            code="unsupported_operation",
            category=ErrorCategory.INPUT,
            safe_message="The operation is unsupported.",
        )

    def cancel(self, correlation_id: str) -> bool:
        self.cancelled.append(correlation_id)
        return True


def test_same_fixture_suite_conforms_across_all_adapter_kinds() -> None:
    handshake = protocol_handshake(streaming=True, cancellation=True)
    fixtures = (
        ConformanceFixture(
            name="echo",
            operation="capability.echo",
            payload={"value": "hello"},
            expected={"echo": "hello"},
        ),
        ConformanceFixture(
            name="cancel",
            operation="capability.cancel",
            payload={},
            cancel_target="correlation-running",
            expected={"cancelled": True},
        ),
    )

    all_results = [
        run_conformance_suite(
            InMemoryAdapter(kind, handshake),
            fixtures,
            server_handshake=handshake,
        )
        for kind in AdapterKind
    ]

    assert all(result.passed for results in all_results for result in results)
    canonical = [result.response.as_dict() for result in all_results[0]]
    for results in all_results[1:]:
        assert [result.response.as_dict() for result in results] == canonical


def test_invalid_envelope_is_a_stable_input_error() -> None:
    handshake = protocol_handshake()
    raw = _raw_request(handshake)
    del raw["correlation_id"]

    with pytest.raises(CapabilityHubError) as caught:
        parse_request(AdapterKind.CLI, raw, server_handshake=handshake)

    assert caught.value.code == "invalid_request_envelope"
    assert caught.value.category is ErrorCategory.INPUT


def test_protocol_handshake_exposes_optional_features_only_when_enabled() -> None:
    base = protocol_handshake()
    full = protocol_handshake(streaming=True, cancellation=True)

    assert STREAMING not in base.supported_features
    assert CANCELLATION not in base.supported_features
    assert STREAMING in full.supported_features
    assert CANCELLATION in full.supported_features
    assert set(BASE_PROTOCOL_FEATURES) <= set(full.required_features)


def test_in_process_request_preserves_sdk_correlation_without_wire_reparse() -> None:
    request = in_process_request(
        AdapterKind.MCP,
        "capability.search",
        {"query": "records", "task_id": "task"},
        request_id="mcp-request-7",
        correlation_id="trace-sdk-42",
    )

    assert request.adapter is AdapterKind.MCP
    assert request.request_id == "mcp-request-7"
    assert request.correlation_id == "trace-sdk-42"
    assert request.negotiation.decision.compatible
