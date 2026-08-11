from __future__ import annotations

import json

from benchmarks.harness import (
    CATALOG_FIXTURE,
    FIXTURE_DIR,
    INITIAL_REDUCTION_THRESHOLD_PERCENT,
    TOTAL_REDUCTION_THRESHOLD_PERCENT,
    assert_release_thresholds,
    load_fixtures,
    run_benchmark,
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


def test_run_is_deterministic_and_uses_expected_target_revisions() -> None:
    first = run_benchmark()
    second = run_benchmark()

    assert first == second
    assert all(
        result.selected_revision == result.expected_target_revision for result in first.results
    )
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
