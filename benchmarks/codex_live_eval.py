"""Explicit, bounded Codex CLI evidence for eager-versus-lazy evaluation.

Nothing in this module calls a model unless ``--live`` is supplied.  A live run is
six subprocesses: three rounds times two modes, with all ten fixed tasks batched
into each call.  The subprocess inherits the user's existing Codex login; this
module never reads, copies, or serializes authentication material.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast

from benchmarks.harness import FixtureCatalog, load_fixtures

SCHEMA = "capabilityhub.codex-live-eval.v1"
ROUNDS = 3
TASKS_PER_CALL = 10
MODES = ("eager", "lazy")


@dataclass(frozen=True, slots=True)
class CodexUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    tool_calls: int

    @property
    def billable_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens


@dataclass(frozen=True, slots=True)
class BatchResult:
    selections: Mapping[str, str]
    usage: CodexUsage
    latency_ms: float
    model: str
    version: str
    reasoning_effort: str = "unknown"


@dataclass(frozen=True, slots=True)
class PairedObservation:
    round: int
    task_id: str
    eager_correct: bool
    lazy_correct: bool
    eager_tokens: int
    lazy_tokens: int
    eager_cost_microusd: int
    lazy_cost_microusd: int


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    low: float
    estimate: float
    high: float


@dataclass(frozen=True, slots=True)
class LiveEvaluationArtifact:
    schema: str
    status: str
    provider: str
    model: str
    version: str
    reasoning_effort: str
    config: Mapping[str, int | float]
    calls: int
    paired_observations: int
    baseline_overhead_tokens: Mapping[str, int]
    total_usage: Mapping[str, CodexUsage]
    mean_latency_ms: Mapping[str, float]
    quality_difference_ci: ConfidenceInterval
    cost_ratio_ci: ConfidenceInterval
    noninferiority_passed: bool
    cost_passed: bool
    release_ready: bool
    observations: tuple[PairedObservation, ...]
    skip_reason: str | None = None


class CodexCLIAdapter:
    """Run Codex in an empty, read-only, ephemeral directory."""

    provider = "codex-cli"

    def __init__(
        self,
        executable: str = "codex",
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int = 180,
    ) -> None:
        self.executable = executable
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self._version: str | None = None

    def evaluate_batch(
        self, catalog: FixtureCatalog, mode: str, *, round_number: int
    ) -> BatchResult:
        if mode not in MODES or round_number not in range(ROUNDS):
            raise ValueError("invalid live evaluation batch")
        if not self.model or self.reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("explicit Codex model and reasoning effort are required")
        prompt = _batch_prompt(catalog, mode, round_number)
        with tempfile.TemporaryDirectory(prefix="capabilityhub-codex-eval-") as empty:
            command = [
                self.executable,
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-C",
                empty,
                "-c",
                f'model_reasoning_effort="{self.reasoning_effort}"',
            ]
            if self.model:
                command.extend(("--model", self.model))
            # Passing the eager catalog as an argv value exceeds Windows' process
            # command-line limit.  ``codex exec -`` reads the identical prompt
            # from stdin and keeps the invocation portable.
            command.append("-")
            started = time.perf_counter_ns()
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
            latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        if completed.returncode != 0:
            raise RuntimeError("Codex CLI live evaluation failed")
        parsed = parse_codex_jsonl(completed.stdout, latency_ms=latency_ms)
        return replace(
            parsed,
            model=self.model,
            version=self._cli_version(),
            reasoning_effort=self.reasoning_effort,
        )

    def _cli_version(self) -> str:
        if self._version is not None:
            return self._version
        completed = subprocess.run(
            [self.executable, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise RuntimeError("Codex CLI version discovery failed")
        self._version = completed.stdout.strip()[:100]
        return self._version


def parse_codex_jsonl(payload: str, *, latency_ms: float) -> BatchResult:
    """Parse public JSONL events without retaining prompts or reasoning text."""

    events: list[dict[str, Any]] = []
    for line in payload.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("Codex CLI emitted invalid JSONL") from error
        if not isinstance(value, dict):
            raise ValueError("Codex CLI event must be an object")
        events.append(cast(dict[str, Any], value))
    if not events:
        raise ValueError("Codex CLI emitted no JSONL events")
    usage = _extract_usage(events)
    response = _extract_agent_message(events)
    selections = _parse_selections(response)
    model = _last_string(events, ("model", "model_name")) or "unknown"
    version = _last_string(events, ("version", "model_version")) or "unknown"
    return BatchResult(selections, usage, latency_ms, model, version)


def run_live_evaluation(
    adapter: CodexCLIAdapter,
    *,
    catalog: FixtureCatalog | None = None,
    bootstrap_samples: int = 2_000,
    seed: int = 20_260_812,
    noninferiority_margin: float = 0.05,
    max_token_ratio: float = 0.90,
    input_usd_per_million: float = 1.0,
    cached_input_usd_per_million: float = 0.25,
    output_usd_per_million: float = 4.0,
) -> LiveEvaluationArtifact:
    fixtures = catalog or load_fixtures()
    if len(fixtures.tasks) != TASKS_PER_CALL:
        raise ValueError("live evaluation requires exactly ten fixed tasks")
    batches: dict[tuple[int, str], BatchResult] = {}
    for round_number in range(ROUNDS):
        for mode in MODES:
            batch = adapter.evaluate_batch(fixtures, mode, round_number=round_number)
            if set(batch.selections) != {task.task_id for task in fixtures.tasks}:
                raise ValueError("Codex CLI result does not cover the fixed task batch")
            batches[(round_number, mode)] = batch
    observations = _observations(
        fixtures,
        batches,
        input_usd_per_million=input_usd_per_million,
        cached_input_usd_per_million=cached_input_usd_per_million,
        output_usd_per_million=output_usd_per_million,
    )
    quality = _paired_interval(
        observations,
        lambda item: float(item.lazy_correct) - float(item.eager_correct),
        bootstrap_samples,
        seed,
    )
    cost_ratio = _ratio_interval(observations, bootstrap_samples, seed + 1)
    totals = {mode: _sum_usage(batches, mode) for mode in MODES}
    overhead = {
        mode: max(0, totals[mode].input_tokens - _estimated_task_tokens(fixtures, mode) * ROUNDS)
        for mode in MODES
    }
    noninferior = quality.low >= -noninferiority_margin
    cost_passed = cost_ratio.high <= max_token_ratio
    first = batches[(0, "eager")]
    return LiveEvaluationArtifact(
        SCHEMA,
        "complete",
        adapter.provider,
        first.model,
        first.version,
        first.reasoning_effort,
        {
            "bootstrap_samples": bootstrap_samples,
            "cached_input_usd_per_million": cached_input_usd_per_million,
            "input_usd_per_million": input_usd_per_million,
            "max_cost_ratio": max_token_ratio,
            "noninferiority_margin": noninferiority_margin,
            "output_usd_per_million": output_usd_per_million,
            "rounds": ROUNDS,
            "seed": seed,
            "tasks_per_call": TASKS_PER_CALL,
        },
        ROUNDS * len(MODES),
        len(observations),
        overhead,
        totals,
        {
            mode: statistics.fmean(
                batches[(round_number, mode)].latency_ms for round_number in range(ROUNDS)
            )
            for mode in MODES
        },
        quality,
        cost_ratio,
        noninferior,
        cost_passed,
        noninferior and cost_passed,
        observations,
    )


def skipped_artifact(reason: str) -> LiveEvaluationArtifact:
    zero = ConfidenceInterval(0.0, 0.0, 0.0)
    return LiveEvaluationArtifact(
        SCHEMA,
        "skipped",
        "codex-cli",
        "unknown",
        "unknown",
        "unknown",
        {},
        0,
        0,
        {},
        {},
        {},
        zero,
        zero,
        False,
        False,
        False,
        (),
        reason,
    )


def write_artifact(artifact: LiveEvaluationArtifact, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(artifact), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def validate_live_artifact(path: str | Path) -> Mapping[str, Any]:
    """Fail closed unless an artifact contains real, complete provider evidence."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("live evaluation artifact is invalid") from error
    if not isinstance(value, dict):
        raise ValueError("live evaluation artifact is invalid")
    artifact = cast(dict[str, Any], value)
    required = {
        "schema": SCHEMA,
        "status": "complete",
        "provider": "codex-cli",
        "calls": ROUNDS * len(MODES),
        "paired_observations": ROUNDS * TASKS_PER_CALL,
        "noninferiority_passed": True,
        "cost_passed": True,
        "release_ready": True,
        "skip_reason": None,
    }
    if any(artifact.get(key) != expected for key, expected in required.items()):
        raise ValueError("live evaluation artifact did not pass")
    if not isinstance(artifact.get("model"), str) or not artifact["model"]:
        raise ValueError("live evaluation model is missing")
    if artifact.get("reasoning_effort") not in {"low", "medium", "high"}:
        raise ValueError("live evaluation reasoning effort is invalid")
    observations = artifact.get("observations")
    totals = artifact.get("total_usage")
    if not isinstance(observations, list) or len(observations) != ROUNDS * TASKS_PER_CALL:
        raise ValueError("live evaluation observations are incomplete")
    if not isinstance(totals, dict):
        raise ValueError("live evaluation usage is missing")
    for mode in MODES:
        usage = totals.get(mode)
        if (
            not isinstance(usage, dict)
            or not isinstance(usage.get("input_tokens"), int)
            or usage["input_tokens"] <= 0
            or usage.get("tool_calls") != 0
        ):
            raise ValueError("live evaluation provider usage is invalid")
    return artifact


