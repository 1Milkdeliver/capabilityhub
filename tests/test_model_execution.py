from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from capabilityhub.model_execution import OpenAIReasoningExecutor
from capabilityhub.models import ReasoningTier
from capabilityhub.orchestration import ModelRequestPolicy


def _policy() -> ModelRequestPolicy:
    return ModelRequestPolicy(
        endpoint="approved-endpoint",
        model="approved-model",
        tier=ReasoningTier.MEDIUM,
        effort="medium",
        maximum_cost_units=5,
        maximum_latency_ms=2_000,
        estimated_tokens=1_024,
    )


def test_openai_executor_maps_enforced_policy_to_official_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    class Responses:
        def create(self, **request: object) -> object:
            requests.append(request)
            details = SimpleNamespace(reasoning_tokens=7)
            usage = SimpleNamespace(
                input_tokens=11,
                output_tokens=13,
                output_tokens_details=details,
            )
            return SimpleNamespace(usage=usage, model="approved-model")

    class Client:
        def __init__(self, **options: str) -> None:
            assert options == {"base_url": "https://approved.invalid/v1"}
            self.responses = Responses()

    monkeypatch.setattr(
        "capabilityhub.model_execution.importlib.import_module",
        lambda name: SimpleNamespace(OpenAI=Client),
    )
    executor = OpenAIReasoningExecutor(
        endpoint_name="approved-endpoint",
        input_provider=lambda _policy: "fixed evaluation input",
        base_url="https://approved.invalid/v1",
        cost_calculator=lambda input_tokens, output_tokens, reasoning_tokens: (
            input_tokens + output_tokens + reasoning_tokens
        ),
    )

    usage = executor.invoke(_policy())

    assert requests == [
        {
            "model": "approved-model",
            "reasoning": {"effort": "medium"},
            "input": "fixed evaluation input",
            "timeout": 2.0,
        }
    ]
    assert usage.endpoint == "approved-endpoint"
    assert usage.reasoning_tokens == 7
    assert usage.cost_units == 31
    with pytest.raises(ValueError, match="endpoint policy mismatch"):
        executor.invoke(replace(_policy(), endpoint="unapproved-endpoint"))


def test_openai_executor_is_explicit_and_missing_sdk_does_not_fake_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> object:
        raise ModuleNotFoundError("optional SDK unavailable")

    monkeypatch.setattr(
        "capabilityhub.model_execution.importlib.import_module", missing
    )

    with pytest.raises(ModuleNotFoundError, match="optional SDK unavailable"):
        OpenAIReasoningExecutor(
            endpoint_name="approved-endpoint",
            input_provider=lambda _policy: "input",
        )
