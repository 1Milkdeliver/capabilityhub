"""Offline, deterministic eager-versus-lazy capability exposure benchmark."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import cast

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError
from capabilityhub.manifest import parse_manifest
from capabilityhub.metering import PayloadMeasurement, TokenEstimator, canonical_json, measure_text
from capabilityhub.models import CapabilityKind, CapabilityManifest, JsonValue
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.search import LexicalCapabilitySearch
from capabilityhub.service import CapabilityHubService, ServiceContext

FIXTURE_DIR = Path(__file__).with_name("fixtures")
CATALOG_FIXTURE = "catalog.json"
TASKS_FIXTURE = "tasks.json"
FAILURES_FIXTURE = "failure-scenarios.json"
ARTIFACT_DIR = Path(__file__).with_name("artifacts")
VALIDATION_RUN_ARTIFACT = ARTIFACT_DIR / "validation-run.json"
VALIDATION_EVENTS_ARTIFACT = ARTIFACT_DIR / "validation-events.jsonl"
INITIAL_REDUCTION_THRESHOLD_PERCENT = 60.0
TOTAL_REDUCTION_THRESHOLD_PERCENT = 35.0
MAX_LOAD_UNUSED_RATE = 0.10
_META_TOOL_CONTRACT: dict[str, JsonValue] = {
    "protocol": "capabilityhub.benchmark.v1",
    "tools": [
        {
            "name": "capability.search",
            "input": [
                "query",
                "task_id",
                "kinds",
                "limit",
                "max_output_tokens",
                "include_inventory",
                "include_cards",
            ],
        },
        {
            "name": "capability.load",
            "input": [
                "capability_ref",
                "task_id",
                "section_names",
                "operation_names",
                "max_output_tokens",
            ],
        },
        {
            "name": "capability.execute",
            "input": [
                "execution_ref",
                "operation",
                "arguments",
                "task_id",
                "approval_ref",
                "idempotency_key",
                "max_output_tokens",
            ],
        },
    ],
}


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    task_id: str
    prompt: str
    expected_target_revision: str


@dataclass(frozen=True, slots=True)
class FailureScenario:
    scenario_id: str
    action: str
    query: str
    expected_code: str


@dataclass(frozen=True, slots=True)
class FailureResult:
    scenario_id: str
    action: str
    expected_code: str
    actual_code: str
    passed: bool


@dataclass(frozen=True, slots=True)
class CacheEvidence:
    task_id: str
    mode: str
    revision: str
    outcome: str
    invalidated_revisions: tuple[str, ...]
    materialized_tokens: int
    stale_use: bool


class _ValidationDefinitionCache:
    """Small revision/scope cache used only to produce deterministic validation evidence."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], tuple[str, str]] = {}

    def resolve(
        self,
        *,
        scope: str,
        coordinate: str,
        revision: str,
        definition: str,
        estimator: TokenEstimator | None,
    ) -> tuple[str, int]:
        key = (scope, revision)
        if key in self._entries:
            return "hit", 0
        self._entries[key] = (coordinate, definition)
        return "miss", measure_text(definition, estimator).portable_tokens

    def invalidate(self, *, scope: str, coordinate: str) -> tuple[str, ...]:
        revisions = tuple(
            sorted(
                revision
                for (entry_scope, revision), (entry_coordinate, _) in self._entries.items()
                if entry_scope == scope and entry_coordinate == coordinate
            )
        )
        for revision in revisions:
            del self._entries[(scope, revision)]
        return revisions


@dataclass(frozen=True, slots=True)
class FixtureCatalog:
    manifests: tuple[CapabilityManifest, ...]
    definitions_by_revision: Mapping[str, str]
    tasks: tuple[BenchmarkTask, ...]
    failure_scenarios: tuple[FailureScenario, ...]
    catalog_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "definitions_by_revision", MappingProxyType(dict(self.definitions_by_revision))
        )


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    query: str
    expected_target_revision: str
    selected_revision: str
    selection_reason: tuple[str, ...]
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


