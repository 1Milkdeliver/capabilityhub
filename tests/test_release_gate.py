from __future__ import annotations

from pathlib import Path

from benchmarks.release_gate import (
    CATALOG_COUNT,
    CONCURRENT_EXECUTIONS,
    LOAD_P95_LIMIT_MS,
    SEARCH_P95_LIMIT_MS,
    run_release_gate,
)


def test_release_gate_uses_real_10k_search_load_and_100_executions() -> None:
    report = run_release_gate()

    assert report.tenant_count == 1
    assert report.catalog_count == CATALOG_COUNT == 10_000
    assert report.search.samples >= 30
    assert report.search.p95_ms < SEARCH_P95_LIMIT_MS == 150
    assert report.cached_load.samples == CONCURRENT_EXECUTIONS
    assert report.cached_load.p95_ms < LOAD_P95_LIMIT_MS == 75
    assert report.concurrent_execution.samples == CONCURRENT_EXECUTIONS == 100
    assert report.successful_executions == CONCURRENT_EXECUTIONS
    assert report.hardware["logical_cpu_count"] is not None


def test_ci_has_fast_artifact_gate_and_opt_in_real_1m_replay() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "validate_release_artifact('benchmarks/artifacts/rag-scale-1m.json')" in workflow
    assert "from benchmarks.release_gate import run_release_gate" in workflow
    assert "workflow_dispatch:" in workflow
    assert "schedule:" in workflow
    assert "--chunks 1000000 --seed 20260812 --concurrency 32" in workflow
    assert "python -m benchmarks.wheel_smoke" in workflow