def _batch_prompt(catalog: FixtureCatalog, mode: str, round_number: int) -> str:
    tasks = [{"id": task.task_id, "task": task.prompt} for task in catalog.tasks]
    if mode == "eager":
        context: object = list(catalog.definitions_by_revision.values())
        instruction = "Select from the complete capability definitions."
    else:
        context = [
            {
                "revision": manifest.identity.revision,
                "kind": manifest.kind.value,
                "summary": manifest.summary,
            }
            for manifest in catalog.manifests
        ]
        instruction = "Select from compact capability cards; no tools are needed."
    request = {"context": context, "mode": mode, "round": round_number, "tasks": tasks}
    return (
        "This is a fixed routing evaluation. Do not use tools, skills, files, network, or shell. "
        + instruction
        + " Return one compact JSON object only: {\"results\":[{\"task_id\":string,"
        "\"selected_revision\":string}]}. Include every task exactly once. Input:"
        + json.dumps(request, sort_keys=True, separators=(",", ":"))
    )


def _extract_usage(events: Sequence[Mapping[str, Any]]) -> CodexUsage:
    candidates: list[CodexUsage] = []
    tool_calls = sum(_is_tool_event(event) for event in events)
    for event in events:
        for value in _mappings(event):
            if "input_tokens" not in value and "output_tokens" not in value:
                continue
            details = value.get("input_tokens_details")
            output_details = value.get("output_tokens_details")
            cached = _integer(value.get("cached_input_tokens"))
            if isinstance(details, Mapping):
                cached = max(cached, _integer(details.get("cached_tokens")))
            reasoning = _integer(value.get("reasoning_tokens"))
            if isinstance(output_details, Mapping):
                reasoning = max(reasoning, _integer(output_details.get("reasoning_tokens")))
            candidates.append(
                CodexUsage(
                    _integer(value.get("input_tokens")),
                    cached,
                    _integer(value.get("output_tokens")),
                    reasoning,
                    tool_calls,
                )
            )
    if not candidates:
        raise ValueError("Codex CLI JSONL has no token usage")
    return max(candidates, key=lambda item: item.billable_tokens)


