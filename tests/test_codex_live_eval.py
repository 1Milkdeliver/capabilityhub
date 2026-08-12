from __future__ import annotations

import json
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from benchmarks.codex_live_eval import (
    BatchResult,
    CodexCLIAdapter,
    CodexUsage,
    main,
    parse_codex_jsonl,
    run_live_evaluation,
    validate_live_artifact,
    write_artifact,
)
from benchmarks.harness import FixtureCatalog, load_fixtures


class _BatchAdapter(CodexCLIAdapter):
    def __init__(self) -> None:
        super().__init__("never-run")
        self.calls: list[tuple[str, int]] = []

    def evaluate_batch(
        self, catalog: FixtureCatalog, mode: str, *, round_number: int
    ) -> BatchResult:
        self.calls.append((mode, round_number))
        selections = {
            task.task_id: task.expected_target_revision for task in catalog.tasks
        }
        eager = mode == "eager"
        usage = CodexUsage(
            input_tokens=10_000 if eager else 5_000,
            cached_input_tokens=1_000 if eager else 500,
            output_tokens=500,
            reasoning_tokens=200,
            tool_calls=0,
        )
        return BatchResult(
            selections, usage, 100 if eager else 80, "gpt-test", "v1", "low"
        )


def test_parser_extracts_real_usage_message_model_and_tools() -> None:
    results = {
        "results": [
            {"task_id": f"task-{index}", "selected_revision": f"revision-{index}"}
            for index in range(10)
        ]
    }
    events = [
        {"type": "thread.started", "model": "gpt-5", "version": "2026-08"},
        {"type": "item.completed", "item": {"type": "tool_call"}},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(results)},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1200,
                "cached_input_tokens": 400,
                "output_tokens": 90,
                "output_tokens_details": {"reasoning_tokens": 30},
            },
        },
    ]
    parsed = parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in events), latency_ms=12.5
    )

    assert parsed.usage == CodexUsage(1200, 400, 90, 30, 1)
    assert parsed.model == "gpt-5"
    assert parsed.version == "2026-08"
    assert len(parsed.selections) == 10


def test_batched_runner_makes_six_calls_and_thirty_pairs() -> None:
    adapter = _BatchAdapter()
    artifact = run_live_evaluation(
        adapter,
        bootstrap_samples=100,
        seed=7,
        max_token_ratio=0.75,
    )

    assert adapter.calls == [
        ("eager", 0),
        ("lazy", 0),
        ("eager", 1),
        ("lazy", 1),
        ("eager", 2),
        ("lazy", 2),
    ]
    assert artifact.calls == 6
    assert artifact.paired_observations == 30
    assert len(artifact.observations) == 30
    assert artifact.noninferiority_passed is True
    assert artifact.cost_passed is True
    assert artifact.release_ready is True
    assert artifact.total_usage["eager"].input_tokens == 30_000
    assert artifact.total_usage["lazy"].cached_input_tokens == 1_500
    assert artifact.baseline_overhead_tokens["eager"] >= 0


def test_default_and_ci_paths_never_invoke_codex(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    destination = tmp_path / "skipped.json"
    assert main(["--artifact", str(destination)]) == 0
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["status"] == "skipped"
    assert payload["skip_reason"] == "live_flag_required"

    monkeypatch.setenv("CI", "true")
    assert main(["--live", "--artifact", str(destination)]) == 0
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["skip_reason"] == "live_evaluation_disabled_in_ci"


def test_parser_rejects_missing_usage_and_duplicate_tasks() -> None:
    message = {"results": [{"task_id": "same", "selected_revision": "one"}] * 2}
    event = {"item": {"type": "agent_message", "text": json.dumps(message)}}
    with pytest.raises(ValueError, match="no token usage"):
        parse_codex_jsonl(json.dumps(event), latency_ms=1)

    usage = {"usage": {"input_tokens": 1, "output_tokens": 1}}
    with pytest.raises(ValueError, match="duplicate"):
        parse_codex_jsonl(
            json.dumps(event) + "\n" + json.dumps(usage), latency_ms=1
        )


def test_fixed_fixture_really_contains_ten_tasks() -> None:
    assert len(load_fixtures().tasks) == 10


def test_release_validator_requires_complete_real_usage(tmp_path: Path) -> None:
    destination = tmp_path / "live.json"
    write_artifact(
        run_live_evaluation(_BatchAdapter(), bootstrap_samples=100, seed=7),
        destination,
    )
    assert validate_live_artifact(destination)["release_ready"] is True

    value = json.loads(destination.read_text(encoding="utf-8"))
    value["total_usage"]["lazy"]["tool_calls"] = 1
    destination.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="provider usage"):
        validate_live_artifact(destination)
