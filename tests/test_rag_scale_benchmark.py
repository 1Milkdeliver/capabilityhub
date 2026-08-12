from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.rag_scale import (
    QUALITY_FIXTURES,
    DiskRagIndex,
    DiskRagProvider,
    dataset_digest,
    run_rag_scale_benchmark,
    stream_chunks,
    validate_release_artifact,
    write_artifact,
)


def test_streamed_disk_index_executes_real_top_k_quality_queries(tmp_path) -> None:
    index = DiskRagIndex(tmp_path / "rag.sqlite3")
    assert index.build(stream_chunks(10_000)) == 10_000
    provider = DiskRagProvider(index)

    for expected, query in QUALITY_FIXTURES:
        result = provider.retrieve(query, top_k=5)
        assert result["results"][0]["chunk_id"] == expected
        assert result["results"][0]["citation"].startswith("corpus/")


def test_fixed_seed_digest_replays_without_materializing_corpus() -> None:
    assert dataset_digest(10_000) == dataset_digest(10_000)
    assert dataset_digest(10_000) != dataset_digest(10_000, seed=123)


def test_ci_run_records_cold_warm_concurrent_quality_and_hard_limits(tmp_path) -> None:
    report = run_rag_scale_benchmark(
        chunk_count=10_000,
        concurrent_reads=4,
        warm_repetitions=2,
        directory=tmp_path / "run",
    )
    assert report.chunk_count == 10_000
    assert report.concurrent.samples == 4
    assert report.concurrent_quality_hits == 4
    assert all(item.rank == 1 for item in report.quality)
    assert report.index_bytes > 0
    assert report.index_bytes <= report.hard_limits["max_index_bytes"]
    assert 0 <= report.cold.p50_ms <= report.cold.p95_ms <= report.cold.max_ms
    assert 0 <= report.warm.p50_ms <= report.warm.p95_ms <= report.warm.max_ms
    assert "--chunks 10000" in report.replay_command

    artifact = write_artifact(report, tmp_path / "evidence.json")
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["schema"] == "capabilityhub.rag-scale-evidence.v1"
    assert payload["dataset_digest"] == report.dataset_digest


def test_hard_index_limit_fails_closed(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="hard resource limit"):
        run_rag_scale_benchmark(
            chunk_count=10_000,
            concurrent_reads=1,
            warm_repetitions=1,
            directory=tmp_path,
            max_index_bytes=1,
        )


def test_checked_in_1m_release_artifact_is_strictly_validated() -> None:
    payload = validate_release_artifact("benchmarks/artifacts/rag-scale-1m.json")
    assert payload["chunk_count"] == 1_000_000
    assert payload["concurrent_quality_hits"] == payload["concurrent_reads"]


def test_release_artifact_rejects_claim_tampering(tmp_path) -> None:
    source = json.loads(
        Path("benchmarks/artifacts/rag-scale-1m.json").read_text(encoding="utf-8")
    )
    source["chunk_count"] = 999_999
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(RuntimeError, match="validation failed"):
        validate_release_artifact(tampered)