def _extract_agent_message(events: Sequence[Mapping[str, Any]]) -> str:
    messages: list[str] = []
    for event in events:
        for value in _mappings(event):
            if value.get("type") == "agent_message":
                text = value.get("text") or value.get("content")
                if isinstance(text, str):
                    messages.append(text)
    if not messages:
        raise ValueError("Codex CLI JSONL has no agent message")
    return messages[-1]


def _parse_selections(message: str) -> Mapping[str, str]:
    try:
        payload = json.loads(message)
        rows = payload["results"]
        result = {str(row["task_id"]): str(row["selected_revision"]) for row in rows}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Codex CLI result schema is invalid") from error
    if not result or len(result) != len(rows):
        raise ValueError("Codex CLI result has duplicate tasks")
    return result


def _observations(
    catalog: FixtureCatalog,
    batches: Mapping[tuple[int, str], BatchResult],
    *,
    input_usd_per_million: float,
    cached_input_usd_per_million: float,
    output_usd_per_million: float,
) -> tuple[PairedObservation, ...]:
    result: list[PairedObservation] = []
    for round_number in range(ROUNDS):
        eager = batches[(round_number, "eager")]
        lazy = batches[(round_number, "lazy")]
        eager_tokens = _allocate(eager.usage.billable_tokens, len(catalog.tasks))
        lazy_tokens = _allocate(lazy.usage.billable_tokens, len(catalog.tasks))
        eager_cost = _allocate(
            _cost_microusd(
                eager.usage,
                input_usd_per_million,
                cached_input_usd_per_million,
                output_usd_per_million,
            ),
            len(catalog.tasks),
        )
        lazy_cost = _allocate(
            _cost_microusd(
                lazy.usage,
                input_usd_per_million,
                cached_input_usd_per_million,
                output_usd_per_million,
            ),
            len(catalog.tasks),
        )
        for index, task in enumerate(catalog.tasks):
            result.append(
                PairedObservation(
                    round_number,
                    task.task_id,
                    eager.selections[task.task_id] == task.expected_target_revision,
                    lazy.selections[task.task_id] == task.expected_target_revision,
                    eager_tokens[index],
                    lazy_tokens[index],
                    eager_cost[index],
                    lazy_cost[index],
                )
            )
    return tuple(result)


