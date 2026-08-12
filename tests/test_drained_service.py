from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event, Lock

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.drained_service import (
    DrainedCapabilityHubService,
    SignedExecutionBindingResolver,
)
from capabilityhub.draining import DrainController, DrainOutcome, LifecycleError, LifecycleState
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    OperationSpec,
    OperationType,
    SideEffect,
)
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext

IDENTITY = CapabilityIdentity("test", "blocking", "1", "sha256:" + "b" * 64)
CONTEXT = ServiceContext("tenant", "principal", "session")


class _BlockingProvider:
    name = "blocking-provider"

    def __init__(self) -> None:
        self.started = Event()
        self.finish = Event()
        self._calls = 0
        self._lock = Lock()

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def discover(self):
        return ()

    def execute(self, identity, request, context):
        del context
        with self._lock:
            self._calls += 1
        self.started.set()
        if not self.finish.wait(5):
            raise RuntimeError("test provider was not released")
        return ExecutionResult(
            identity.revision,
            request.operation,
            {"ok": True},
            self.name,
            2,
            "provider-audit",
        )


@dataclass
class _Fixture:
    wrapper: DrainedCapabilityHubService
    service: CapabilityHubService
    resolver: SignedExecutionBindingResolver
    drain: DrainController
    provider: _BlockingProvider
    audit: MemoryAuditSink
    loaded_ref: str
    budget: BudgetLedger


def _fixture(
    *,
    cancellable: bool = False,
    require_argument: bool = False,
    cancel: Callable[[str], bool] | None = None,
) -> _Fixture:
    input_schema = (
        {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "integer"}},
        }
        if require_argument
        else {}
    )
    operation = OperationSpec(
        "run",
        OperationType.EXECUTE,
        input_schema=input_schema,
        side_effect=SideEffect.READ,
    )
    manifest = CapabilityManifest(
        identity=IDENTITY,
        kind=CapabilityKind.API,
        summary="Blocking provider integration fixture",
        provider="blocking-provider",
        operations=(operation,),
    )
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(IDENTITY.coordinate, IDENTITY.revision)
    references = ReferenceSigner(b"drained-service-reference-test-key")
    provider = _BlockingProvider()
    audit = MemoryAuditSink()
    service = CapabilityHubService(
        registry=registry,
        providers=(provider,),
        references=references,
        audit=audit,
    )
    drain = DrainController()
    drain.register(IDENTITY.coordinate, IDENTITY.revision)
    resolver = SignedExecutionBindingResolver(
        references=references,
        registry=registry,
        cancellable=lambda _manifest, selected: cancellable and selected is operation,
    )
    wrapper = DrainedCapabilityHubService(
        service,
        drain=drain,
        resolver=resolver,
        cancel=cancel,
    )
    budget = BudgetLedger(
        "task",
        {"bytes": 100_000, "executions": 10, "loads": 10, "portable_tokens": 100_000},
    )
    search = wrapper.search("blocking", task_id="task", context=CONTEXT, budget=budget)
    loaded = wrapper.load(
        search.cards[0].capability_ref,
        task_id="task",
        context=CONTEXT,
        budget=budget,
        operation_names=("run",),
    )
    return _Fixture(
        wrapper,
        service,
        resolver,
        drain,
        provider,
        audit,
        loaded.execution_ref,
        budget,
    )


def test_search_and_load_are_transparent_delegations() -> None:
    fixture = _fixture()

    assert fixture.loaded_ref.startswith("chref1.")
    assert [event.event_type for event in fixture.audit.events] == ["search", "load"]
    assert fixture.drain.snapshot(IDENTITY.coordinate, IDENTITY.revision).in_flight == 0


def test_draining_rejection_occurs_before_provider_side_effect() -> None:
    fixture = _fixture()
    fixture.drain.begin_drain(IDENTITY.coordinate, IDENTITY.revision)

    with pytest.raises(LifecycleError) as caught:
        fixture.wrapper.execute(
            ExecutionRequest(fixture.loaded_ref, "run", {}, "task"),
            context=CONTEXT,
            budget=fixture.budget,
        )

    assert caught.value.code == "lifecycle_not_accepting"
    assert fixture.provider.calls == 0
    assert [event.event_type for event in fixture.audit.events] == ["search", "load"]


def test_concurrent_drain_blocks_new_execution_and_retires_after_old_completion() -> None:
    fixture = _fixture()
    first_request = ExecutionRequest(fixture.loaded_ref, "run", {}, "task")

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(
            fixture.wrapper.execute,
            first_request,
            context=CONTEXT,
            budget=fixture.budget,
        )
        assert fixture.provider.started.wait(2)
        draining = fixture.drain.begin_drain(IDENTITY.coordinate, IDENTITY.revision)
        with pytest.raises(LifecycleError) as rejected:
            fixture.wrapper.execute(
                ExecutionRequest(fixture.loaded_ref, "run", {}, "task"),
                context=CONTEXT,
                budget=fixture.budget,
            )
        assert rejected.value.code == "lifecycle_not_accepting"
        assert draining.in_flight == 1
        assert fixture.provider.calls == 1
        fixture.provider.finish.set()
        result = running.result(timeout=2)

    assert result.output == {"ok": True}
    assert fixture.drain.snapshot(IDENTITY.coordinate, IDENTITY.revision).state is (
        LifecycleState.RETIRED
    )
    assert [event.event_type for event in fixture.audit.events] == ["search", "load", "execute"]