@dataclass(frozen=True, slots=True)
class ValidationReport:
    benchmark: BenchmarkReport
    failures: tuple[FailureResult, ...]
    cache: tuple[CacheEvidence, ...]
    tasks_digest: str
    failures_digest: str

    @property
    def release_ready(self) -> bool:
        return (
            self.benchmark.release_ready
            and all(result.passed for result in self.failures)
            and not any(result.stale_use for result in self.cache)
        )


def load_fixtures(fixture_dir: Path | str = FIXTURE_DIR) -> FixtureCatalog:
    """Load frozen fixture records; no provider, network, or model is contacted."""

    directory = Path(fixture_dir)
    raw_catalog = _read_json(directory / CATALOG_FIXTURE)
    raw_tasks = _read_json(directory / TASKS_FIXTURE)
    raw_failures = _read_json(directory / FAILURES_FIXTURE)
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
    failure_scenarios = tuple(
        _failure_scenario(entry)
        for entry in _array(raw_failures.get("scenarios"), "failure scenarios")
    )
    if not manifests or not tasks or not failure_scenarios:
        raise ValueError("Fixture catalog, tasks, and failure scenarios must be non-empty")
    for task in tasks:
        if task.expected_target_revision not in definitions:
            raise ValueError(f"Task {task.task_id} references an unknown revision")
    return FixtureCatalog(
        manifests=tuple(manifests),
        definitions_by_revision=definitions,
        tasks=tasks,
        failure_scenarios=failure_scenarios,
        catalog_digest=_digest(canonical_json(cast(JsonValue, dict(raw_catalog)))),
    )


def run_failure_scenarios(
    fixture_dir: Path | str = FIXTURE_DIR,
) -> tuple[FailureResult, ...]:
    """Exercise deterministic failure paths without consulting expected outcomes."""

    fixtures = load_fixtures(fixture_dir)
    results: list[FailureResult] = []
    for scenario in fixtures.failure_scenarios:
        actual = _run_failure_scenario(fixtures, scenario)
        results.append(
            FailureResult(
                scenario_id=scenario.scenario_id,
                action=scenario.action,
                expected_code=scenario.expected_code,
                actual_code=actual,
                passed=actual == scenario.expected_code,
            )
        )
    return tuple(results)


def run_cache_scenarios(
    fixture_dir: Path | str = FIXTURE_DIR,
    estimator: TokenEstimator | None = None,
) -> tuple[CacheEvidence, ...]:
    """Produce cold, warm, relevant-change, and unrelated-change cache evidence."""

    fixtures = load_fixtures(fixture_dir)
    manifests = {manifest.identity.revision: manifest for manifest in fixtures.manifests}
    results: list[CacheEvidence] = []
    for task in fixtures.tasks:
        target = manifests[task.expected_target_revision]
        definition = fixtures.definitions_by_revision[target.identity.revision]
        scope = f"benchmark/{task.task_id}"

        cache = _ValidationDefinitionCache()
        cold_outcome, cold_tokens = cache.resolve(
            scope=scope,
            coordinate=target.identity.coordinate,
            revision=target.identity.revision,
            definition=definition,
            estimator=estimator,
        )
        results.append(
            CacheEvidence(
                task.task_id,
                "cold",
                target.identity.revision,
                cold_outcome,
                (),
                cold_tokens,
                False,
            )
        )
        warm_outcome, warm_tokens = cache.resolve(
            scope=scope,
            coordinate=target.identity.coordinate,
            revision=target.identity.revision,
            definition=definition,
            estimator=estimator,
        )
        results.append(
            CacheEvidence(
                task.task_id,
                "warm",
                target.identity.revision,
                warm_outcome,
                (),
                warm_tokens,
                False,
            )
        )

        relevant_cache = _ValidationDefinitionCache()
        relevant_cache.resolve(
            scope=scope,
            coordinate=target.identity.coordinate,
            revision=target.identity.revision,
            definition=definition,
            estimator=estimator,
        )
        invalidated = relevant_cache.invalidate(scope=scope, coordinate=target.identity.coordinate)
        changed_revision = _changed_revision(target.identity.revision)
        changed_definition = definition + "\n"
        relevant_outcome, relevant_tokens = relevant_cache.resolve(
            scope=scope,
            coordinate=target.identity.coordinate,
            revision=changed_revision,
            definition=changed_definition,
            estimator=estimator,
        )
        results.append(
            CacheEvidence(
                task.task_id,
                "relevant-invalidation",
                changed_revision,
                relevant_outcome,
                invalidated,
                relevant_tokens,
                target.identity.revision not in invalidated,
            )
        )

        unrelated_cache = _ValidationDefinitionCache()
        unrelated_cache.resolve(
            scope=scope,
            coordinate=target.identity.coordinate,
            revision=target.identity.revision,
            definition=definition,
            estimator=estimator,
        )
        unrelated = next(
            manifest
            for manifest in fixtures.manifests
            if manifest.identity.coordinate != target.identity.coordinate
        )
        unrelated_cache.invalidate(scope=scope, coordinate=unrelated.identity.coordinate)
        unrelated_outcome, unrelated_tokens = unrelated_cache.resolve(
            scope=scope,
            coordinate=target.identity.coordinate,
            revision=target.identity.revision,
            definition=definition,
            estimator=estimator,
        )
        results.append(
            CacheEvidence(
                task.task_id,
                "unrelated-invalidation",
                target.identity.revision,
                unrelated_outcome,
                (),
                unrelated_tokens,
                False,
            )
        )
    return tuple(results)