def _paired_interval(
    observations: Sequence[PairedObservation],
    metric: Any,
    samples: int,
    seed: int,
) -> ConfidenceInterval:
    if len(observations) < 30 or samples < 100:
        raise ValueError("at least 30 paired observations and 100 bootstrap samples required")
    rng = random.Random(seed)
    values = [float(metric(item)) for item in observations]
    estimates = sorted(
        statistics.fmean(values[rng.randrange(len(values))] for _ in values)
        for _ in range(samples)
    )
    return _interval(estimates, statistics.fmean(values))


def _ratio_interval(
    observations: Sequence[PairedObservation], samples: int, seed: int
) -> ConfidenceInterval:
    rng = random.Random(seed)

    def ratio(items: Sequence[PairedObservation]) -> float:
        eager = sum(item.eager_cost_microusd for item in items)
        return (
            sum(item.lazy_cost_microusd for item in items) / eager
            if eager
            else math.inf
        )

    estimates = sorted(
        ratio([observations[rng.randrange(len(observations))] for _ in observations])
        for _ in range(samples)
    )
    return _interval(estimates, ratio(observations))


def _interval(values: Sequence[float], estimate: float) -> ConfidenceInterval:
    return ConfidenceInterval(
        round(values[int(len(values) * 0.025)], 8),
        round(estimate, 8),
        round(values[min(len(values) - 1, int(len(values) * 0.975))], 8),
    )


def _sum_usage(batches: Mapping[tuple[int, str], BatchResult], mode: str) -> CodexUsage:
    selected = [batches[(round_number, mode)].usage for round_number in range(ROUNDS)]
    values = (
        sum(getattr(item, field) for item in selected)
        for field in CodexUsage.__dataclass_fields__
    )
    return CodexUsage(*values)


def _estimated_task_tokens(catalog: FixtureCatalog, mode: str) -> int:
    prompts = sum(len(task.prompt) for task in catalog.tasks) // 4
    if mode == "eager":
        return prompts + sum(len(value) for value in catalog.definitions_by_revision.values()) // 4
    return prompts + sum(len(manifest.summary) for manifest in catalog.manifests) // 4


def _cost_microusd(
    usage: CodexUsage,
    input_price: float,
    cached_input_price: float,
    output_price: float,
) -> int:
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
    return round(
        uncached * input_price
        + usage.cached_input_tokens * cached_input_price
        + usage.output_tokens * output_price
    )


def _allocate(total: int, count: int) -> tuple[int, ...]:
    quotient, remainder = divmod(total, count)
    return tuple(quotient + (1 if index < remainder else 0) for index in range(count))


def _mappings(value: object) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, Any], value)
        result.append(mapping)
        for child in mapping.values():
            result.extend(_mappings(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_mappings(child))
    return result


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _is_tool_event(event: Mapping[str, Any]) -> int:
    kinds = {str(value.get("type", "")) for value in _mappings(event)}
    return int(bool(kinds & {"command_execution", "mcp_tool_call", "tool_call"}))


def _last_string(events: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> str | None:
    found: str | None = None
    for event in events:
        for value in _mappings(event):
            for key in keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    found = candidate
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"))
    args = parser.parse_args(argv)
    if not args.live:
        artifact = skipped_artifact("live_flag_required")
    elif os.environ.get("CI"):
        artifact = skipped_artifact("live_evaluation_disabled_in_ci")
    elif not args.model or not args.reasoning_effort:
        artifact = skipped_artifact("explicit_model_and_effort_required")
    else:
        artifact = run_live_evaluation(
            CodexCLIAdapter(
                args.codex,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
        )
    write_artifact(artifact, args.artifact)
    print(json.dumps({"calls": artifact.calls, "status": artifact.status}))
    return 0 if artifact.status == "skipped" or artifact.release_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
