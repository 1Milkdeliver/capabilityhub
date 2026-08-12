from __future__ import annotations

import json

import pytest

from benchmarks.harness import BenchmarkTask, FixtureCatalog, load_fixtures
from benchmarks.model_eval import (
    EvalConfig,
    ModelOutcome,
    ModelUsage,
    OfflineFixtureAdapter,
    main,
    run_evaluation,
    skipped_artifact,
    validate_artifact,
    write_artifact,
)


def test_offline_runner_uses_fixed_tasks_30_trials_and_passes_gates(tmp_path) -> None:
    fixtures = load_fixtures()
    artifact = run_evaluation(
        OfflineFixtureAdapter(),
        config=EvalConfig(trials=30, bootstrap_samples=200, seed=7),
        catalog=fixtures,
    )

    assert artifact.release_ready is True
    assert len(artifact.trials) == 30 * len(fixtures.tasks) * 2
    assert (
        artifact.summaries["lazy"].mean_input_tokens
        < artifact.summaries["eager"].mean_input_tokens
    )
    assert artifact.summaries["lazy"].mean_tool_calls == 1
    assert artifact.accuracy_difference_ci.low >= -0.05
    assert artifact.cost_ratio_ci.high <= 0.90
    path = write_artifact(artifact, tmp_path / "model-eval.json")
    payload = validate_artifact(path)
    assert payload["schema"] == "capabilityhub.model-eval.v1"
    assert "prompt" not in path.read_text(encoding="utf-8")


class _InferiorAdapter:
    provider = "fixture"
    model = "inferior"

    def evaluate(
        self, task: BenchmarkTask, mode: str, _catalog: FixtureCatalog
    ) -> ModelOutcome:
        selected = task.expected_target_revision if mode == "eager" else "wrong"
        return ModelOutcome(selected, True, ModelUsage(100, 10, 0, 0), 1)


def test_non_inferiority_gate_rejects_quality_regression() -> None:
    artifact = run_evaluation(
        _InferiorAdapter(), config=EvalConfig(trials=30, bootstrap_samples=100)
    )
    assert artifact.non_inferiority_passed is False
    assert artifact.release_ready is False


def test_live_cli_without_credentials_writes_honest_skip(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    destination = tmp_path / "skip.json"

    assert main(["--live", "--artifact", str(destination)]) == 0

    payload = validate_artifact(destination, require_complete=False)
    assert payload["status"] == "skipped"
    assert payload["skip_reason"] == "missing_openai_api_key"
    assert payload["trials"] == []


def test_artifact_writer_rejects_secret_markers(tmp_path) -> None:
    artifact = skipped_artifact("openai", "sk-secret-canary", "missing")
    with pytest.raises(ValueError, match="secret"):
        write_artifact(artifact, tmp_path / "unsafe.json")


def test_schema_validation_rejects_tampered_trial_shape(tmp_path) -> None:
    artifact = run_evaluation(
        OfflineFixtureAdapter(), config=EvalConfig(trials=30, bootstrap_samples=100)
    )
    destination = write_artifact(artifact, tmp_path / "valid.json")
    payload = json.loads(destination.read_text(encoding="utf-8"))
    payload["trials"][0]["raw_output"] = "not allowed"
    destination.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="validation failed"):
        validate_artifact(destination)
