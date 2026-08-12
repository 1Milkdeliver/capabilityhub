"""Optional model execution behind an enforced reasoning request policy."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Any, Protocol

from capabilityhub.orchestration import ModelRequestPolicy


@dataclass(frozen=True, slots=True)
class ModelInvocationUsage:
    endpoint: str
    model: str
    effort: str
    reasoning_tokens: int
    cost_units: int | None
    latency_ms: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.reasoning_tokens, "reasoning_tokens"),
            (self.cost_units, "cost_units"),
            (self.latency_ms, "latency_ms"),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"model invocation {label} must be non-negative")


class ModelRequestExecutor(Protocol):
    def invoke(self, policy: ModelRequestPolicy) -> ModelInvocationUsage: ...


CostCalculator = Callable[[int, int, int], int]
InputProvider = Callable[[ModelRequestPolicy], str]


class OpenAIReasoningExecutor:
    """Explicit live adapter using the official SDK; never loaded by default."""

    def __init__(
        self,
        *,
        endpoint_name: str,
        input_provider: InputProvider,
        api_key: str | None = None,
        base_url: str | None = None,
        cost_calculator: CostCalculator | None = None,
    ) -> None:
        if not endpoint_name or len(endpoint_name) > 128:
            raise ValueError("model endpoint name is invalid")
        module = importlib.import_module("openai")
        client_options: dict[str, str] = {}
        if api_key is not None:
            client_options["api_key"] = api_key
        if base_url is not None:
            client_options["base_url"] = base_url
        self._client = module.OpenAI(**client_options)
        self._endpoint_name = endpoint_name
        self._input = input_provider
        self._cost = cost_calculator

    def invoke(self, policy: ModelRequestPolicy) -> ModelInvocationUsage:
        if policy.endpoint != self._endpoint_name:
            raise ValueError("model endpoint policy mismatch")
        started = perf_counter_ns()
        request: dict[str, Any] = {
            "model": policy.model,
            "reasoning": {"effort": policy.effort},
            "input": self._input(policy),
        }
        if policy.maximum_latency_ms is not None:
            request["timeout"] = policy.maximum_latency_ms / 1_000
        response = self._client.responses.create(**request)
        latency = round((perf_counter_ns() - started) / 1_000_000)
        response_model = getattr(response, "model", policy.model)
        if response_model != policy.model:
            raise ValueError("model response policy mismatch")
        usage: Any = response.usage
        input_tokens = int(usage.input_tokens)
        output_tokens = int(usage.output_tokens)
        details = getattr(usage, "output_tokens_details", None)
        reasoning_tokens = int(getattr(details, "reasoning_tokens", 0) or 0)
        cost = (
            None
            if self._cost is None
            else self._cost(input_tokens, output_tokens, reasoning_tokens)
        )
        return ModelInvocationUsage(
            policy.endpoint,
            policy.model,
            policy.effort,
            reasoning_tokens,
            cost,
            latency,
        )
