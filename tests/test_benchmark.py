from __future__ import annotations

import json

from benchmarks.harness import (
    CATALOG_FIXTURE,
    FIXTURE_DIR,
    INITIAL_REDUCTION_THRESHOLD_PERCENT,
    TOTAL_REDUCTION_THRESHOLD_PERCENT,
    VALIDATION_EVENTS_ARTIFACT,
    VALIDATION_RUN_ARTIFACT,
    assert_release_thresholds,
    assert_validation_artifacts,
    build_validation_artifacts,
    load_fixtures,
    run_benchmark,
    run_cache_scenarios,
    run_failure_scenarios,
    run_validation,
)
from capabilityhub.models import CapabilityKind


def test_fixture_catalog_has_one_hundred_definitions_across_all_provider_kinds() -> None:
    fixtures = load_fixtures()

    assert len(fixtures.manifests) == 100
    assert {manifest.kind for manifest in fixtures.manifests} == set(CapabilityKind)
    assert {manifest.kind: 0 for manifest in fixtures.manifests} == {
        kind: 0 for kind in CapabilityKind
    }
    assert len(fixtures.definitions_by_revision) == 100
    assert len(fixtures.tasks) == 10
    assert len(fixtures.failure_scenarios) == 5


def test_deterministic_failure_scenarios_fail_closed_with_expected_codes() -> None:
    first = run_failure_scenarios()
    second = run_failure_scenarios()

    assert first == second
    assert {result.action for result in first} == {
        "invalid_kind",
        "no_match",
        "search_budget",
        "stale_reference",
        "tampered_reference",
    }
    assert all(result.passed for result in first)
    assert all(result.actual_code == result.expected_code for result in first)


def test_cache_modes_prove_cold_warm_and_revision_invalidation_behavior() -> None:
    first = run_cache_scenarios()
    second = run_cache_scenarios()

    assert first == second
    by_mode = {
        mode: [item for item in first if item.mode == mode]
        for mode in {item.mode for item in first}
    }
    assert set(by_mode) == {
        "cold",
        "relevant-invalidation",
        "unrelated-invalidation",
        "warm",
    }
    assert all(item.outcome == "miss" and item.materialized_tokens > 0 for item in by_mode["cold"])
    assert all(item.outcome == "hit" and item.materialized_tokens == 0 for item in by_mode["warm"])
    assert all(
        item.outcome == "miss"
        and item.materialized_tokens > 0
        and len(item.invalidated_revisions) == 1
        for item in by_mode["relevant-invalidation"]
    )
    assert all(
        item.outcome == "hit" and item.materialized_tokens == 0
        for item in by_mode["unrelated-invalidation"]
    )
    assert not any(item.stale_use for item in first)


def test_validation_artifacts_are_deterministic_and_replayable() -> None:
    report = run_validation()
    summary, events = build_validation_artifacts(report)

    assert report.release_ready
    assert summary["release_gate_passed"] is True
    assert summary["semantic_selection"] == {"accuracy": 1.0, "correct": 10, "total": 10}
    assert summary["failures"] == {"passed": 5, "total": 5}
    assert summary["events"]["count"] == 55
    assert len(events.splitlines()) == 55
    assert_validation_artifacts(VALIDATION_RUN_ARTIFACT, VALIDATION_EVENTS_ARTIFACT)


def test_eager_and_lazy_use_identical_definitions_and_meet_release_thresholds() -> None:
    report = run_benchmark()

    assert report.definitions_identical
    assert report.estimator == "utf8-bytes-div-4-v1"
    assert report.eager_catalog.utf8_bytes > report.lazy_initial.utf8_bytes > 0
    assert report.initial_reduction_percent >= INITIAL_REDUCTION_THRESHOLD_PERCENT
    assert report.total_reduction_percent >= TOTAL_REDUCTION_THRESHOLD_PERCENT
    assert report.initial_token_reduction_percent >= INITIAL_REDUCTION_THRESHOLD_PERCENT
    assert report.total_token_reduction_percent >= TOTAL_REDUCTION_THRESHOLD_PERCENT
    assert all(report.provider_coverage.values())
    assert report.load_unused_rate == 0.0
    assert all(result.selection_correct for result in report.results)
    assert_release_thresholds(report)


def test_run_is_deterministic_and_routes_prompts_without_oracle_selection() -> None:
    first = run_benchmark()
    second = run_benchmark()

    assert first == second
    assert all(
        result.selected_revision == result.expected_target_revision for result in first.results
    )
    assert all(result.query for result in first.results)
    assert all(result.selection_reason != ("no_match",) for result in first.results)
    assert all(result.selected_load.portable_tokens > 0 for result in first.results)
    assert all(result.search_card.portable_tokens > 0 for result in first.results)
    assert all(
        result.lazy_total.portable_tokens < result.eager_full.portable_tokens
        for result in first.results
    )


def test_fixture_definitions_are_the_exact_eager_payload_records() -> None:
    fixtures = load_fixtures()
    raw_catalog = json.loads((FIXTURE_DIR / CATALOG_FIXTURE).read_text(encoding="utf-8"))
    fixture_revisions = {manifest.identity.revision for manifest in fixtures.manifests}

    assert len(raw_catalog["capabilities"]) == 100
    assert fixture_revisions == set(fixtures.definitions_by_revision)


def test_pinned_reference_run_matches_the_current_meta_tool_contract() -> None:
    report = run_benchmark()
    reference = json.loads((FIXTURE_DIR.parent / "reference-run.json").read_text(encoding="utf-8"))
    accounting = reference["accounting"]

    assert accounting["lazy_initial_fixed_meta_tools"] == {
        "utf8_bytes": report.lazy_initial.utf8_bytes,
        "portable_tokens": report.lazy_initial.portable_tokens,
    }
    assert accounting["initial_reduction"] == {
        "utf8_reduction_percent": report.initial_reduction_percent,
        "portable_token_reduction_percent": report.initial_token_reduction_percent,
    }
    assert accounting["mean_lazy_task_sequence"] == {
        "sequence": ["fixed meta-tools", "one expected search card", "one selected definition"],
        "utf8_reduction_percent": report.total_reduction_percent,
        "portable_token_reduction_percent": report.total_token_reduction_percent,
    }
