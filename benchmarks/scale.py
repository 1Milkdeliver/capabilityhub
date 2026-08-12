"""Replayable 10k metadata search and concurrent read latency evidence.

This is a synthetic, in-process metadata benchmark.  It is not evidence for a
one-million-document RAG corpus, model quality, or production provider latency.
"""

from __future__ import annotations

import hashlib
import math
import os
import platform
import random
import re
import time
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier
from types import MappingProxyType

from capabilityhub.metering import canonical_json
from capabilityhub.models import JsonValue
from capabilityhub.search import SearchRankingConfig

DEFAULT_SEED = 20_260_812
DEFAULT_CAPABILITY_COUNT = 10_000
DEFAULT_CONCURRENT_READS = 100
TOP_K = 8
SEARCH_P95_LIMIT_MS = 1_000.0
_WORD_RE = re.compile(r"[a-z0-9]+")
_SCOPE_LIMITS = (
    "10k synthetic metadata capabilities only",
    "in-process read-only lexical search only",
    "not a 1m-document RAG claim",
    "not a model-quality claim",
    "not a production-provider claim",
)
_QUALITY_SPECS = (
    (37, "finance reconciliation ledger"),
    (1037, "satellite weather telemetry"),
    (2037, "medical archive retrieval"),
    (3037, "warehouse inventory robotics"),
    (4037, "legal contract redaction"),
    (5037, "audio transcript diarization"),
    (6037, "security credential rotation"),
    (7037, "travel itinerary localization"),
)


@dataclass(frozen=True, slots=True)
class MetadataCapability:
    capability_id: str
    title: str
    summary: str
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QualityFixture:
    query: str
    expected_capability_id: str


@dataclass(frozen=True, slots=True)
class LatencyStats:
    samples: int
    p50_ms: float
    p95_ms: float
    max_ms: float


@dataclass(frozen=True, slots=True)
class QualityEvidence:
    query: str
    expected_capability_id: str
    rank: int | None
    top8_hit: bool
    top3_hit: bool


@dataclass(frozen=True, slots=True)
class ScaleBenchmarkReport:
    seed: int
    capability_count: int
    top_k: int
    concurrent_read_target: int
    catalog_build_ms: float
    cold: LatencyStats
    warm: LatencyStats
    concurrent: LatencyStats
    concurrent_quality_hits: int
    quality: tuple[QualityEvidence, ...]
    dataset_digest: str
    hardware: Mapping[str, JsonValue]
    scope_limits: tuple[str, ...]
    ranking_revision: str
    ranking_digest: str
    index_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "hardware", MappingProxyType(dict(self.hardware)))


