"""Fast release evidence for catalog search, cached load, and concurrent execution."""

from __future__ import annotations

import math
import os
import platform
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

from benchmarks.rag_scale import Latency
from benchmarks.scale import DEFAULT_SEED, MetadataSearchIndex, generate_metadata_catalog
from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    OperationSpec,
    OperationType,
    SectionDescriptor,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext

CATALOG_COUNT = 10_000
CONCURRENT_EXECUTIONS = 100
SEARCH_P95_LIMIT_MS = 150.0
LOAD_P95_LIMIT_MS = 75.0


@dataclass(frozen=True, slots=True)
class ReleaseGateReport:
    schema: str
    seed: int
    tenant_count: int
    catalog_count: int
    search: Latency
    cached_load: Latency
    concurrent_execution: Latency
    concurrent_execution_target: int
    successful_executions: int
    hard_limits: dict[str, float | int]
    hardware: dict[str, str | int | None]


class _ExecutionProvider:
    name = "release-provider"

    def __init__(self, manifest: CapabilityManifest) -> None:
        self._manifest = manifest

    def discover(self) -> tuple[CapabilityManifest, ...]:
        return (self._manifest,)

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        if identity != self._manifest.identity or request.operation != "read":
            raise RuntimeError("release fixture received an unexpected request")
        return ExecutionResult(
            identity.revision,
            request.operation,
            {"ok": True, "task_digest": request.task_id[-8:]},
            self.name,
            4,
            f"release-{request.task_id[-8:]}",
        )


def run_release_gate(*, seed: int = DEFAULT_SEED) -> ReleaseGateReport:
    catalog, fixtures = generate_metadata_catalog(count=CATALOG_COUNT, seed=seed)
    index = MetadataSearchIndex(catalog)
    queries = tuple(fixture.query for fixture in fixtures)
    search_samples = [
        _timed(lambda query=query: index.search(query, limit=8))
        for _ in range(10)
        for query in queries
    ]
    if any(
        fixture.expected_capability_id not in index.search(fixture.query, limit=8)
        for fixture in fixtures
    ):
        raise RuntimeError("10k catalog quality fixture failed")

    service, context = _service()
    prepared: list[tuple[ExecutionRequest, BudgetLedger]] = []
    load_samples: list[float] = []
    for position in range(CONCURRENT_EXECUTIONS):
        task = f"release-task-{position:03d}"
        budget = _budget(task)
        card = service.search(
            "offline release fixture",
            task_id=task,
            context=context,
            budget=budget,
            limit=1,
        ).cards[0]
        started = time.perf_counter_ns()
        loaded = service.load(
            card.capability_ref,
            task_id=task,
            context=context,
            budget=budget,
            section_names=("contract",),
            operation_names=("read",),
        )
        load_samples.append(_elapsed_ms(started))
        prepared.append(
            (ExecutionRequest(loaded.execution_ref, "read", {}, task), budget)
        )

    def execute(item: tuple[ExecutionRequest, BudgetLedger]) -> tuple[float, bool]:
        request, budget = item
        started = time.perf_counter_ns()
        result = service.execute(request, context=context, budget=budget)
        output = result.output
        return _elapsed_ms(started), isinstance(output, dict) and output.get("ok") is True

    with ThreadPoolExecutor(max_workers=CONCURRENT_EXECUTIONS) as executor:
        executions = tuple(executor.map(execute, prepared))

    search = _latency(search_samples)
    cached_load = _latency(load_samples)
    concurrent = _latency(item[0] for item in executions)
    successful = sum(item[1] for item in executions)
    if search.p95_ms >= SEARCH_P95_LIMIT_MS:
        raise RuntimeError("10k catalog search p95 exceeded the release limit")
    if cached_load.p95_ms >= LOAD_P95_LIMIT_MS:
        raise RuntimeError("cached load p95 exceeded the release limit")
    if successful != CONCURRENT_EXECUTIONS:
        raise RuntimeError("not every real concurrent execution succeeded")
    return ReleaseGateReport(
        schema="capabilityhub.release-gate-evidence.v1",
        seed=seed,
        tenant_count=1,
        catalog_count=len(catalog),
        search=search,
        cached_load=cached_load,
        concurrent_execution=concurrent,
        concurrent_execution_target=CONCURRENT_EXECUTIONS,
        successful_executions=successful,
        hard_limits={
            "search_p95_ms": SEARCH_P95_LIMIT_MS,
            "cached_load_p95_ms": LOAD_P95_LIMIT_MS,
            "concurrent_execution_target": CONCURRENT_EXECUTIONS,
        },
        hardware={
            "logical_cpu_count": os.cpu_count(),
            "machine": platform.machine() or "unknown",
            "os": platform.system() or "unknown",
            "python": platform.python_version(),
        },
    )


def report_json(report: ReleaseGateReport) -> dict[str, Any]:
    return asdict(report)


def _service() -> tuple[CapabilityHubService, ServiceContext]:
    manifest = CapabilityManifest(
        CapabilityIdentity("release", "offline", "1", "sha256:" + "a" * 64),
        CapabilityKind.API,
        "Offline release fixture.",
        "release-provider",
        (OperationSpec("read", OperationType.EXECUTE),),
        sections=(SectionDescriptor("contract", "text/plain", "offline contract", 3),),
    )
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    service = CapabilityHubService(
        registry=registry,
        providers=(_ExecutionProvider(manifest),),
        references=ReferenceSigner(b"release-gate-reference-key-material"),
        audit=MemoryAuditSink(),
    )
    return service, ServiceContext("release-tenant", "release-principal", "release-session")


def _budget(task: str) -> BudgetLedger:
    return BudgetLedger(
        task,
        {"bytes": 100_000, "executions": 1, "loads": 1, "portable_tokens": 10_000},
    )


def _timed(call: Any) -> float:
    started = time.perf_counter_ns()
    call()
    return _elapsed_ms(started)


def _elapsed_ms(started: int) -> float:
    return (time.perf_counter_ns() - started) / 1_000_000


def _latency(samples: Any) -> Latency:
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("latency samples must not be empty")
    return Latency(
        len(ordered),
        round(ordered[max(0, math.ceil(len(ordered) * 0.50) - 1)], 6),
        round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 6),
        round(ordered[-1], 6),
    )