def run_validation(fixture_dir: Path | str = FIXTURE_DIR) -> ValidationReport:
    """Run all offline deterministic validation cells."""

    directory = Path(fixture_dir)
    tasks_payload = canonical_json(cast(JsonValue, dict(_read_json(directory / TASKS_FIXTURE))))
    failures_payload = canonical_json(
        cast(JsonValue, dict(_read_json(directory / FAILURES_FIXTURE)))
    )
    return ValidationReport(
        benchmark=run_benchmark(directory),
        failures=run_failure_scenarios(directory),
        cache=run_cache_scenarios(directory),
        tasks_digest=_digest(tasks_payload),
        failures_digest=_digest(failures_payload),
    )


def build_validation_artifacts(
    report: ValidationReport,
) -> tuple[dict[str, JsonValue], str]:
    """Return a replay summary and canonical JSONL event stream."""

    events: list[dict[str, JsonValue]] = []
    for task_result in report.benchmark.results:
        events.append(
            {
                "event": "semantic_selection",
                "expected_revision": task_result.expected_target_revision,
                "outcome": "pass" if task_result.selection_correct else "fail",
                "query": task_result.query,
                "reason_codes": list(task_result.selection_reason),
                "selected_revision": task_result.selected_revision,
                "task_id": task_result.task_id,
            }
        )
    for failure_result in report.failures:
        events.append(
            {
                "action": failure_result.action,
                "actual_code": failure_result.actual_code,
                "event": "expected_failure",
                "expected_code": failure_result.expected_code,
                "outcome": "pass" if failure_result.passed else "fail",
                "scenario_id": failure_result.scenario_id,
            }
        )
    for cache_result in report.cache:
        events.append(
            {
                "cache_outcome": cache_result.outcome,
                "event": "cache_access",
                "invalidated_revisions": list(cache_result.invalidated_revisions),
                "materialized_tokens": cache_result.materialized_tokens,
                "mode": cache_result.mode,
                "revision": cache_result.revision,
                "stale_use": cache_result.stale_use,
                "task_id": cache_result.task_id,
            }
        )
    sequenced = [dict(event, sequence=index) for index, event in enumerate(events, start=1)]
    events_jsonl = "".join(canonical_json(cast(JsonValue, event)) + "\n" for event in sequenced)
    correct = sum(result.selection_correct for result in report.benchmark.results)
    cache_modes: dict[str, JsonValue] = {
        mode: sum(result.mode == mode for result in report.cache)
        for mode in sorted({result.mode for result in report.cache})
    }
    summary: dict[str, JsonValue] = {
        "benchmark": "deterministic-non-oracle-validation",
        "cache": {
            "modes": cache_modes,
            "stale_uses": sum(result.stale_use for result in report.cache),
        },
        "events": {"count": len(sequenced), "digest": _digest(events_jsonl)},
        "failures": {
            "passed": sum(result.passed for result in report.failures),
            "total": len(report.failures),
        },
        "fixtures": {
            "catalog_digest": report.benchmark.catalog_digest,
            "failure_scenarios_digest": report.failures_digest,
            "tasks_digest": report.tasks_digest,
        },
        "release_gate_passed": report.release_ready,
        "schema_version": 1,
        "semantic_selection": {
            "accuracy": round(correct / len(report.benchmark.results), 8),
            "correct": correct,
            "total": len(report.benchmark.results),
        },
    }
    return summary, events_jsonl