def test_cancel_callback_failure_does_not_release_execution_pin() -> None:
    callbacks: list[str] = []

    def failing_cancel(target: str) -> bool:
        callbacks.append(target)
        raise RuntimeError("SECRET-CALLBACK-FAILURE")

    fixture = _fixture(cancellable=True, cancel=failing_cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(
            fixture.wrapper.execute,
            ExecutionRequest(fixture.loaded_ref, "run", {}, "task"),
            context=CONTEXT,
            budget=fixture.budget,
        )
        assert fixture.provider.started.wait(2)
        fixture.drain.begin_drain(IDENTITY.coordinate, IDENTITY.revision)
        dispatched = fixture.wrapper.advance_drain(
            IDENTITY.coordinate,
            IDENTITY.revision,
            deadline=0,
            now=0,
        )

        assert dispatched.progress.outcome is DrainOutcome.CANCEL_REQUESTED
        assert dispatched.attempted == 1
        assert dispatched.succeeded == 0
        assert dispatched.failed == 1
        assert callbacks == list(dispatched.progress.cancellation_requests)
        assert dispatched.progress.snapshot.in_flight == 1
        assert fixture.drain.snapshot(IDENTITY.coordinate, IDENTITY.revision).in_flight == 1
        fixture.provider.finish.set()
        running.result(timeout=2)

    assert fixture.drain.snapshot(IDENTITY.coordinate, IDENTITY.revision).state is (
        LifecycleState.RETIRED
    )


def test_service_typed_errors_and_audit_are_preserved_and_pin_is_released() -> None:
    fixture = _fixture(require_argument=True)

    with pytest.raises(CapabilityHubError) as caught:
        fixture.wrapper.execute(
            ExecutionRequest(fixture.loaded_ref, "run", {}, "task"),
            context=CONTEXT,
            budget=fixture.budget,
        )

    assert caught.value.code == "invalid_operation_arguments"
    assert caught.value.category is ErrorCategory.INPUT
    assert fixture.provider.calls == 0
    assert fixture.drain.snapshot(IDENTITY.coordinate, IDENTITY.revision).in_flight == 0
    execute_event = fixture.audit.events[-1]
    assert execute_event.event_type == "execute"
    assert execute_event.outcome == "failure"
    assert execute_event.reason_codes == ("invalid_operation_arguments",)


def test_signed_resolver_rejects_tampered_ref_before_admission() -> None:
    fixture = _fixture()
    tampered = fixture.loaded_ref[:-1] + ("A" if fixture.loaded_ref[-1] != "A" else "B")

    with pytest.raises(CapabilityHubError) as caught:
        fixture.wrapper.execute(
            ExecutionRequest(tampered, "run", {}, "task"),
            context=CONTEXT,
            budget=fixture.budget,
        )

    assert caught.value.category is ErrorCategory.REFERENCE
    assert fixture.provider.calls == 0
    assert fixture.drain.snapshot(IDENTITY.coordinate, IDENTITY.revision).in_flight == 0


def test_duplicate_active_pin_factory_is_rejected_without_sharing_lifecycle_pin() -> None:
    fixture = _fixture()
    duplicate_pin_wrapper = DrainedCapabilityHubService(
        fixture.service,
        drain=fixture.drain,
        resolver=fixture.resolver,
        pin_id_factory=lambda _request, _binding: "fixed-pin",
    )
    request = ExecutionRequest(fixture.loaded_ref, "run", {}, "task")

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(
            duplicate_pin_wrapper.execute,
            request,
            context=CONTEXT,
            budget=fixture.budget,
        )
        assert fixture.provider.started.wait(2)
        with pytest.raises(CapabilityHubError) as caught:
            duplicate_pin_wrapper.execute(request, context=CONTEXT, budget=fixture.budget)
        assert caught.value.code == "execution_pin_conflict"
        assert fixture.provider.calls == 1
        assert fixture.drain.snapshot(IDENTITY.coordinate, IDENTITY.revision).in_flight == 1
        fixture.provider.finish.set()
        running.result(timeout=2)


def test_durable_revision_pin_spans_provider_execution_and_is_released() -> None:
    fixture = _fixture()
    durable: dict[str, str] = {}
    wrapper = DrainedCapabilityHubService(
        fixture.service,
        drain=fixture.drain,
        resolver=fixture.resolver,
        pin_id_factory=lambda _request, _binding: "durable-pin",
        pin_revision=lambda _coordinate, pin_id: durable.setdefault(
            pin_id, IDENTITY.revision
        ),
        release_revision=lambda pin_id: durable.pop(pin_id, None) is not None,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(
            wrapper.execute,
            ExecutionRequest(fixture.loaded_ref, "run", {}, "task"),
            context=CONTEXT,
            budget=fixture.budget,
        )
        assert fixture.provider.started.wait(2)
        assert durable == {"durable-pin": IDENTITY.revision}
        fixture.provider.finish.set()
        running.result(timeout=2)

    assert durable == {}


def test_durable_pointer_change_rejects_admission_and_cleans_temporary_pin() -> None:
    fixture = _fixture()
    durable: set[str] = set()

    def pin_new_revision(_coordinate: str, pin_id: str) -> str:
        durable.add(pin_id)
        return "test/blocking@2#sha256:new"

    wrapper = DrainedCapabilityHubService(
        fixture.service,
        drain=fixture.drain,
        resolver=fixture.resolver,
        pin_id_factory=lambda _request, _binding: "raced-pin",
        pin_revision=pin_new_revision,
        release_revision=lambda pin_id: not durable.remove(pin_id),
    )

    with pytest.raises(CapabilityHubError) as caught:
        wrapper.execute(
            ExecutionRequest(fixture.loaded_ref, "run", {}, "task"),
            context=CONTEXT,
            budget=fixture.budget,
        )

    assert caught.value.code == "lifecycle_not_accepting"
    assert durable == set()
    assert fixture.provider.calls == 0