class MetadataSearchIndex:
    """Immutable postings index safe for concurrent reads after construction."""

    def __init__(
        self,
        capabilities: Iterable[MetadataCapability],
        *,
        ranking: SearchRankingConfig | None = None,
    ) -> None:
        records = tuple(capabilities)
        self._ranking = ranking or SearchRankingConfig()
        by_id = {record.capability_id: record for record in records}
        if len(by_id) != len(records):
            raise ValueError("capability ids must be unique")
        postings: dict[str, set[str]] = {}
        tokens_by_id: dict[
            str, tuple[frozenset[str], frozenset[str], frozenset[str]]
        ] = {}
        for record in records:
            title = frozenset(_tokens(record.title))
            summary = frozenset(_tokens(record.summary))
            keywords = frozenset(
                token for value in record.keywords for token in _tokens(value)
            )
            tokens = title | summary | keywords
            tokens_by_id[record.capability_id] = (title, summary, keywords)
            for token in tokens:
                postings.setdefault(token, set()).add(record.capability_id)
        self._records = MappingProxyType(by_id)
        self._tokens = MappingProxyType(tokens_by_id)
        self._postings = MappingProxyType(
            {token: frozenset(ids) for token, ids in postings.items()}
        )
        material = f"{_catalog_digest(records)}\0{self._ranking.digest}".encode()
        self.index_revision = "sha256:" + hashlib.sha256(material).hexdigest()

    @property
    def ranking_revision(self) -> str:
        return self._ranking.revision

    @property
    def ranking_digest(self) -> str:
        return self._ranking.digest

    def search(self, query: str, *, limit: int = TOP_K) -> tuple[str, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        query_tokens = tuple(dict.fromkeys(_tokens(query)))
        candidates: set[str] = set()
        for token in query_tokens:
            candidates.update(self._postings.get(token, ()))
        ranked = sorted(
            candidates,
            key=lambda capability_id: (
                -self._score(capability_id, frozenset(query_tokens)),
                capability_id,
            ),
        )
        return tuple(ranked[:limit])

    def _score(self, capability_id: str, query: frozenset[str]) -> int:
        title, summary, keywords = self._tokens[capability_id]
        score = (
            len(query & title) * self._ranking.weights["name"]
            + len(query & summary) * self._ranking.weights["summary"]
            + len(query & keywords) * self._ranking.weights["alias"]
        )
        if title == query:
            score += self._ranking.weights["exact_name"]
        return score


def generate_metadata_catalog(
    *, count: int = DEFAULT_CAPABILITY_COUNT, seed: int = DEFAULT_SEED
) -> tuple[tuple[MetadataCapability, ...], tuple[QualityFixture, ...]]:
    if count < max(index for index, _ in _QUALITY_SPECS) + 1:
        raise ValueError("count is too small for the fixed quality fixtures")
    rng = random.Random(seed)
    vocabulary = tuple(f"topic{index:03d}" for index in range(256))
    quality_by_index = dict(_QUALITY_SPECS)
    records: list[MetadataCapability] = []
    fixtures: list[QualityFixture] = []
    for index in range(count):
        capability_id = f"synthetic/capability-{index:05d}"
        terms = tuple(rng.sample(vocabulary, 5))
        quality = quality_by_index.get(index)
        if quality is not None:
            quality_terms = tuple(quality.split())
            title = f"{quality} service"
            keywords = (*terms, *quality_terms)
            fixtures.append(QualityFixture(quality, capability_id))
        else:
            title = f"Synthetic capability {index:05d}"
            keywords = terms
        records.append(
            MetadataCapability(
                capability_id=capability_id,
                title=title,
                summary=f"Read-only metadata for {terms[0]} {terms[1]}",
                keywords=keywords,
            )
        )
    return tuple(records), tuple(fixtures)


def run_scale_benchmark(
    *,
    capability_count: int = DEFAULT_CAPABILITY_COUNT,
    concurrent_reads: int = DEFAULT_CONCURRENT_READS,
    seed: int = DEFAULT_SEED,
    warm_repetitions: int = 8,
) -> ScaleBenchmarkReport:
    """Measure cold, warm, and >=100 concurrent metadata read targets."""

    if concurrent_reads < 100:
        raise ValueError("concurrent_reads must be at least 100")
    if warm_repetitions <= 0:
        raise ValueError("warm_repetitions must be positive")
    catalog, fixtures = generate_metadata_catalog(count=capability_count, seed=seed)

    started = time.perf_counter_ns()
    index = MetadataSearchIndex(catalog)
    catalog_build_ms = _elapsed_ms(started)

    cold_samples: list[float] = []
    for fixture in fixtures:
        started = time.perf_counter_ns()
        cold_index = MetadataSearchIndex(catalog)
        cold_index.search(fixture.query, limit=TOP_K)
        cold_samples.append(_elapsed_ms(started))

    # Prime the shared immutable index, then measure repeated warm reads.
    for fixture in fixtures:
        index.search(fixture.query, limit=TOP_K)
    warm_samples = [
        _timed_search(index, fixture.query) for _ in range(warm_repetitions) for fixture in fixtures
    ]

    barrier = Barrier(concurrent_reads + 1)

    def concurrent_read(read_index: int) -> tuple[float, bool]:
        fixture = fixtures[read_index % len(fixtures)]
        barrier.wait()
        started_at = time.perf_counter_ns()
        results = index.search(fixture.query, limit=TOP_K)
        return _elapsed_ms(started_at), fixture.expected_capability_id in results

    with ThreadPoolExecutor(max_workers=concurrent_reads) as executor:
        futures = [executor.submit(concurrent_read, index) for index in range(concurrent_reads)]
        barrier.wait()
        concurrent_results = [future.result() for future in futures]

    quality = tuple(_quality_evidence(index, fixture) for fixture in fixtures)
    cold = _latency(cold_samples)
    warm = _latency(warm_samples)
    concurrent = _latency(item[0] for item in concurrent_results)
    if warm.p95_ms > SEARCH_P95_LIMIT_MS or concurrent.p95_ms > SEARCH_P95_LIMIT_MS:
        raise RuntimeError("10k metadata search p95 exceeded the hard limit")
    if any(not evidence.top3_hit for evidence in quality):
        raise RuntimeError("10k metadata quality fixture missed the correct top three")
    return ScaleBenchmarkReport(
        seed=seed,
        capability_count=len(catalog),
        top_k=TOP_K,
        concurrent_read_target=concurrent_reads,
        catalog_build_ms=round(catalog_build_ms, 6),
        cold=cold,
        warm=warm,
        concurrent=concurrent,
        concurrent_quality_hits=sum(item[1] for item in concurrent_results),
        quality=quality,
        dataset_digest=_catalog_digest(catalog),
        hardware=_hardware_metadata(),
        scope_limits=_SCOPE_LIMITS,
        ranking_revision=index.ranking_revision,
        ranking_digest=index.ranking_digest,
        index_revision=index.index_revision,
    )


def _quality_evidence(index: MetadataSearchIndex, fixture: QualityFixture) -> QualityEvidence:
    results = index.search(fixture.query, limit=TOP_K)
    try:
        rank = results.index(fixture.expected_capability_id) + 1
    except ValueError:
        rank = None
    return QualityEvidence(
        query=fixture.query,
        expected_capability_id=fixture.expected_capability_id,
        rank=rank,
        top8_hit=rank is not None and rank <= TOP_K,
        top3_hit=rank is not None and rank <= 3,
    )


def _timed_search(index: MetadataSearchIndex, query: str) -> float:
    started = time.perf_counter_ns()
    index.search(query, limit=TOP_K)
    return _elapsed_ms(started)


def _elapsed_ms(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1_000_000


def _latency(samples: Iterable[float]) -> LatencyStats:
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("latency samples must not be empty")
    return LatencyStats(
        samples=len(ordered),
        p50_ms=round(_percentile(ordered, 0.50), 6),
        p95_ms=round(_percentile(ordered, 0.95), 6),
        max_ms=round(ordered[-1], 6),
    )


def _percentile(ordered: list[float], quantile: float) -> float:
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def _catalog_digest(catalog: tuple[MetadataCapability, ...]) -> str:
    payload: JsonValue = [
        {
            "capability_id": item.capability_id,
            "keywords": list(item.keywords),
            "summary": item.summary,
            "title": item.title,
        }
        for item in catalog
    ]
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _hardware_metadata() -> Mapping[str, JsonValue]:
    return {
        "logical_cpu_count": os.cpu_count(),
        "machine": platform.machine() or "unknown",
        "os": platform.system() or "unknown",
        "os_release": platform.release() or "unknown",
        "processor": platform.processor() or "unknown",
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
    }


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall(text.casefold()))