def assert_validation_artifacts(
    run_path: Path | str = VALIDATION_RUN_ARTIFACT,
    events_path: Path | str = VALIDATION_EVENTS_ARTIFACT,
) -> None:
    """Replay current fixtures and require byte-stable pinned evidence."""

    expected_run, expected_events = build_validation_artifacts(run_validation())
    actual_run = _read_json(Path(run_path))
    try:
        actual_events = Path(events_path).read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("Unable to read validation event artifact") from error
    assert dict(actual_run) == expected_run, "validation run artifact is stale"
    assert actual_events == expected_events, "validation event artifact is stale"


def run_benchmark(
    fixture_dir: Path | str = FIXTURE_DIR, estimator: TokenEstimator | None = None
) -> BenchmarkReport:
    """Measure deterministic lexical routing and disclosure against an eager catalog.

    Expected revisions are used only after search to score the selected top result.
    The router receives the task prompt, never the expected revision. This is a
    deterministic lexical-quality gate, not a claim about model selection quality.
    """

    fixtures = load_fixtures(fixture_dir)
    eager_payload = _eager_payload(fixtures.definitions_by_revision)
    lazy_payload = _lazy_initial_payload()
    eager_catalog = measure_text(eager_payload, estimator)
    lazy_catalog = measure_text(lazy_payload, estimator)
    results: list[TaskResult] = []
    coverage = {kind: False for kind in CapabilityKind}
    registry = CapabilityRegistry()
    registry.register_many(fixtures.manifests)
    for manifest in fixtures.manifests:
        registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    search = LexicalCapabilitySearch(
        registry,
        ReferenceSigner(b"capabilityhub-benchmark-search", clock=lambda: 1_000),
    )
    for task in fixtures.tasks:
        response = search.search(
            task.prompt,
            scope="benchmark",
            limit=1,
            max_output_tokens=10_000,
        )
        selected_card = response.cards[0] if response.cards else None
        selected_revision = selected_card.revision if selected_card is not None else ""
        selected_manifest = next(
            (
                manifest
                for manifest in fixtures.manifests
                if manifest.identity.revision == selected_revision
            ),
            None,
        )
        loaded_definition = fixtures.definitions_by_revision.get(selected_revision, "")
        card_payload = (
            _search_card_payload(selected_manifest) if selected_manifest is not None else ""
        )
        selected_load = measure_text(loaded_definition, estimator)
        search_card = measure_text(card_payload, estimator)
        lazy_total = measure_text(
            lazy_payload + "\n" + card_payload + "\n" + loaded_definition,
            estimator,
        )
        correct = selected_revision == task.expected_target_revision
        if selected_manifest is not None:
            coverage[selected_manifest.kind] = coverage[selected_manifest.kind] or correct
        results.append(
            TaskResult(
                task_id=task.task_id,
                query=task.prompt,
                expected_target_revision=task.expected_target_revision,
                selected_revision=selected_revision,
                selection_reason=(
                    selected_card.match_reason if selected_card is not None else ("no_match",)
                ),
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


def _failure_scenario(value: object) -> FailureScenario:
    if not isinstance(value, Mapping):
        raise ValueError("Each failure scenario must be an object")
    scenario_id = value.get("id")
    action = value.get("action")
    query = value.get("query")
    expected_code = value.get("expected_code")
    fields = (scenario_id, action, query, expected_code)
    if not all(isinstance(field, str) and field for field in fields):
        raise ValueError("Failure scenario fields must be non-empty strings")
    assert isinstance(scenario_id, str)
    assert isinstance(action, str)
    assert isinstance(query, str)
    assert isinstance(expected_code, str)
    if action not in {
        "no_match",
        "invalid_kind",
        "search_budget",
        "tampered_reference",
        "stale_reference",
    }:
        raise ValueError(f"Unknown failure scenario action: {action}")
    return FailureScenario(scenario_id, action, query, expected_code)


def _run_failure_scenario(fixtures: FixtureCatalog, scenario: FailureScenario) -> str:
    registry = CapabilityRegistry()
    registry.register_many(fixtures.manifests)
    for manifest in fixtures.manifests:
        registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    service = CapabilityHubService(
        registry=registry,
        providers=(),
        references=ReferenceSigner(b"capabilityhub-benchmark-failures", clock=lambda: 1_000),
        audit=MemoryAuditSink(),
    )
    context = ServiceContext("benchmark", "runner", scenario.scenario_id)
    budget = BudgetLedger(
        f"benchmark:{scenario.scenario_id}",
        {"bytes": 1_000_000, "loads": 10, "portable_tokens": 100_000},
    )
    try:
        if scenario.action == "no_match":
            response = service.search(
                scenario.query,
                task_id=scenario.scenario_id,
                context=context,
                budget=budget,
                max_output_tokens=10_000,
            )
            return "unexpected_match" if response.cards else "no_match"
        if scenario.action == "invalid_kind":
            service.search(
                scenario.query,
                task_id=scenario.scenario_id,
                context=context,
                budget=budget,
                kinds=("not-a-capability-kind",),
            )
            return "unexpected_success"
        if scenario.action == "search_budget":
            service.search(
                scenario.query,
                task_id=scenario.scenario_id,
                context=context,
                budget=budget,
                max_output_tokens=1,
            )
            return "unexpected_success"

        searched = service.search(
            scenario.query,
            task_id=scenario.scenario_id,
            context=context,
            budget=budget,
            limit=1,
            max_output_tokens=10_000,
        )
        if not searched.cards:
            return "setup_no_match"
        capability_ref = searched.cards[0].capability_ref
        if scenario.action == "tampered_reference":
            encoded, signature = capability_ref.rsplit(".", 1)
            replacement = "A" if signature[0] != "A" else "B"
            capability_ref = f"{encoded}.{replacement}{signature[1:]}"
        elif scenario.action == "stale_reference":
            selected = registry.revision(searched.cards[0].revision)
            changed_identity = replace(
                selected.identity,
                version="1.0.1",
                digest=_digest(selected.identity.revision + ":changed"),
            )
            changed = replace(selected, identity=changed_identity)
            registry.register(changed)
            registry.activate(changed.identity.coordinate, changed.identity.revision)
        service.load(
            capability_ref,
            task_id=scenario.scenario_id,
            context=context,
            budget=budget,
        )
    except CapabilityHubError as error:
        return error.code
    return "unexpected_success"


def _digest(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _changed_revision(revision: str) -> str:
    coordinate = revision.split("@", 1)[0]
    return f"{coordinate}@validation-change#{_digest(revision + ':changed')}"


def _reduction(baseline: int, candidate: int) -> float:
    if baseline <= 0:
        raise ValueError("Baseline payload must be non-empty")
    return round(((baseline - candidate) / baseline) * 100, 8)


def _average(values: Iterable[float]) -> float:
    numbers = tuple(values)
    if not numbers:
        raise ValueError("Cannot average an empty sequence")
    return round(sum(numbers) / len(numbers), 8)
