from __future__ import annotations

import time

import pytest

from benchmarks.scale import (
    DEFAULT_CAPABILITY_COUNT,
    DEFAULT_CONCURRENT_READS,
    DEFAULT_SEED,
    LatencyStats,
    MetadataSearchIndex,
    generate_metadata_catalog,
    run_scale_benchmark,
)
from capabilityhub.search import SearchRankingConfig


@pytest.fixture(scope="module")
def scale_report():
    started = time.perf_counter()
    report = run_scale_benchmark()
    elapsed = time.perf_counter() - started
    assert elapsed < 30.0
    return report


def test_default_run_measures_10k_and_at_least_100_concurrent_reads(
    scale_report,
) -> None:
    assert scale_report.seed == DEFAULT_SEED
    assert scale_report.capability_count == DEFAULT_CAPABILITY_COUNT == 10_000
    assert scale_report.concurrent_read_target == DEFAULT_CONCURRENT_READS >= 100
    assert scale_report.concurrent.samples == scale_report.concurrent_read_target
    assert scale_report.concurrent_quality_hits == scale_report.concurrent_read_target


def test_quality_fixtures_hit_expected_capability_in_top3(scale_report) -> None:
    assert scale_report.top_k == 8
    assert len(scale_report.quality) >= 1
    assert all(evidence.top8_hit for evidence in scale_report.quality)
    assert all(
        evidence.top3_hit and evidence.rank is not None and evidence.rank <= 3
        for evidence in scale_report.quality
    )


@pytest.mark.parametrize("path", ["cold", "warm", "concurrent"])
def test_latency_summary_has_ordered_percentiles_and_wide_ci_cap(scale_report, path: str) -> None:
    latency: LatencyStats = getattr(scale_report, path)
    assert latency.samples > 0
    assert 0 <= latency.p50_ms <= latency.p95_ms <= latency.max_ms
    assert latency.max_ms < 10_000
    assert scale_report.catalog_build_ms < 10_000


def test_fixed_seed_replays_identical_dataset_digest(scale_report) -> None:
    catalog, fixtures = generate_metadata_catalog(count=DEFAULT_CAPABILITY_COUNT, seed=DEFAULT_SEED)
    second = run_scale_benchmark(
        capability_count=DEFAULT_CAPABILITY_COUNT,
        concurrent_reads=100,
        seed=DEFAULT_SEED,
        warm_repetitions=1,
    )

    assert len(catalog) == 10_000
    assert len(fixtures) == len(scale_report.quality)
    assert second.dataset_digest == scale_report.dataset_digest


def test_report_records_replay_environment_and_scope_limits(scale_report) -> None:
    assert {
        "logical_cpu_count",
        "machine",
        "os",
        "os_release",
        "processor",
        "python_implementation",
        "python_version",
    } <= set(scale_report.hardware)
    scope = " ".join(scale_report.scope_limits).lower()
    assert "1m-document rag" in scope
    assert "model-quality" in scope
    assert "production-provider" in scope
    assert scale_report.ranking_revision == "ranking-v1"
    assert scale_report.ranking_digest.startswith("sha256:")
    assert scale_report.index_revision.startswith("sha256:")


def test_invalid_scale_targets_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least 100"):
        run_scale_benchmark(concurrent_reads=99)
    with pytest.raises(ValueError, match="quality fixtures"):
        generate_metadata_catalog(count=100)


def test_ranking_config_change_updates_observable_index_revision() -> None:
    catalog, _ = generate_metadata_catalog(count=DEFAULT_CAPABILITY_COUNT, seed=DEFAULT_SEED)
    first = MetadataSearchIndex(catalog)
    default = SearchRankingConfig()
    changed = MetadataSearchIndex(
        catalog,
        ranking=SearchRankingConfig(
            revision="ranking-v2",
            weights={**default.weights, "summary": default.weights["summary"] + 1},
        ),
    )

    assert first.ranking_digest != changed.ranking_digest
    assert first.index_revision != changed.index_revision
