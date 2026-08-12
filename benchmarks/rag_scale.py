"""Replayable disk-backed RAG index benchmark up to one million chunks."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import sqlite3
import tempfile
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_SEED = 20_260_812
CI_CHUNKS = 10_000
FULL_CHUNKS = 1_000_000
DEFAULT_TOP_K = 5
QUALITY_FIXTURES = (
    (37, "amber zephyr ledger"),
    (1_037, "cobalt orchard telemetry"),
    (7_919, "violet harbor contract"),
)


@dataclass(frozen=True, slots=True)
class RagHit:
    chunk_id: int
    citation: str
    text: str
    score: float


@dataclass(frozen=True, slots=True)
class Latency:
    samples: int
    p50_ms: float
    p95_ms: float
    max_ms: float


@dataclass(frozen=True, slots=True)
class QualityEvidence:
    query: str
    expected_chunk_id: int
    rank: int | None


@dataclass(frozen=True, slots=True)
class RagScaleReport:
    schema: str
    seed: int
    chunk_count: int
    top_k: int
    build_ms: float
    cold: Latency
    warm: Latency
    concurrent: Latency
    concurrent_reads: int
    concurrent_quality_hits: int
    quality: tuple[QualityEvidence, ...]
    dataset_digest: str
    index_bytes: int
    hard_limits: dict[str, float | int]
    hardware: dict[str, str | int | None]
    replay_command: str


class DiskRagIndex:
    """SQLite FTS5 index with streaming writes and per-query read connections."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    def build(self, chunks: Iterable[tuple[int, str, str]]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("DROP TABLE IF EXISTS chunks")
            connection.execute(
                "CREATE VIRTUAL TABLE chunks USING fts5("
                "chunk_id UNINDEXED, citation UNINDEXED, text)"
            )
            count = 0
            batch: list[tuple[int, str, str]] = []
            for chunk in chunks:
                batch.append(chunk)
                if len(batch) == 2_000:
                    connection.executemany("INSERT INTO chunks VALUES (?, ?, ?)", batch)
                    count += len(batch)
                    batch.clear()
            if batch:
                connection.executemany("INSERT INTO chunks VALUES (?, ?, ?)", batch)
                count += len(batch)
            connection.execute("INSERT INTO chunks(chunks) VALUES ('optimize')")
        return count

    def search(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> tuple[RagHit, ...]:
        if not query.strip() or not 1 <= top_k <= 20:
            raise ValueError("query and top_k are outside benchmark bounds")
        phrase = '"' + query.replace('"', '""') + '"'
        uri = f"file:{self.path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
            return self._search_connection(connection, phrase, top_k)

    @staticmethod
    def _search_connection(
        connection: sqlite3.Connection, phrase: str, top_k: int
    ) -> tuple[RagHit, ...]:
        rows = connection.execute(
            "SELECT chunk_id, citation, text, bm25(chunks) FROM chunks "
            "WHERE chunks MATCH ? ORDER BY bm25(chunks), CAST(chunk_id AS INTEGER) LIMIT ?",
            (phrase, top_k),
        ).fetchall()
        return tuple(
            RagHit(int(row[0]), str(row[1]), str(row[2]), float(row[3])) for row in rows
        )


class DiskRagProvider:
    """Minimal benchmark provider proving retrieval uses the on-disk index."""

    def __init__(self, index: DiskRagIndex) -> None:
        self.index = index

    def retrieve(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> dict[str, Any]:
        return {
            "results": [asdict(hit) for hit in self.index.search(query, top_k=top_k)],
            "top_k": top_k,
        }


def stream_chunks(count: int, *, seed: int = DEFAULT_SEED) -> Iterator[tuple[int, str, str]]:
    if count <= max(index for index, _ in QUALITY_FIXTURES):
        raise ValueError("chunk count is too small for fixed quality fixtures")
    rng = random.Random(seed)
    fixtures = dict(QUALITY_FIXTURES)
    vocabulary = tuple(f"term{index:04d}" for index in range(2_048))
    for chunk_id in range(count):
        terms = rng.sample(vocabulary, 6)
        marker = fixtures.get(chunk_id)
        text = " ".join((*terms, marker or "ordinary retrieval chunk"))
        yield chunk_id, f"corpus/{chunk_id // 1_000:04d}.txt#L{chunk_id % 1_000 + 1}", text


def dataset_digest(count: int, *, seed: int = DEFAULT_SEED) -> str:
    digest = hashlib.sha256()
    for chunk_id, citation, text in stream_chunks(count, seed=seed):
        digest.update(f"{chunk_id}\0{citation}\0{text}\n".encode())
    return "sha256:" + digest.hexdigest()


def run_rag_scale_benchmark(
    *,
    chunk_count: int = CI_CHUNKS,
    seed: int = DEFAULT_SEED,
    concurrent_reads: int = 8,
    warm_repetitions: int = 5,
    directory: str | Path | None = None,
    max_build_seconds: float = 900,
    max_index_bytes: int | None = None,
    max_p95_ms: float = 5_000,
) -> RagScaleReport:
    """Build and query a real disk index, raising when evidence exceeds hard caps."""

    minimum = max(index for index, _ in QUALITY_FIXTURES) + 1
    if not minimum <= chunk_count <= FULL_CHUNKS:
        raise ValueError(f"chunk_count must be from {minimum} to 1000000")
    if concurrent_reads < 1 or warm_repetitions < 1:
        raise ValueError("read counts must be positive")
    selected_max_bytes = max_index_bytes or chunk_count * 1_024
    owned: tempfile.TemporaryDirectory[str] | None = None
    if directory is None:
        owned = tempfile.TemporaryDirectory()
        root = Path(owned.name)
    else:
        root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    index = DiskRagIndex(root / "rag-index.sqlite3")
    started = time.perf_counter_ns()
    built = index.build(stream_chunks(chunk_count, seed=seed))
    build_ms = _elapsed_ms(started)
    if built != chunk_count:
        raise RuntimeError("indexed chunk count did not match requested scale")

    queries = tuple(query for _, query in QUALITY_FIXTURES)
    cold = [
        _timed(lambda query=query: index.search(query))
        for _ in range(warm_repetitions)
        for query in queries
    ]
    uri = f"file:{index.path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        warm = [
            _timed(
                lambda query=query: index._search_connection(
                    connection, '"' + query.replace('"', '""') + '"', DEFAULT_TOP_K
                )
            )
            for _ in range(warm_repetitions)
            for query in queries
        ]

    def concurrent_query(position: int) -> tuple[float, bool]:
        expected, query = QUALITY_FIXTURES[position % len(QUALITY_FIXTURES)]
        started_at = time.perf_counter_ns()
        hits = index.search(query)
        return _elapsed_ms(started_at), bool(hits and hits[0].chunk_id == expected)

    with ThreadPoolExecutor(max_workers=concurrent_reads) as executor:
        concurrent_results = tuple(executor.map(concurrent_query, range(concurrent_reads)))

    quality = tuple(_quality(index, expected, query) for expected, query in QUALITY_FIXTURES)
    index_bytes = sum(
        path.stat().st_size
        for path in root.glob("rag-index.sqlite3*")
        if path.is_file()
    )
    limits: dict[str, float | int] = {
        "max_build_seconds": max_build_seconds,
        "max_index_bytes": selected_max_bytes,
        "max_p95_ms": max_p95_ms,
        "max_chunks": FULL_CHUNKS,
    }
    latencies = (_latency(cold), _latency(warm), _latency(item[0] for item in concurrent_results))
    if build_ms > max_build_seconds * 1_000 or index_bytes > selected_max_bytes:
        raise RuntimeError("RAG scale build exceeded a hard resource limit")
    if any(item.p95_ms > max_p95_ms for item in latencies):
        raise RuntimeError("RAG scale query exceeded the p95 hard limit")
    report = RagScaleReport(
        schema="capabilityhub.rag-scale-evidence.v1",
        seed=seed,
        chunk_count=chunk_count,
        top_k=DEFAULT_TOP_K,
        build_ms=round(build_ms, 6),
        cold=latencies[0],
        warm=latencies[1],
        concurrent=latencies[2],
        concurrent_reads=concurrent_reads,
        concurrent_quality_hits=sum(item[1] for item in concurrent_results),
        quality=quality,
        dataset_digest=dataset_digest(chunk_count, seed=seed),
        index_bytes=index_bytes,
        hard_limits=limits,
        hardware={
            "logical_cpu_count": os.cpu_count(),
            "machine": platform.machine() or "unknown",
            "os": platform.system() or "unknown",
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        replay_command=(
            "python -m benchmarks.rag_scale --chunks "
            f"{chunk_count} --seed {seed} --concurrency {concurrent_reads} "
            "--artifact benchmarks/artifacts/rag-scale.json"
        ),
    )
    if owned is not None:
        owned.cleanup()
    return report


def write_artifact(report: RagScaleReport, destination: str | Path) -> Path:
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=".rag-scale-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(asdict(report), stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return target


def _quality(index: DiskRagIndex, expected: int, query: str) -> QualityEvidence:
    hits = index.search(query)
    rank = next(
        (position for position, hit in enumerate(hits, 1) if hit.chunk_id == expected),
        None,
    )
    return QualityEvidence(query, expected, rank)


def _timed(call: Any) -> float:
    started = time.perf_counter_ns()
    call()
    return _elapsed_ms(started)


def _elapsed_ms(started: int) -> float:
    return (time.perf_counter_ns() - started) / 1_000_000


def _latency(samples: Iterable[float]) -> Latency:
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("latency samples must not be empty")
    return Latency(
        len(ordered),
        round(_percentile(ordered, 0.50), 6),
        round(_percentile(ordered, 0.95), 6),
        round(ordered[-1], 6),
    )


def _percentile(ordered: list[float], quantile: float) -> float:
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=int, default=CI_CHUNKS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_rag_scale_benchmark(
        chunk_count=args.chunks,
        seed=args.seed,
        concurrent_reads=args.concurrency,
    )
    write_artifact(report, args.artifact)
    print(json.dumps(asdict(report), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
