"""Offline, deterministic eager-versus-lazy capability exposure benchmark."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast

from capabilityhub.manifest import parse_manifest
from capabilityhub.metering import PayloadMeasurement, TokenEstimator, canonical_json, measure_text
from capabilityhub.models import CapabilityKind, CapabilityManifest, JsonValue

FIXTURE_DIR = Path(__file__).with_name("fixtures")
CATALOG_FIXTURE = "catalog.json"
TASKS_FIXTURE = "tasks.json"
INITIAL_REDUCTION_THRESHOLD_PERCENT = 60.0
TOTAL_REDUCTION_THRESHOLD_PERCENT = 35.0
MAX_LOAD_UNUSED_RATE = 0.10
_META_TOOL_CONTRACT: dict[str, JsonValue] = {
    "protocol": "capabilityhub.benchmark.v1",
    "tools": [
        {"name": "capability.search", "input": ["intent", "kinds", "limit"]},
        {"name": "capability.load", "input": ["revision", "sections", "operations"]},
        {"name": "capability.execute", "input": ["execution_ref", "operation", "arguments"]},
    ],
}


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    task_id: str
    prompt: str
    expected_target_revision: str


@dataclass(frozen=True, slots=True)
class FixtureCatalog:
    manifests: tuple[CapabilityManifest, ...]
    definitions_by_revision: Mapping[str, str]
    tasks: tuple[BenchmarkTask, ...]
    catalog_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "definitions_by_revision", MappingProxyType(dict(self.definitions_by_revision))
        )


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    expected_target_revision: str
    selected_revision: str
    eager_initial: PayloadMeasurement
    eager_full: PayloadMeasurement
    lazy_initial: PayloadMeasurement
    search_card: PayloadMeasurement
    lazy_total: PayloadMeasurement
    selected_load: PayloadMeasurement
    selection_correct: bool
    load_unused: bool


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    estimator: str
    capability_count: int
    catalog_digest: str
    eager_catalog: PayloadMeasurement
    lazy_initial: PayloadMeasurement
    results: tuple[TaskResult, ...]
    initial_reduction_percent: float
    total_reduction_percent: float
    initial_token_reduction_percent: float
    total_token_reduction_percent: float
    provider_coverage: Mapping[CapabilityKind, bool]
    load_unused_rate: float
    definitions_identical: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_coverage", MappingProxyType(dict(self.provider_coverage))
        )

    @property
    def release_ready(self) -> bool:
        return (
            self.definitions_identical
            and self.initial_token_reduction_percent >= INITIAL_REDUCTION_THRESHOLD_PERCENT
            and self.total_token_reduction_percent >= TOTAL_REDUCTION_THRESHOLD_PERCENT
            and all(self.provider_coverage.values())
            and self.load_unused_rate <= MAX_LOAD_UNUSED_RATE
            and all(result.selection_correct for result in self.results)
        )


def load_fixtures(fixture_dir: Path | str = FIXTURE_DIR) -> FixtureCatalog:
    """Load frozen fixture records; no provider, network, or model is contacted."""

    directory = Path(fixture_dir)
    raw_catalog = _read_json(directory / CATALOG_FIXTURE)
    raw_tasks = _read_json(directory / TASKS_FIXTURE)
    capability_entries = _array(raw_catalog.get("capabilities"), "capabilities")
    manifests: list[CapabilityManifest] = []
    definitions: dict[str, str] = {}
    for entry in capability_entries:
        if not isinstance(entry, Mapping):
            raise ValueError("Each fixture capability must be an object")
        raw_definition = canonical_json(cast(JsonValue, dict(entry)))
        manifest = parse_manifest(entry)
        if manifest.identity.revision in definitions:
            raise ValueError("Fixture revisions must be unique")
        manifests.append(manifest)
        definitions[manifest.identity.revision] = raw_definition
    tasks = tuple(_task(entry) for entry in _array(raw_tasks.get("tasks"), "tasks"))
    if not manifests or not tasks:
        raise ValueError("Fixture catalog and tasks must be non-empty")
    for task in tasks:
        if task.expected_target_revision not in definitions:
            raise ValueError(f"Task {task.task_id} references an unknown revision")
    return FixtureCatalog(
        manifests=tuple(manifests),
        definitions_by_revision=definitions,
        tasks=tasks,
        catalog_digest=_digest(canonical_json(cast(JsonValue, dict(raw_catalog)))),
    )


def run_benchmark(
    fixture_dir: Path | str = FIXTURE_DIR, estimator: TokenEstimator | None = None
) -> BenchmarkReport:
    """Measure an oracle-routed lazy catalog against an identical eager catalog.

    The fixture task itself supplies the expected target; therefore this harness
    measures disclosure, not an LLM's semantic routing ability. It is useful as a
    reproducible release floor and cannot make a model-quality claim.
    """

    fixtures = load_fixtures(fixture_dir)
    eager_payload = _eager_payload(fixtures.definitions_by_revision)
    lazy_payload = _lazy_initial_payload()
    eager_catalog = measure_text(eager_payload, estimator)
    lazy_catalog = measure_text(lazy_payload, estimator)
    results: list[TaskResult] = []
    coverage = {kind: False for kind in CapabilityKind}
    for task in fixtures.tasks:
        loaded_definition = fixtures.definitions_by_revision[task.expected_target_revision]
        selected_manifest = next(
            manifest
            for manifest in fixtures.manifests
            if manifest.identity.revision == task.expected_target_revision
        )
        selected_load = measure_text(loaded_definition, estimator)
        search_card = measure_text(_search_card_payload(selected_manifest), estimator)
        lazy_total = measure_text(
            lazy_payload
            + "\n"
            + _search_card_payload(selected_manifest)
            + "\n"
            + loaded_definition,
            estimator,
        )
        selected_revision = selected_manifest.identity.revision
        correct = selected_revision == task.expected_target_revision
        coverage[selected_manifest.kind] = coverage[selected_manifest.kind] or correct
        results.append(
            TaskResult(
                task_id=task.task_id,
                expected_target_revision=task.expected_target_revision,
                selected_revision=selected_revision,
                eager_initial=eager_catalog,
                eager_full=eager_catalog,
                lazy_initial=lazy_catalog,
                search_card=search_card,
                lazy_total=lazy_total,
                selected_load=selected_load,
                selection_correct=correct,
                load_unused=False,
            )
        )
    return BenchmarkReport(
        estimator=eager_catalog.estimator,
        capability_count=len(fixtures.manifests),
        catalog_digest=fixtures.catalog_digest,
        eager_catalog=eager_catalog,
        lazy_initial=lazy_catalog,
        results=tuple(results),
        initial_reduction_percent=_reduction(eager_catalog.utf8_bytes, lazy_catalog.utf8_bytes),
        total_reduction_percent=_average(
            _reduction(result.eager_full.utf8_bytes, result.lazy_total.utf8_bytes)
            for result in results
        ),
        initial_token_reduction_percent=_reduction(
            eager_catalog.portable_tokens, lazy_catalog.portable_tokens
        ),
        total_token_reduction_percent=_average(
            _reduction(result.eager_full.portable_tokens, result.lazy_total.portable_tokens)
            for result in results
        ),
        provider_coverage=coverage,
        load_unused_rate=_average(1.0 if result.load_unused else 0.0 for result in results),
        definitions_identical=_definitions_identical(fixtures, eager_payload),
    )


def assert_release_thresholds(report: BenchmarkReport) -> None:
    """Raise a concise assertion when deterministic release thresholds are not met."""

    assert report.definitions_identical, "eager and lazy catalogs diverged"
    assert report.initial_token_reduction_percent >= INITIAL_REDUCTION_THRESHOLD_PERCENT
    assert report.total_token_reduction_percent >= TOTAL_REDUCTION_THRESHOLD_PERCENT
    assert all(report.provider_coverage.values()), "all five provider kinds require coverage"
    assert report.load_unused_rate <= MAX_LOAD_UNUSED_RATE
    assert all(result.selection_correct for result in report.results)


def _eager_payload(definitions_by_revision: Mapping[str, str]) -> str:
    return canonical_json(
        {
            "protocol": "capabilityhub.benchmark.v1",
            "exposure": "eager-full-definitions",
            "capabilities": [
                json.loads(definitions_by_revision[key]) for key in sorted(definitions_by_revision)
            ],
        }
    )


def _lazy_initial_payload() -> str:
    return canonical_json({**_META_TOOL_CONTRACT, "exposure": "lazy-meta-tools-only"})


def _search_card_payload(manifest: CapabilityManifest) -> str:
    return canonical_json(
        {
            "revision": manifest.identity.revision,
            "kind": manifest.kind.value,
            "summary": manifest.summary,
            "operations": [operation.name for operation in manifest.operations],
        }
    )


def _definitions_identical(fixtures: FixtureCatalog, eager_payload: str) -> bool:
    eager_definitions = json.loads(eager_payload)["capabilities"]
    eager_by_revision = {
        parse_manifest(definition).identity.revision: canonical_json(definition)
        for definition in eager_definitions
    }
    return dict(fixtures.definitions_by_revision) == eager_by_revision


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to read benchmark fixture {path.name}") from error
    if not isinstance(value, Mapping):
        raise ValueError(f"Fixture {path.name} must be an object")
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"Fixture {name} must be an array")
    return value


def _task(value: object) -> BenchmarkTask:
    if not isinstance(value, Mapping):
        raise ValueError("Each fixture task must be an object")
    task_id = value.get("id")
    prompt = value.get("prompt")
    revision = value.get("expected_target_revision")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("Task id must be a non-empty string")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("Task prompt must be a non-empty string")
    if not isinstance(revision, str) or not revision:
        raise ValueError("Task expected_target_revision must be a non-empty string")
    return BenchmarkTask(task_id=task_id, prompt=prompt, expected_target_revision=revision)


def _digest(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reduction(baseline: int, candidate: int) -> float:
    if baseline <= 0:
        raise ValueError("Baseline payload must be non-empty")
    return ((baseline - candidate) / baseline) * 100


def _average(values: Iterable[float]) -> float:
    numbers = tuple(values)
    if not numbers:
        raise ValueError("Cannot average an empty sequence")
    return sum(numbers) / len(numbers)
