"""Transport-neutral request and response contract for CapSift clients.

This module defines conformance primitives only.  It deliberately does not
implement a CLI process, MCP server, or HTTP service.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, runtime_checkable

from capabilityhub.compatibility import (
    CompatibilityDecision,
    FeatureHandshake,
    decide_compatibility,
    v1alpha1_handshake,
)
from capabilityhub.errors import CapabilityHubError, ErrorCategory

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

REQUEST_ENVELOPE = "protocol.request-envelope"
CORRELATION_ID = "protocol.correlation-id"
NORMALIZED_ERRORS = "protocol.normalized-errors"
STREAMING = "transport.streaming"
CANCELLATION = "transport.cancellation"
BASE_PROTOCOL_FEATURES = (REQUEST_ENVELOPE, CORRELATION_ID, NORMALIZED_ERRORS)


class AdapterKind(StrEnum):
    CLI = "cli"
    MCP = "mcp"
    HTTP = "http"
    LIBRARY = "library"


@dataclass(frozen=True, slots=True)
class ProtocolNegotiation:
    decision: CompatibilityDecision
    streaming: bool
    cancellation: bool


@dataclass(frozen=True, slots=True)
class RequestEnvelope:
    adapter: AdapterKind
    request_id: str
    correlation_id: str
    operation: str
    payload: Mapping[str, JsonValue]
    handshake: FeatureHandshake
    negotiation: ProtocolNegotiation
    stream: bool = False
    cancel_target: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.request_id, "request_id")
        _validate_identifier(self.correlation_id, "correlation_id")
        _validate_identifier(self.operation, "operation")
        if self.cancel_target is not None:
            _validate_identifier(self.cancel_target, "cancel_target")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class NormalizedError:
    code: str
    category: str
    retryable: bool
    safe_message: str
    details: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True, slots=True)
class ResponseEnvelope:
    request_id: str
    correlation_id: str
    ok: bool
    result: JsonValue = None
    error: NormalizedError | None = None
    sequence: int = 0
    terminal: bool = True

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.ok == (self.error is not None):
            raise ValueError("successful responses cannot contain an error")

    def as_dict(self) -> dict[str, JsonValue]:
        value: dict[str, JsonValue] = {
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "ok": self.ok,
            "sequence": self.sequence,
            "terminal": self.terminal,
        }
        if self.ok:
            value["result"] = self.result
        else:
            assert self.error is not None
            value["error"] = {
                "code": self.error.code,
                "category": self.error.category,
                "retryable": self.error.retryable,
                "safe_message": self.error.safe_message,
                "details": dict(self.error.details),
            }
        return value


def protocol_handshake(*, streaming: bool = False, cancellation: bool = False) -> FeatureHandshake:
    """Advertise the protocol contract and optional transport capabilities."""

    optional = tuple(
        feature
        for feature, enabled in ((STREAMING, streaming), (CANCELLATION, cancellation))
        if enabled
    )
    return v1alpha1_handshake(
        extra_supported=(*BASE_PROTOCOL_FEATURES, *optional),
        extra_required=BASE_PROTOCOL_FEATURES,
    )


def in_process_request(
    adapter: AdapterKind,
    operation: str,
    payload: Mapping[str, JsonValue],
    *,
    request_id: str,
    correlation_id: str,
    handshake: FeatureHandshake | None = None,
) -> RequestEnvelope:
    """Build a negotiated envelope for an SDK or in-memory adapter boundary."""

    selected = handshake or protocol_handshake()
    return RequestEnvelope(
        adapter=adapter,
        request_id=request_id,
        correlation_id=correlation_id,
        operation=operation,
        payload=payload,
        handshake=selected,
        negotiation=negotiate_protocol(selected, selected),
    )


def negotiate_protocol(client: FeatureHandshake, server: FeatureHandshake) -> ProtocolNegotiation:
    decision = decide_compatibility(client, server)
    if not decision.compatible:
        raise CapabilityHubError(
            code="incompatible_protocol",
            category=ErrorCategory.INPUT,
            safe_message="Client and server protocol features are incompatible.",
            details={
                "reason_codes": list(decision.reason_codes),
                "unsupported_client_required": list(decision.unsupported_client_required),
                "unsupported_server_required": list(decision.unsupported_server_required),
            },
        )
    enabled = set(decision.enabled_features)
    return ProtocolNegotiation(
        decision=decision,
        streaming=STREAMING in enabled,
        cancellation=CANCELLATION in enabled,
    )


def parse_request(
    adapter: AdapterKind,
    raw: Mapping[str, Any],
    *,
    server_handshake: FeatureHandshake,
) -> RequestEnvelope:
    """Parse the same wire envelope for every client adapter."""

    try:
        handshake_raw = _mapping(raw["handshake"], "handshake")
        handshake = FeatureHandshake(
            api_versions=_strings(handshake_raw["api_versions"], "api_versions"),
            supported_features=_strings(handshake_raw["supported_features"], "supported_features"),
            required_features=_strings(
                handshake_raw.get("required_features", ()),
                "required_features",
                allow_empty=True,
            ),
        )
        request_id = _string(raw["request_id"], "request_id")
        correlation_id = _string(raw["correlation_id"], "correlation_id")
        operation = _string(raw["operation"], "operation")
        payload = _json_mapping(raw.get("payload", {}), "payload")
        stream = _boolean(raw.get("stream", False), "stream")
        cancel_target_raw = raw.get("cancel_target")
        cancel_target = (
            None if cancel_target_raw is None else _string(cancel_target_raw, "cancel_target")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CapabilityHubError(
            code="invalid_request_envelope",
            category=ErrorCategory.INPUT,
            safe_message="The request envelope is invalid.",
        ) from exc

    negotiation = negotiate_protocol(handshake, server_handshake)
    if stream and not negotiation.streaming:
        raise CapabilityHubError(
            code="streaming_not_negotiated",
            category=ErrorCategory.INPUT,
            safe_message="Streaming was requested but was not negotiated.",
        )
    if cancel_target is not None and not negotiation.cancellation:
        raise CapabilityHubError(
            code="cancellation_not_negotiated",
            category=ErrorCategory.INPUT,
            safe_message="Cancellation was requested but was not negotiated.",
        )
    return RequestEnvelope(
        adapter=adapter,
        request_id=request_id,
        correlation_id=correlation_id,
        operation=operation,
        payload=payload,
        handshake=handshake,
        negotiation=negotiation,
        stream=stream,
        cancel_target=cancel_target,
    )


def success_response(
    request: RequestEnvelope,
    result: JsonValue,
    *,
    sequence: int = 0,
    terminal: bool = True,
) -> ResponseEnvelope:
    return ResponseEnvelope(
        request_id=request.request_id,
        correlation_id=request.correlation_id,
        ok=True,
        result=result,
        sequence=sequence,
        terminal=terminal,
    )


def error_response(
    *,
    request_id: str,
    correlation_id: str,
    error: Exception,
) -> ResponseEnvelope:
    """Normalize known failures and redact unexpected exception messages."""

    if isinstance(error, CapabilityHubError):
        normalized = NormalizedError(
            code=error.code,
            category=error.category.value,
            retryable=error.retryable,
            safe_message=error.safe_message,
            details=_safe_details(error.details),
        )
    else:
        normalized = NormalizedError(
            code="internal_error",
            category=ErrorCategory.INTERNAL.value,
            retryable=False,
            safe_message="The request could not be completed.",
            details={},
        )
    return ResponseEnvelope(
        request_id=request_id,
        correlation_id=correlation_id,
        ok=False,
        error=normalized,
    )


@runtime_checkable
class AdapterContract(Protocol):
    """Minimal boundary every CLI/MCP/HTTP/library adapter must satisfy."""

    kind: AdapterKind
    handshake: FeatureHandshake

    def dispatch(self, request: RequestEnvelope) -> JsonValue: ...

    def cancel(self, correlation_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ConformanceFixture:
    name: str
    operation: str
    payload: Mapping[str, JsonValue]
    expected: JsonValue
    stream: bool = False
    cancel_target: str | None = None


@dataclass(frozen=True, slots=True)
class ConformanceResult:
    fixture: str
    adapter: AdapterKind
    response: ResponseEnvelope
    passed: bool


def run_conformance_suite(
    adapter: AdapterContract,
    fixtures: Sequence[ConformanceFixture],
    *,
    server_handshake: FeatureHandshake,
) -> tuple[ConformanceResult, ...]:
    """Run transport-independent fixtures against any in-process adapter boundary."""

    results: list[ConformanceResult] = []
    for fixture in fixtures:
        raw = _fixture_envelope(fixture, adapter.handshake)
        request_id = _string(raw["request_id"], "request_id")
        correlation_id = _string(raw["correlation_id"], "correlation_id")
        try:
            request = parse_request(adapter.kind, raw, server_handshake=server_handshake)
            if request.cancel_target is not None:
                output: JsonValue = {"cancelled": adapter.cancel(request.cancel_target)}
            else:
                output = adapter.dispatch(request)
            response = success_response(request, output)
        except Exception as exc:
            response = error_response(
                request_id=request_id,
                correlation_id=correlation_id,
                error=exc,
            )
        results.append(
            ConformanceResult(
                fixture=fixture.name,
                adapter=adapter.kind,
                response=response,
                passed=response.ok and response.result == fixture.expected,
            )
        )
    return tuple(results)


def _fixture_envelope(fixture: ConformanceFixture, handshake: FeatureHandshake) -> dict[str, Any]:
    return {
        "request_id": f"request-{fixture.name}",
        "correlation_id": f"correlation-{fixture.name}",
        "operation": fixture.operation,
        "payload": dict(fixture.payload),
        "stream": fixture.stream,
        "cancel_target": fixture.cancel_target,
        "handshake": {
            "api_versions": list(handshake.api_versions),
            "supported_features": list(handshake.supported_features),
            "required_features": list(handshake.required_features),
        },
    }


def _validate_identifier(value: str, label: str) -> None:
    if not value or len(value) > 256 or any(character.isspace() for character in value):
        raise ValueError(f"{label} must be a non-empty identifier of at most 256 characters")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean")
    return value


def _strings(value: Any, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a list")
    result = tuple(value)
    if any(not isinstance(item, str) for item in result):
        raise TypeError(f"{label} entries must be strings")
    if not result and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    return result


def _json_mapping(value: Any, label: str) -> dict[str, JsonValue]:
    mapping = _mapping(value, label)
    return {key: _json_value(item, f"{label}.{key}") for key, item in mapping.items()}


def _json_value(value: Any, label: str) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"{label} object keys must be strings")
        return {key: _json_value(item, label) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item, label) for item in value]
    raise TypeError(f"{label} must contain JSON-compatible values")


def _safe_details(details: Mapping[str, Any]) -> Mapping[str, JsonValue]:
    try:
        return _json_mapping(details, "details")
    except (TypeError, ValueError):
        return {}
