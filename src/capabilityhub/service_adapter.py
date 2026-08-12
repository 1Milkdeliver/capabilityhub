"""Transport-neutral AdapterContract backed by CapabilityHubService."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import TypeVar

from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.model_execution import ModelInvocationUsage, ModelRequestExecutor
from capabilityhub.models import (
    ExecutionRequest,
    ExecutionResult,
    LoadedCapability,
    ReasoningTier,
    SearchCard,
    SideEffect,
)
from capabilityhub.observability import (
    InMemoryObservability,
    ProviderCategory,
    SpanHandle,
    TraceContext,
)
from capabilityhub.orchestration import (
    AppliedReasoningRouter,
    ModelRequestPolicy,
    ReasoningConstraints,
    ReasoningEndpoint,
    ReasoningWorkload,
)
from capabilityhub.protocol import (
    AdapterKind,
    JsonValue,
    RequestEnvelope,
    protocol_handshake,
)
from capabilityhub.search import SearchResponse
from capabilityhub.service import CapabilityHubService, ServiceContext

ContextProvider = Callable[[], ServiceContext]
BudgetProvider = Callable[[str], BudgetLedger]
InventoryProvider = Callable[[], dict[str, JsonValue] | None]
CancelCallback = Callable[[str], bool]
ReasoningWorkloadProvider = Callable[[str, Mapping[str, JsonValue]], ReasoningWorkload]
ReasoningConstraintsProvider = Callable[[RequestEnvelope], ReasoningConstraints]

_SEARCH = "capability.search"
_LOAD = "capability.load"
_EXECUTE = "capability.execute"
_MAX_TEXT = 4_096
_MAX_SELECTIONS = 100
_ProvidedT = TypeVar("_ProvidedT")


class CapabilityHubServiceAdapter:
    """Expose one service through the shared CLI/MCP/HTTP/library contract."""

    def __init__(
        self,
        service: CapabilityHubService,
        *,
        kind: AdapterKind,
        context_provider: ContextProvider,
        budget_provider: BudgetProvider,
        inventory_provider: InventoryProvider | None = None,
        cancel_callback: CancelCallback | None = None,
        observability: InMemoryObservability | None = None,
        reasoning_router: AppliedReasoningRouter | None = None,
        reasoning_workload_provider: ReasoningWorkloadProvider | None = None,
        reasoning_constraints_provider: ReasoningConstraintsProvider | None = None,
        reasoning_executor: ModelRequestExecutor | None = None,
    ) -> None:
        self.kind = kind
        self.handshake = protocol_handshake(cancellation=cancel_callback is not None)
        self._service = service
        self._context_provider = context_provider
        self._budget_provider = budget_provider
        self._inventory_provider = inventory_provider
        self._cancel_callback = cancel_callback
        self._observability = observability
        self._reasoning = reasoning_router or AppliedReasoningRouter(
            budget_provider=budget_provider,
            endpoints=tuple(
                ReasoningEndpoint(f"local-{tier.value}", tier) for tier in ReasoningTier
            ),
        )
        self._reasoning_workload_provider = (
            reasoning_workload_provider or _default_reasoning_workload
        )
        self._reasoning_constraints_provider = reasoning_constraints_provider
        self._reasoning_executor = reasoning_executor

    def dispatch(self, request: RequestEnvelope) -> JsonValue:
        """Validate an exact meta-tool payload and return JSON-domain data."""

        observer = self._observability
        if observer is None:
            return self._dispatch(request)
        span = _start_observation(observer, request)
        try:
            result = self._dispatch(request)
        except Exception as error:
            _finish_observation(span, error_code=_safe_error_code(error))
            raise
        portable_tokens, payload_bytes = _result_counters(request.operation, result)
        _finish_observation(
            span,
            portable_tokens=portable_tokens,
            payload_bytes=payload_bytes,
        )
        return result

    def _dispatch(self, request: RequestEnvelope) -> JsonValue:
        """Dispatch without observability work when the optional observer is absent."""

        if request.adapter is not self.kind:
            raise _input("adapter_kind_mismatch", "The request uses a different adapter kind.")
        if request.stream:
            raise _input("streaming_unsupported", "This adapter does not support streaming.")
        if request.cancel_target is not None:
            raise _input(
                "invalid_cancel_dispatch",
                "Cancellation requests must use the adapter cancellation operation.",
            )
        if request.operation == _SEARCH:
            return self._search(request)
        if request.operation == _LOAD:
            return self._load(request)
        if request.operation == _EXECUTE:
            return self._execute(request)
        raise _input("unsupported_operation", "The requested operation is not supported.")

    def cancel(self, correlation_id: str) -> bool:
        """Delegate cancellation when configured; otherwise fail explicitly."""

        if not correlation_id or len(correlation_id) > 256:
            raise _input("invalid_correlation_id", "The correlation identifier is invalid.")
        callback = self._cancel_callback
        if callback is None:
            raise _input("cancellation_unsupported", "This adapter does not support cancellation.")
        return _provided(lambda: callback(correlation_id))

    def reasoning_state(self, task_id: str) -> dict[str, JsonValue] | None:
        """Return the last privacy-safe tier application for a task."""

        return self._reasoning.state(task_id)

    def _search(self, request: RequestEnvelope) -> JsonValue:
        payload = _payload(
            request.payload,
            required={"query", "task_id"},
            optional={
                "kinds",
                "limit",
                "max_output_tokens",
                "include_inventory",
                "include_cards",
            },
        )
        task_id = _text(payload["task_id"], maximum=256)
        self._apply_reasoning(request, task_id, payload)
        include_inventory = _boolean(payload.get("include_inventory", False))
        inventory = None
        if include_inventory and self._inventory_provider is not None:
            inventory = _provided(self._inventory_provider)
            if inventory is not None:
                inventory = _json_object(inventory)
        try:
            response = self._service.search(
                _query(payload["query"]),
                task_id=task_id,
                context=self._context(),
                budget=self._budget(task_id),
                kinds=_optional_strings(payload.get("kinds")),
                limit=_positive_integer(payload.get("limit", 8)),
                max_output_tokens=_positive_integer(payload.get("max_output_tokens", 900)),
                include_cards=_boolean(payload.get("include_cards", True)),
                inventory=inventory,
            )
        except Exception as error:
            self._record_reasoning_failure(task_id, request.operation, error)
            raise
        self._reasoning.record_result(
            task_id=task_id, operation=request.operation, error_code=None
        )
        return _search_json(response)

    def _load(self, request: RequestEnvelope) -> JsonValue:
        payload = _payload(
            request.payload,
            required={"capability_ref", "task_id"},
            optional={"section_names", "operation_names", "max_output_tokens"},
        )
        task_id = _text(payload["task_id"], maximum=256)
        self._apply_reasoning(request, task_id, payload)
        try:
            loaded = self._service.load(
                _text(payload["capability_ref"]),
                task_id=task_id,
                context=self._context(),
                budget=self._budget(task_id),
                section_names=_optional_strings(payload.get("section_names")),
                operation_names=_optional_strings(payload.get("operation_names")),
                max_output_tokens=_positive_integer(payload.get("max_output_tokens", 2_000)),
            )
        except Exception as error:
            self._record_reasoning_failure(task_id, request.operation, error)
            raise
        self._reasoning.record_result(
            task_id=task_id, operation=request.operation, error_code=None
        )
        return _loaded_json(loaded)

    def _execute(self, request: RequestEnvelope) -> JsonValue:
        payload = _payload(
            request.payload,
            required={"execution_ref", "operation", "arguments", "task_id"},
            optional={"approval_ref", "idempotency_key", "max_output_tokens"},
        )
        task_id = _text(payload["task_id"], maximum=256)
        self._apply_reasoning(request, task_id, payload)
        max_output = payload.get("max_output_tokens")
        try:
            result = self._service.execute(
                ExecutionRequest(
                    execution_ref=_text(payload["execution_ref"]),
                    operation=_text(payload["operation"], maximum=256),
                    arguments=_json_object(payload["arguments"]),
                    task_id=task_id,
                    approval_ref=_optional_text(payload.get("approval_ref")),
                    idempotency_key=_optional_text(payload.get("idempotency_key")),
                ),
                context=self._context(),
                budget=self._budget(task_id),
                max_output_tokens=(None if max_output is None else _positive_integer(max_output)),
            )
        except Exception as error:
            self._record_reasoning_failure(task_id, request.operation, error)
            raise
        self._reasoning.record_result(
            task_id=task_id, operation=request.operation, error_code=None
        )
        return _execution_json(result)

    def _apply_reasoning(
        self,
        request: RequestEnvelope,
        task_id: str,
        payload: Mapping[str, JsonValue],
    ) -> None:
        workload = _provided(
            lambda: self._reasoning_workload_provider(request.operation, payload)
        )
        if not isinstance(workload, ReasoningWorkload):
            raise _internal_provider_error()
        constraints_provider = self._reasoning_constraints_provider
        constraints = (
            ReasoningConstraints()
            if constraints_provider is None
            else _provided(lambda: constraints_provider(request))
        )
        if not isinstance(constraints, ReasoningConstraints):
            raise _internal_provider_error()
        decision = _provided(
            lambda: self._reasoning.decide(
                task_id=task_id,
                operation=request.operation,
                workload=workload,
                constraints=constraints,
            )
        )
        executor = self._reasoning_executor
        if executor is None:
            return
        policy = _provided(
            lambda: self._reasoning.request_policy(decision, constraints=constraints)
        )
        budget = self._budget(task_id)
        reservation = budget.reserve({"reasoning_tokens": policy.estimated_tokens})
        try:
            usage = _provided(lambda: executor.invoke(policy))
            if not isinstance(usage, ModelInvocationUsage):
                raise _reasoning_execution_error("reasoning_usage_invalid")
            reservation.reconcile({"reasoning_tokens": usage.reasoning_tokens})
            _enforce_model_usage(policy, usage)
        except Exception as error:
            if reservation.active:
                reservation.cancel()
            self._reasoning.record_result(
                task_id=task_id,
                operation=request.operation,
                error_code=_safe_error_code(error),
            )
            raise

    def _record_reasoning_failure(
        self, task_id: str, operation: str, error: Exception
    ) -> None:
        self._reasoning.record_result(
            task_id=task_id,
            operation=operation,
            error_code=_safe_error_code(error),
        )

    def _context(self) -> ServiceContext:
        context = _provided(self._context_provider)
        if not isinstance(context, ServiceContext):
            raise _internal_provider_error()
        return context

    def _budget(self, task_id: str) -> BudgetLedger:
        budget = _provided(lambda: self._budget_provider(task_id))
        if not isinstance(budget, BudgetLedger):
            raise _internal_provider_error()
        return budget


def _payload(
    value: Mapping[str, JsonValue],
    *,
    required: set[str],
    optional: set[str],
) -> Mapping[str, JsonValue]:
    keys = set(value)
    if not required <= keys or not keys <= required | optional:
        raise _invalid_payload()
    return value


def _text(value: object, *, maximum: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _invalid_payload()
    return value


def _query(value: object) -> str:
    if not isinstance(value, str) or len(value) > _MAX_TEXT:
        raise _invalid_payload()
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid_payload()
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise _invalid_payload()
    return value


def _optional_strings(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _invalid_payload()
    if len(value) > _MAX_SELECTIONS:
        raise _invalid_payload()
    return tuple(_text(item, maximum=256) for item in value)


def _json_object(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise _invalid_payload()
    return {key: _json_value(item, depth=0) for key, item in value.items()}


def _json_value(value: object, *, depth: int) -> JsonValue:
    if depth > 64:
        raise _invalid_payload()
    if isinstance(value, float) and not math.isfinite(value):
        raise _invalid_payload()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise _invalid_payload()
        return {key: _json_value(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item, depth=depth + 1) for item in value]
    raise _invalid_payload()


def _search_json(response: SearchResponse) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {
        "cards": [_card_json(card) for card in response.cards],
        "kind_counts": dict(response.kind_counts),
        "payload_bytes": response.payload_bytes,
        "portable_tokens": response.portable_tokens,
        "total_matches": response.total_matches,
        "truncated": response.truncated,
    }
    if response.inventory is not None:
        result["inventory"] = deepcopy(response.inventory)
    return result


def _card_json(card: SearchCard) -> dict[str, JsonValue]:
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


def _loaded_json(loaded: LoadedCapability) -> dict[str, JsonValue]:
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


def _execution_json(result: ExecutionResult) -> dict[str, JsonValue]:
    return {
        "audit_id": result.audit_id,
        "capability_revision": result.capability_revision,
        "operation": result.operation,
        "output": deepcopy(result.output),
        "portable_tokens": result.portable_tokens,
        "provider": result.provider,
    }


def _provided(callback: Callable[[], _ProvidedT]) -> _ProvidedT:
    try:
        return callback()
    except CapabilityHubError:
        raise
    except Exception as exc:
        raise _internal_provider_error() from exc


def _enforce_model_usage(
    policy: ModelRequestPolicy, usage: ModelInvocationUsage
) -> None:
    if (
        usage.endpoint != policy.endpoint
        or usage.model != policy.model
        or usage.effort != policy.effort
    ):
        raise _reasoning_execution_error("reasoning_endpoint_mismatch")
    if (
        policy.maximum_cost_units is not None
        and (usage.cost_units is None or usage.cost_units > policy.maximum_cost_units)
    ):
        raise _reasoning_execution_error("reasoning_cost_exceeded")
    if (
        policy.maximum_latency_ms is not None
        and usage.latency_ms > policy.maximum_latency_ms
    ):
        raise _reasoning_execution_error("reasoning_latency_exceeded")


def _reasoning_execution_error(code: str) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.POLICY,
        safe_message="The model invocation violated the applied reasoning policy.",
    )


def _start_observation(
    observer: InMemoryObservability, request: RequestEnvelope
) -> SpanHandle | None:
    operation = {
        _SEARCH: "search",
        _LOAD: "load",
        _EXECUTE: "execute",
    }.get(request.operation, "other")
    try:
        context = TraceContext.from_correlation(request.correlation_id, request.adapter.value)
        return observer.start_span(
            context,
            operation=operation,
            provider_category=ProviderCategory.OTHER,
        )
    except Exception:
        return None


def _finish_observation(
    span: SpanHandle | None,
    *,
    portable_tokens: int = 0,
    payload_bytes: int = 0,
    error_code: str | None = None,
) -> None:
    if span is None:
        return
    try:
        span.finish(
            portable_tokens=portable_tokens,
            payload_bytes=payload_bytes,
            error_code=error_code,
        )
    except Exception:
        return


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, CapabilityHubError):
        code = error.code
        if (
            isinstance(code, str)
            and 1 <= len(code) <= 64
            and code[0].islower()
            and all(
                character.islower() or character.isdigit() or character in "_.-"
                for character in code
            )
        ):
            return code
    return "adapter_unhandled_error"


def _result_counters(operation: str, result: JsonValue) -> tuple[int, int]:
    if not isinstance(result, Mapping):
        return 0, 0
    portable_tokens = result.get("portable_tokens")
    payload_bytes = result.get("payload_bytes") if operation == _SEARCH else None
    selected_tokens = 0
    selected_bytes = 0
    if isinstance(portable_tokens, int) and not isinstance(portable_tokens, bool):
        selected_tokens = max(0, portable_tokens)
    if isinstance(payload_bytes, int) and not isinstance(payload_bytes, bool):
        selected_bytes = max(0, payload_bytes)
    return selected_tokens, selected_bytes


def _default_reasoning_workload(
    operation: str, _payload: Mapping[str, JsonValue]
) -> ReasoningWorkload:
    risk = SideEffect.READ if operation == _EXECUTE else SideEffect.NONE
    return ReasoningWorkload(risk=risk)


def _invalid_payload() -> CapabilityHubError:
    return _input("invalid_adapter_payload", "The operation payload is invalid.")


def _internal_provider_error() -> CapabilityHubError:
    return CapabilityHubError(
        code="adapter_provider_failed",
        category=ErrorCategory.INTERNAL,
        safe_message="An adapter dependency could not provide request state.",
    )


def _input(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INPUT, safe_message=message)
