"""Lifecycle admission wrapper for the real CapabilityHub service."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import RLock
from typing import Protocol
from uuid import uuid4

from capabilityhub.budget import BudgetLedger
from capabilityhub.draining import DrainController, DrainProgress
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import (
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
    LoadedCapability,
    OperationSpec,
)
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.search import SearchResponse
from capabilityhub.service import CapabilityHubService, ServiceContext


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    coordinate: str
    revision: str
    operation: OperationSpec
    cancellable: bool

    def __post_init__(self) -> None:
        if not self.coordinate or not self.revision:
            raise ValueError("execution binding identifiers must be non-empty")
        if not isinstance(self.cancellable, bool):
            raise TypeError("cancellable must be a boolean")


class ExecutionBindingResolver(Protocol):
    """Resolve an authenticated execution ref to its lifecycle binding."""

    def resolve(
        self, request: ExecutionRequest, *, context: ServiceContext
    ) -> ExecutionBinding: ...


CancellableResolver = Callable[[CapabilityManifest, OperationSpec], bool]
PinIdFactory = Callable[[ExecutionRequest, ExecutionBinding], str]
CancelCallback = Callable[[str], bool]


class SignedExecutionBindingResolver:
    """Verify a service execution reference and resolve its declared operation."""

    def __init__(
        self,
        *,
        references: ReferenceSigner,
        registry: CapabilityRegistry,
        cancellable: CancellableResolver,
    ) -> None:
        self._references = references
        self._registry = registry
        self._cancellable = cancellable

    def resolve(self, request: ExecutionRequest, *, context: ServiceContext) -> ExecutionBinding:
        claims = self._references.verify(
            request.execution_ref,
            expected_scope=context.reference_scope,
            expected_purpose="execution",
        )
        manifest = self._registry.revision(claims.revision)
        operation = manifest.operation(request.operation)
        if operation is None:
            raise CapabilityHubError(
                code="unknown_operation",
                category=ErrorCategory.REFERENCE,
                safe_message="The capability does not declare this operation.",
            )
        try:
            cancellable = self._cancellable(manifest, operation)
        except CapabilityHubError:
            raise
        except Exception as error:
            raise _wrapper_error("execution_cancellability_resolution_failed") from error
        if not isinstance(cancellable, bool):
            raise _wrapper_error("execution_cancellability_resolution_failed")
        return ExecutionBinding(
            manifest.identity.coordinate,
            manifest.identity.revision,
            operation,
            cancellable,
        )


@dataclass(frozen=True, slots=True)
class CancellationDispatch:
    progress: DrainProgress
    attempted: int
    succeeded: int
    failed: int


class DrainedCapabilityHubService:
    """Delegate search/load and guard execute with a lifecycle admission pin."""

    def __init__(
        self,
        service: CapabilityHubService,
        *,
        drain: DrainController,
        resolver: ExecutionBindingResolver,
        cancel: CancelCallback | None = None,
        pin_id_factory: PinIdFactory | None = None,
    ) -> None:
        self._service = service
        self._drain = drain
        self._resolver = resolver
        self._cancel = cancel
        self._pin_id_factory = pin_id_factory or _random_pin_id
        self._active_pin_ids: set[str] = set()
        self._pin_lock = RLock()

    def search(
        self,
        query: str,
        *,
        task_id: str,
        context: ServiceContext,
        budget: BudgetLedger,
        kinds: Iterable[CapabilityKind | str] | None = None,
        limit: int = 8,
        max_output_tokens: int = 900,
        include_cards: bool = True,
        inventory: dict[str, JsonValue] | None = None,
    ) -> SearchResponse:
        return self._service.search(
            query,
            task_id=task_id,
            context=context,
            budget=budget,
            kinds=kinds,
            limit=limit,
            max_output_tokens=max_output_tokens,
            include_cards=include_cards,
            inventory=inventory,
        )

    def load(
        self,
        capability_ref: str,
        *,
        task_id: str,
        context: ServiceContext,
        budget: BudgetLedger,
        section_names: Iterable[str] | None = None,
        operation_names: Iterable[str] | None = None,
        max_output_tokens: int = 2_000,
    ) -> LoadedCapability:
        return self._service.load(
            capability_ref,
            task_id=task_id,
            context=context,
            budget=budget,
            section_names=section_names,
            operation_names=operation_names,
            max_output_tokens=max_output_tokens,
        )

    def execute(
        self,
        request: ExecutionRequest,
        *,
        context: ServiceContext,
        budget: BudgetLedger,
        max_output_tokens: int | None = None,
    ) -> ExecutionResult:
        binding = self._resolver.resolve(request, context=context)
        if binding.operation.name != request.operation:
            raise _wrapper_error("execution_binding_mismatch")
        try:
            pin_id = self._pin_id_factory(request, binding)
        except Exception as error:
            raise _wrapper_error("execution_pin_creation_failed") from error
        if not isinstance(pin_id, str) or not pin_id:
            raise _wrapper_error("execution_pin_creation_failed")
        with self._pin_lock:
            if pin_id in self._active_pin_ids:
                raise _wrapper_error("execution_pin_conflict")
            self._active_pin_ids.add(pin_id)
        try:
            pin = self._drain.admit(
                binding.coordinate,
                binding.revision,
                pin_id,
                cancellable=binding.cancellable,
            )
            try:
                return self._service.execute(
                    request,
                    context=context,
                    budget=budget,
                    max_output_tokens=max_output_tokens,
                )
            finally:
                self._drain.release(pin.pin_id)
        finally:
            with self._pin_lock:
                self._active_pin_ids.discard(pin_id)

    def advance_drain(
        self,
        coordinate: str,
        revision: str | None = None,
        *,
        deadline: float,
        now: float | None = None,
    ) -> CancellationDispatch:
        progress = self._drain.advance(
            coordinate,
            revision,
            deadline=deadline,
            now=now,
        )
        succeeded = 0
        failed = 0
        for target in progress.cancellation_requests:
            try:
                cancelled = self._cancel(target) if self._cancel is not None else False
            except Exception:
                cancelled = False
            if cancelled:
                succeeded += 1
            else:
                failed += 1
        return CancellationDispatch(
            progress=progress,
            attempted=len(progress.cancellation_requests),
            succeeded=succeeded,
            failed=failed,
        )


def _random_pin_id(_request: ExecutionRequest, _binding: ExecutionBinding) -> str:
    return f"execution-{uuid4().hex}"


def _wrapper_error(code: str) -> CapabilityHubError:
    messages = {
        "execution_binding_mismatch": "The execution binding does not match the request.",
        "execution_cancellability_resolution_failed": (
            "Execution cancellation capability could not be resolved safely."
        ),
        "execution_pin_creation_failed": "The execution admission pin could not be created.",
        "execution_pin_conflict": "The execution admission pin is already active.",
    }
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.INTERNAL,
        safe_message=messages[code],
        retryable=False,
    )
