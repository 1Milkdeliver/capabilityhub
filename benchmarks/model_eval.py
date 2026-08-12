"""Provider-neutral eager-versus-lazy model cost and quality evaluation."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import random
import statistics
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from benchmarks.harness import BenchmarkTask, FixtureCatalog, load_fixtures

SCHEMA = "capabilityhub.model-eval.v1"
MODES = ("eager", "lazy")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    tool_calls: int


@dataclass(frozen=True, slots=True)
class ModelOutcome:
    selected_revision: str
    success: bool
    usage: ModelUsage
    latency_ms: float


class ModelAdapter(Protocol):
    provider: str
    model: str

    def evaluate(self, task: BenchmarkTask, mode: str, catalog: FixtureCatalog) -> ModelOutcome: ...


@dataclass(frozen=True, slots=True)
class EvalConfig:
    trials: int = 30
    bootstrap_samples: int = 2_000
    seed: int = 20_260_812
    non_inferiority_margin: float = 0.05
    max_cost_ratio: float = 0.90
    input_usd_per_million: float = 1.0
    output_usd_per_million: float = 4.0
    reasoning_usd_per_million: float = 4.0

    def __post_init__(self) -> None:
        if self.trials < 30 or self.bootstrap_samples < 100:
            raise ValueError("model evaluation requires >=30 trials and >=100 bootstrap samples")
        if not 0 <= self.non_inferiority_margin < 1 or self.max_cost_ratio <= 0:
            raise ValueError("model evaluation gate bounds are invalid")


@dataclass(frozen=True, slots=True)
class TrialRecord:
    task_id: str
    trial: int
    mode: str
    selected_revision: str
    success: bool
    selection_correct: bool
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    tool_calls: int
    latency_ms: float
    cost_microusd: int


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    estimate: float
    high: float


@dataclass(frozen=True, slots=True)
class ModeSummary:
    observations: int
    success_rate: float
    selection_accuracy: float
    mean_input_tokens: float
    mean_output_tokens: float
    mean_reasoning_tokens: float
    mean_tool_calls: float
    mean_latency_ms: float
    mean_cost_microusd: float


@dataclass(frozen=True, slots=True)
class EvaluationArtifact:
    schema: str
    status: str
    provider: str
    model: str
    fixture_digest: str
    config: dict[str, int | float]
    summaries: dict[str, ModeSummary]
    accuracy_difference_ci: Interval
    cost_ratio_ci: Interval
    non_inferiority_passed: bool
    cost_budget_passed: bool
    release_ready: bool
    trials: tuple[TrialRecord, ...]
    skip_reason: str | None = None


class OfflineFixtureAdapter:
    """Deterministic CI adapter exercising the real runner without claiming live evidence."""

    provider = "offline-fixture"
    model = "deterministic-v1"

    def evaluate(self, task: BenchmarkTask, mode: str, catalog: FixtureCatalog) -> ModelOutcome:
        digest = hashlib.sha256(f"{task.task_id}:{mode}".encode()).digest()
        eager = sum(len(value) for value in catalog.definitions_by_revision.values()) // 4
        input_tokens = eager if mode == "eager" else max(80, eager // 20)
        return ModelOutcome(
            task.expected_target_revision,
            True,
            ModelUsage(input_tokens, 20 + digest[0] % 3, 4, 0 if mode == "eager" else 1),
            10.0 + digest[1] / 100,
        )


class OpenAIResponsesAdapter:
    """Optional official-SDK live adapter; imported only for an explicit live run."""

    provider = "openai"

    def __init__(self, model: str, *, api_key: str | None = None) -> None:
        module = importlib.import_module("openai")
        self._client = module.OpenAI(api_key=api_key)
        self.model = model

    def evaluate(self, task: BenchmarkTask, mode: str, catalog: FixtureCatalog) -> ModelOutcome:
        started = time.perf_counter_ns()
        if mode == "eager":
            response = self._client.responses.create(
                model=self.model,
                input=_eager_input(task, catalog),
            )
            tool_calls = 0
            usage = _openai_usage(response, tool_calls)
        else:
            first = self._client.responses.create(
                model=self.model,
                input=_lazy_input(task),
                tools=[_search_tool()],
                tool_choice="required",
            )
            calls = [item for item in first.output if getattr(item, "type", "") == "function_call"]
            outputs = [
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": _compact_catalog(catalog),
                }
                for call in calls
            ]
            response = self._client.responses.create(
                model=self.model,
                previous_response_id=first.id,
                input=outputs,
            )
            usage = _sum_usage(_openai_usage(first, len(calls)), _openai_usage(response, 0))
        selected, success = _selection(response.output_text)
        return ModelOutcome(
            selected,
            success,
            usage,
            (time.perf_counter_ns() - started) / 1_000_000,
        )


def run_evaluation(
    adapter: ModelAdapter,
    *,
    config: EvalConfig | None = None,
    catalog: FixtureCatalog | None = None,
) -> EvaluationArtifact:
    selected_config = config or EvalConfig()
    fixtures = catalog or load_fixtures()
    records: list[TrialRecord] = []
    for trial in range(selected_config.trials):
        for task in fixtures.tasks:
            for mode in MODES:
                outcome = adapter.evaluate(task, mode, fixtures)
                _validate_outcome(outcome)
                records.append(_record(task, trial, mode, outcome, selected_config))
    summaries = {mode: _summary(records, mode) for mode in MODES}
    accuracy = _paired_bootstrap(records, selected_config, metric="accuracy_difference")
    cost = _paired_bootstrap(records, selected_config, metric="cost_ratio")
    non_inferior = accuracy.low >= -selected_config.non_inferiority_margin
    cost_ok = cost.high <= selected_config.max_cost_ratio
    return EvaluationArtifact(
        SCHEMA,
        "complete",
        adapter.provider,
        adapter.model,
        _fixture_digest(fixtures),
        _config_json(selected_config),
        summaries,
        accuracy,
        cost,
        non_inferior,
        cost_ok,
        non_inferior and cost_ok,
        tuple(records),
    )


def skipped_artifact(provider: str, model: str, reason: str) -> EvaluationArtifact:
    zero = Interval(0, 0, 0)
    return EvaluationArtifact(
        SCHEMA, "skipped", provider, model, "", {}, {}, zero, zero, False, False, False, (), reason
    )


def write_artifact(artifact: EvaluationArtifact, destination: str | Path) -> Path:
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(artifact), sort_keys=True, separators=(",", ":"))
    if _looks_secret(payload):
        raise ValueError("model evaluation artifact contains forbidden secret material")
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".model-eval-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def validate_artifact(path: str | Path, *, require_complete: bool = True) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise ValueError
        if set(payload) != set(EvaluationArtifact.__dataclass_fields__):
            raise ValueError
        if payload.get("status") not in {"complete", "skipped"} or _looks_secret(
            json.dumps(payload)
        ):
            raise ValueError
        if require_complete and payload["status"] != "complete":
            raise ValueError
        if payload["status"] == "complete":
            if int(payload["config"]["trials"]) < 30 or not payload["trials"]:
                raise ValueError
            if set(payload["summaries"]) != set(MODES):
                raise ValueError
            for trial in payload["trials"]:
                if set(trial) != set(TrialRecord.__dataclass_fields__):
                    raise ValueError
            if bool(payload["release_ready"]) != (
                bool(payload["non_inferiority_passed"]) and bool(payload["cost_budget_passed"])
            ):
                raise ValueError
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("model evaluation artifact validation failed") from error
    return cast(dict[str, Any], payload)


def _record(
    task: BenchmarkTask, trial: int, mode: str, outcome: ModelOutcome, config: EvalConfig
) -> TrialRecord:
    usage = outcome.usage
    cost = (
        usage.input_tokens * config.input_usd_per_million
        + usage.output_tokens * config.output_usd_per_million
        + usage.reasoning_tokens * config.reasoning_usd_per_million
    )
    return TrialRecord(
        task.task_id,
        trial,
        mode,
        outcome.selected_revision,
        outcome.success,
        outcome.selected_revision == task.expected_target_revision,
        usage.input_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
        usage.tool_calls,
        round(outcome.latency_ms, 6),
        round(cost),
    )


def _summary(records: Sequence[TrialRecord], mode: str) -> ModeSummary:
    selected = [record for record in records if record.mode == mode]

    def mean(field: str) -> float:
        return statistics.fmean(float(getattr(item, field)) for item in selected)

    return ModeSummary(
        len(selected),
        mean("success"),
        mean("selection_correct"),
        mean("input_tokens"),
        mean("output_tokens"),
        mean("reasoning_tokens"),
        mean("tool_calls"),
        mean("latency_ms"),
        mean("cost_microusd"),
    )


def _paired_bootstrap(
    records: Sequence[TrialRecord], config: EvalConfig, *, metric: str
) -> Interval:
    pairs: dict[tuple[int, str], dict[str, TrialRecord]] = {}
    for record in records:
        pairs.setdefault((record.trial, record.task_id), {})[record.mode] = record
    values = tuple(item for item in pairs.values() if set(item) == set(MODES))
    if not values:
        raise ValueError("paired observations are required")

    def measure(sample: Sequence[dict[str, TrialRecord]]) -> float:
        if metric == "accuracy_difference":
            return statistics.fmean(
                float(item["lazy"].selection_correct) - float(item["eager"].selection_correct)
                for item in sample
            )
        eager = statistics.fmean(item["eager"].cost_microusd for item in sample)
        lazy = statistics.fmean(item["lazy"].cost_microusd for item in sample)
        return lazy / eager if eager else math.inf

    rng = random.Random(config.seed + (0 if metric == "accuracy_difference" else 1))
    samples = sorted(
        measure([values[rng.randrange(len(values))] for _ in values])
        for _ in range(config.bootstrap_samples)
    )
    return Interval(
        round(samples[int(len(samples) * 0.025)], 8),
        round(measure(values), 8),
        round(samples[min(len(samples) - 1, int(len(samples) * 0.975))], 8),
    )


def _validate_outcome(outcome: ModelOutcome) -> None:
    usage = outcome.usage
    counters = (
        usage.input_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
        usage.tool_calls,
    )
    if any(isinstance(value, bool) or value < 0 for value in counters):
        raise ValueError("provider usage counters must be non-negative integers")
    if not math.isfinite(outcome.latency_ms) or outcome.latency_ms < 0:
        raise ValueError("provider latency must be non-negative and finite")


def _openai_usage(response: Any, tool_calls: int) -> ModelUsage:
    usage = response.usage
    details = getattr(usage, "output_tokens_details", None)
    reasoning = int(getattr(details, "reasoning_tokens", 0) or 0)
    return ModelUsage(int(usage.input_tokens), int(usage.output_tokens), reasoning, tool_calls)


def _sum_usage(first: ModelUsage, second: ModelUsage) -> ModelUsage:
    return ModelUsage(
        first.input_tokens + second.input_tokens,
        first.output_tokens + second.output_tokens,
        first.reasoning_tokens + second.reasoning_tokens,
        first.tool_calls + second.tool_calls,
    )


def _selection(text: str) -> tuple[str, bool]:
    try:
        payload = json.loads(text)
        return str(payload["selected_revision"]), bool(payload.get("success", True))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "", False


def _eager_input(task: BenchmarkTask, catalog: FixtureCatalog) -> str:
    definitions = "\n".join(catalog.definitions_by_revision.values())
    return _instruction(task) + "\nAvailable capability definitions:\n" + definitions


def _lazy_input(task: BenchmarkTask) -> str:
    return _instruction(task) + "\nCall capability_search before selecting."


def _instruction(task: BenchmarkTask) -> str:
    return (
        "Select the exact capability revision for the task. Return only JSON with "
        f"selected_revision and success. Task: {task.prompt}"
    )


def _search_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "capability_search",
        "description": "Return compact capability cards for the task.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    }


def _compact_catalog(catalog: FixtureCatalog) -> str:
    return json.dumps(
        [
            {
                "revision": manifest.identity.revision,
                "kind": manifest.kind.value,
                "summary": manifest.summary,
            }
            for manifest in catalog.manifests
        ],
        separators=(",", ":"),
    )


def _config_json(config: EvalConfig) -> dict[str, int | float]:
    return cast(dict[str, int | float], asdict(config))


def _fixture_digest(catalog: FixtureCatalog) -> str:
    tasks = [
        {
            "expected": task.expected_target_revision,
            "id": task.task_id,
            "prompt": task.prompt,
        }
        for task in catalog.tasks
    ]
    material = json.dumps(
        {"catalog": catalog.catalog_digest, "tasks": tasks},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _looks_secret(payload: str) -> bool:
    lowered = payload.casefold()
    return any(marker in lowered for marker in ("authorization", "bearer ", "sk-"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", default="gpt-5-mini")
    parser.add_argument("--trials", type=int, default=30)
    args = parser.parse_args(argv)
    if args.live:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            artifact = skipped_artifact("openai", args.model, "missing_openai_api_key")
        else:
            try:
                adapter: ModelAdapter = OpenAIResponsesAdapter(args.model, api_key=key)
            except ModuleNotFoundError:
                artifact = skipped_artifact("openai", args.model, "openai_sdk_unavailable")
            else:
                artifact = run_evaluation(adapter, config=EvalConfig(trials=args.trials))
    else:
        artifact = run_evaluation(
            OfflineFixtureAdapter(), config=EvalConfig(trials=args.trials)
        )
    write_artifact(artifact, args.artifact)
    print(json.dumps({"release_ready": artifact.release_ready, "status": artifact.status}))
    return 0 if artifact.status == "skipped" or artifact.release_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
