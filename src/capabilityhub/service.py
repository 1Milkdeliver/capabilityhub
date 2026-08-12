"""Transport-neutral application service for CapabilityHub's three meta-tools."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from threading import RLock

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from capabilityhub.audit import AuditEvent, AuditSink
from capabilityhub.authorization import ParameterAuthorizer
from capabilityhub.budget import BudgetLedger, BudgetReservation
from capabilityhub.degraded import DecisionOutcome, DegradedDecision
from capabilityhub.degraded import Operation as DependencyOperation
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.idempotency import IdempotencyRecord, IdempotencySlot, IdempotencyStore
from capabilityhub.metering import canonical_json, measure_text
from capabilityhub.models import (
    CapabilityKind,
    CapabilityManifest,
    CapabilityNotice,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
    LoadedCapability,
    OmissionKind,
    OperationSpec,
    RehydrationHandle,
    SectionDescriptor,
    SideEffect,
)
from capabilityhub.policy import PolicyContext, PolicyOutcome, ReferencePolicy
from capabilityhub.providers.base import CapabilityProvider, ProviderContext
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.resilience import FailureCertainty, ResilientProviderExecutor
from capabilityhub.search import LexicalCapabilitySearch, SearchResponse
from capabilityhub.supervision import ProviderSupervisor

MAX_REHYDRATION_HANDLES = 4
MAX_OMISSION_NAMES_PER_KIND = 8
MAX_LOAD_NOTICES = 4


@dataclass(frozen=True, slots=True)
class ServiceContext:
    tenant_id: str
    principal_id: str
    session_id: str
    granted_permissions: frozenset[str] = frozenset()
    parameter_authorizer: ParameterAuthorizer | None = None
    approved: bool = False
    allow_irreversible: bool = False
    deadline_ms: int = 30_000
    max_output_tokens: int = 2_000

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.principal_id or not self.session_id:
            raise ValueError("tenant_id, principal_id, and session_id must be non-empty")
        if self.deadline_ms <= 0 or self.max_output_tokens <= 0:
            raise ValueError("deadline_ms and max_output_tokens must be positive")

    @property
    def reference_scope(self) -> str:
        return canonical_json(
            {
                "principal": self.principal_id,
                "session": self.session_id,
                "tenant": self.tenant_id,
            }
        )


@dataclass(frozen=True, slots=True)
class _ExecutionGrant:
    revision: str
    operations: frozenset[str]


@dataclass(slots=True)
class _IdempotencyRecord:
    arguments_digest: str
    status: str
    result: ExecutionResult | None = None


@dataclass(slots=True)
class _SharedServiceState:
    grants: dict[str, _ExecutionGrant]
    idempotency: dict[tuple[str, str, str, str, str], _IdempotencyRecord]
    sequence: int
    lock: RLock


class CapabilityHubService:
    """Coordinates search, progressive load, and governed provider execution."""

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        providers: Iterable[CapabilityProvider] | Mapping[str, CapabilityProvider],
        references: ReferenceSigner,
        audit: AuditSink,
        policy: ReferencePolicy | None = None,
        load_ref_ttl_seconds: int = 300,
        execution_ref_ttl_seconds: int = 300,
        idempotency_store: IdempotencyStore | None = None,
        provider_supervisor: ProviderSupervisor | None = None,
        provider_executor: ResilientProviderExecutor[ExecutionResult] | None = None,
        retry_certainty_classifier: Callable[[CapabilityHubError], FailureCertainty] | None = None,
        dependency_decider: Callable[[DependencyOperation], DegradedDecision] | None = None,
        _shared_state: _SharedServiceState | None = None,
    ) -> None:
        if execution_ref_ttl_seconds <= 0:
            raise ValueError("execution_ref_ttl_seconds must be positive")
        values = tuple(providers.values()) if isinstance(providers, Mapping) else tuple(providers)
        by_name: dict[str, CapabilityProvider] = {}
        for provider in values:
            if not provider.name or provider.name in by_name:
                raise ValueError("provider names must be non-empty and unique")
            by_name[provider.name] = provider
        self._registry = registry
        self._providers = by_name
        self._references = references
        self._audit = audit
        self._policy = policy or ReferencePolicy()
        self._search_engine = LexicalCapabilitySearch(
            registry, references, load_ref_ttl_seconds=load_ref_ttl_seconds
        )
        self._load_ref_ttl = load_ref_ttl_seconds
        self._execution_ttl = execution_ref_ttl_seconds
        self._idempotency_store = idempotency_store
        self._provider_supervisor = provider_supervisor
        self._provider_executor = provider_executor
        self._retry_certainty_classifier = retry_certainty_classifier
        self._dependency_decider = dependency_decider
        self._shared = _shared_state or _SharedServiceState({}, {}, 0, RLock())

    def fork_catalog(
        self,
        *,
        registry: CapabilityRegistry,
        providers: Iterable[CapabilityProvider] | Mapping[str, CapabilityProvider],
    ) -> CapabilityHubService:
        """Create an immutable catalog generation while retaining refs, grants, and audit order."""

        return CapabilityHubService(
            registry=registry,
            providers=providers,
            references=self._references,
            audit=self._audit,
            policy=self._policy,
            load_ref_ttl_seconds=self._load_ref_ttl,
            execution_ref_ttl_seconds=self._execution_ttl,
            idempotency_store=self._idempotency_store,
            provider_supervisor=self._provider_supervisor,
            provider_executor=self._provider_executor,
            retry_certainty_classifier=self._retry_certainty_classifier,
            dependency_decider=self._dependency_decider,
            _shared_state=self._shared,
        )

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
        try:
            self._guard_dependencies(DependencyOperation.SEARCH)
            response = self._search_engine.search(
                query,
                scope=context.reference_scope,
                kinds=kinds,
                limit=limit,
                max_output_tokens=max_output_tokens,
                include_cards=include_cards,
                inventory=inventory,
                allowed_revisions=self._visible_revisions(context),
            )
            budget.spend(
                {"portable_tokens": response.portable_tokens, "bytes": response.payload_bytes}
            )
        except CapabilityHubError as error:
            self._emit(task_id, "search", None, "failure", reason_codes=(error.code,))
            raise
        self._emit(
            task_id,
            "search",
            None,
            "success",
            portable_tokens=response.portable_tokens,
            payload_bytes=response.payload_bytes,
            metadata={
                "result_count": len(response.cards),
                "total_matches": response.total_matches,
                "truncated": response.truncated,
            },
        )
        return response

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
        manifest: CapabilityManifest | None = None
        try:
            self._guard_dependencies(DependencyOperation.LOAD)
            _positive_budget(max_output_tokens)
            claims = self._references.verify(
                capability_ref,
                expected_scope=context.reference_scope,
                expected_purpose="load",
            )
            manifest = self._active_manifest(claims.revision)
            sections = _select_sections(manifest, section_names)
            if any(section.sensitive for section in sections) and (
                "content.sensitive" not in context.granted_permissions
            ):
                raise _policy_error(
                    "sensitive_section_denied",
                    "Loading sensitive capability content requires explicit permission.",
                )
            operations = _select_operations(manifest, operation_names)
            handles = self._rehydration_handles(manifest, sections, operations, context)
            notices, omitted_notice_count = _manifest_notices(manifest)
            execution_ref = ""
            if manifest.kind is not CapabilityKind.SKILL:
                execution_ref = self._references.issue(
                    revision=manifest.identity.revision,
                    scope=context.reference_scope,
                    purpose="execution",
                    ttl_seconds=self._execution_ttl,
                )
            loaded, payload_bytes = _loaded_response(
                manifest,
                sections,
                operations,
                execution_ref,
                notices,
                handles,
                omitted_notice_count,
            )
            if loaded.portable_tokens > max_output_tokens:
                raise _budget(
                    "load_output_budget_exceeded",
                    "The selected capability material exceeds the load output budget.",
                    required_tokens=loaded.portable_tokens,
                    max_output_tokens=max_output_tokens,
                )
            budget.spend(
                {
                    "bytes": payload_bytes,
                    "loads": 1,
                    "portable_tokens": loaded.portable_tokens,
                }
            )
            if execution_ref:
                with self._shared.lock:
                    self._shared.grants[execution_ref] = _ExecutionGrant(
                        manifest.identity.revision,
                        frozenset(operation.name for operation in operations),
                    )
        except CapabilityHubError as error:
            self._emit(
                task_id,
                "load",
                manifest.identity.revision if manifest is not None else None,
                "failure",
                reason_codes=(error.code,),
            )
            raise
        self._emit(
            task_id,
            "load",
            manifest.identity.revision,
            "success",
            portable_tokens=loaded.portable_tokens,
            payload_bytes=payload_bytes,
            metadata={
                "operation_count": len(loaded.operations),
                "section_count": len(loaded.sections),
                "skill_load_only": manifest.kind is CapabilityKind.SKILL,
            },
        )
        return loaded

    def rehydrate(
        self,
        handle: RehydrationHandle,
        *,
        task_id: str,
        context: ServiceContext,
        budget: BudgetLedger,
        max_output_tokens: int = 2_000,
    ) -> LoadedCapability:
        """Load exactly one previously omitted section or operation."""

        purpose = _rehydration_purpose(handle.kind, handle.selector_digest)
        claims = self._references.verify(
            handle.reference,
            expected_scope=context.reference_scope,
            expected_purpose=purpose,
        )
        if claims.expires_at != handle.expires_at:
            raise _reference(
                "rehydration_expiry_mismatch",
                "The rehydration handle expiry does not match its signed reference.",
            )
        manifest = self._active_manifest(claims.revision)
        target = _resolve_rehydration_target(manifest, handle.kind, handle.selector_digest)
        load_ref = self._references.issue(
            revision=manifest.identity.revision,
            scope=context.reference_scope,
            purpose="load",
            ttl_seconds=self._load_ref_ttl,
        )
        return self.load(
            load_ref,
            task_id=task_id,
            context=context,
            budget=budget,
            section_names=(target,) if handle.kind is OmissionKind.SECTION else (),
            operation_names=(target,) if handle.kind is OmissionKind.OPERATION else (),
            max_output_tokens=max_output_tokens,
        )

    def _rehydration_handles(
        self,
        manifest: CapabilityManifest,
        sections: tuple[SectionDescriptor, ...],
        operations: tuple[OperationSpec, ...],
        context: ServiceContext,
    ) -> tuple[RehydrationHandle, ...]:
        selected_sections = {section.name for section in sections}
        selected_operations = {operation.name for operation in operations}
        targets = [
            *(
                (OmissionKind.SECTION, section.name)
                for section in manifest.sections
                if section.name not in selected_sections
            ),
            *(
                (OmissionKind.OPERATION, operation.name)
                for operation in manifest.operations
                if operation.name not in selected_operations
            ),
        ]
        handles: list[RehydrationHandle] = []
        for kind, name in targets[:MAX_REHYDRATION_HANDLES]:
            selector = _rehydration_selector(manifest.identity.revision, kind, name)
            reference = self._references.issue(
                revision=manifest.identity.revision,
                scope=context.reference_scope,
                purpose=_rehydration_purpose(kind, selector),
                ttl_seconds=self._load_ref_ttl,
            )
            claims = self._references.verify(
                reference,
                expected_scope=context.reference_scope,
                expected_purpose=_rehydration_purpose(kind, selector),
                expected_revision=manifest.identity.revision,
            )
            handles.append(RehydrationHandle(kind, selector, reference, claims.expires_at))
        return tuple(handles)

    def issue_approval(
        self,
        *,
        revision: str,
        operation: str,
        arguments: Mapping[str, JsonValue],
        task_id: str,
        context: ServiceContext,
        ttl_seconds: int = 300,
    ) -> str:
        """Issue a short-lived approval bound to one exact execution intent.

        This is a control-plane library operation and is deliberately not exposed as
        a model-facing MCP tool.
        """

        manifest = self._active_manifest(revision)
        if manifest.operation(operation) is None:
            raise _reference("unknown_operation", "The capability does not declare this operation.")
        approval_ref = self._references.issue(
            revision=revision,
            scope=_approval_scope(context, operation, arguments),
            purpose="approval",
            ttl_seconds=ttl_seconds,
        )
        self._emit(
            task_id,
            "approval_issue",
            revision,
            "success",
            metadata={"operation": operation},
        )
        return approval_ref

    def execute(
        self,
        request: ExecutionRequest,
        *,
        context: ServiceContext,
        budget: BudgetLedger,
        max_output_tokens: int | None = None,
    ) -> ExecutionResult:
        limit = context.max_output_tokens if max_output_tokens is None else max_output_tokens
        manifest: CapabilityManifest | None = None
        reservation: BudgetReservation | None = None
        idempotency_slot: tuple[str, str, str, str, str] | None = None
        try:
            self._guard_dependencies(DependencyOperation.EXECUTE)
            _positive_budget(limit)
            claims = self._references.verify(
                request.execution_ref,
                expected_scope=context.reference_scope,
                expected_purpose="execution",
            )
            manifest = self._active_manifest(claims.revision)
            if manifest.kind is CapabilityKind.SKILL:
                raise _policy_error(
                    "skill_execution_not_supported",
                    "Skills are load-only and cannot be executed.",
                )
            grant = self._grant(request.execution_ref)
            if (
                grant.revision != manifest.identity.revision
                or request.operation not in grant.operations
            ):
                raise _reference(
                    "operation_not_loaded",
                    "The requested operation was not selected by the load call.",
                )
            operation = manifest.operation(request.operation)
            if operation is None:
                raise _reference(
                    "unknown_operation", "The capability does not declare this operation."
                )
            _validate_arguments(operation, request.arguments)
            if context.parameter_authorizer is not None:
                authorization = context.parameter_authorizer.authorize(
                    manifest,
                    dependencies=self._dependency_manifests(manifest),
                    normalized_arguments=request.arguments,
                )
                if not authorization.allowed:
                    raise CapabilityHubError(
                        code="argument_authorization_denied",
                        category=ErrorCategory.POLICY,
                        safe_message="Execution arguments were not allowed by policy.",
                        details={"reason_codes": authorization.reason_codes},
                    )
            approved = False
            if request.approval_ref is not None:
                self._references.verify(
                    request.approval_ref,
                    expected_scope=_approval_scope(context, request.operation, request.arguments),
                    expected_revision=manifest.identity.revision,
                    expected_purpose="approval",
                )
                approved = True
            decision = self._policy.decide(
                manifest,
                operation,
                PolicyContext(
                    context.granted_permissions,
                    approved=approved,
                    allow_irreversible=context.allow_irreversible,
                ),
            )
            if decision.outcome is not PolicyOutcome.ALLOW:
                category = (
                    ErrorCategory.APPROVAL
                    if decision.outcome is PolicyOutcome.APPROVAL_REQUIRED
                    else ErrorCategory.POLICY
                )
                raise CapabilityHubError(
                    code=decision.outcome.value,
                    category=category,
                    safe_message="Execution was not allowed by policy.",
                    details={"reason_codes": decision.reason_codes},
                )
            if (
                operation.side_effect in {SideEffect.REVERSIBLE_WRITE, SideEffect.IRREVERSIBLE}
                and request.idempotency_key is None
            ):
                raise CapabilityHubError(
                    code="idempotency_key_required",
                    category=ErrorCategory.POLICY,
                    safe_message="Write operations require an idempotency key.",
                )
            provider = self._providers.get(manifest.provider)
            if provider is None:
                raise CapabilityHubError(
                    code="provider_not_configured",
                    category=ErrorCategory.PROVIDER,
                    safe_message="The capability's named provider is not configured.",
                )
            reservation = budget.reserve({"executions": 1, "portable_tokens": limit})
            try:
                replay, idempotency_slot = self._admit_idempotency(
                    request,
                    context,
                    manifest.identity.revision,
                    operation.side_effect,
                )
            except Exception:
                reservation.cancel()
                reservation = None
                raise
            if replay is not None:
                reservation.cancel()
                reservation = None
                self._emit(
                    request.task_id,
                    "execute",
                    manifest.identity.revision,
                    "success",
                    portable_tokens=replay.portable_tokens,
                    reason_codes=("idempotent_replay",),
                    metadata={"operation": replay.operation, "provider": replay.provider},
                )
                return replay
            provider_context = ProviderContext(
                context.tenant_id,
                context.principal_id,
                context.session_id,
                context.deadline_ms,
                limit,
            )

            def invoke_provider() -> ExecutionResult:
                if self._provider_supervisor is None:
                    return provider.execute(manifest.identity, request, provider_context)
                return self._provider_supervisor.execute(
                    provider, manifest.identity, request, provider_context
                )

            if self._provider_executor is None:
                result = invoke_provider()
            else:
                result = self._provider_executor.execute(
                    provider.name,
                    invoke_provider,
                    operation=operation,
                    request=request,
                    deadline_seconds=context.deadline_ms / 1_000,
                    classify_certainty=self._retry_certainty_classifier,
                )
            result = self._normalize_result(result, manifest, request, provider.name)
            _validate_provider_output(operation, result.output)
            if result.portable_tokens > limit:
                reservation.reconcile({"executions": 1, "portable_tokens": limit})
                reservation = None
                raise _budget(
                    "provider_output_budget_exceeded",
                    "Provider output exceeded the hard output budget.",
                    max_output_tokens=limit,
                )
            reservation.reconcile({"executions": 1, "portable_tokens": result.portable_tokens})
            reservation = None
            self._complete_idempotency(idempotency_slot, result)
        except CapabilityHubError as error:
            if reservation is not None and reservation.active:
                reservation.reconcile({"executions": 1})
            self._mark_idempotency_uncertain(idempotency_slot)
            self._emit(
                request.task_id,
                "execute",
                manifest.identity.revision if manifest is not None else None,
                "failure",
                reason_codes=(error.code,),
            )
            raise

        except Exception as error:
            if reservation is not None and reservation.active:
                reservation.reconcile({"executions": 1})
            self._mark_idempotency_uncertain(idempotency_slot)
            self._emit(
                request.task_id,
                "execute",
                manifest.identity.revision if manifest is not None else None,
                "failure",
                reason_codes=("provider_unhandled_error",),
            )
            raise CapabilityHubError(
                code="provider_unhandled_error",
                category=ErrorCategory.PROVIDER,
                safe_message="The provider failed without a safe structured error.",
            ) from error
        self._emit(
            request.task_id,
            "execute",
            manifest.identity.revision,
            "success",
            portable_tokens=result.portable_tokens,
            metadata={"operation": result.operation, "provider": result.provider},
        )
        return result

    def _guard_dependencies(self, operation: DependencyOperation) -> None:
        if self._dependency_decider is None:
            return
        enforce_dependency_decision(self._dependency_decider(operation))

    def _active_manifest(self, revision: str) -> CapabilityManifest:
        try:
            manifest = self._registry.revision(revision)
        except CapabilityHubError as error:
            if error.code == "unknown_revision":
                raise _reference(
                    "stale_revision", "The referenced capability revision is no longer active."
                ) from error
            raise
        active_revision = self._registry.activations.get(manifest.identity.coordinate)
        if active_revision != revision:
            raise _reference(
                "stale_revision", "The referenced capability revision is no longer active."
            )
        return manifest

    def _grant(self, execution_ref: str) -> _ExecutionGrant:
        with self._shared.lock:
            grant = self._shared.grants.get(execution_ref)
        if grant is None:
            raise _reference(
                "unknown_execution_grant",
                "The execution reference was not issued by this service instance.",
            )
        return grant

    def _visible_revisions(self, context: ServiceContext) -> frozenset[str]:
        if context.parameter_authorizer is not None:
            return frozenset(
                revision
                for revision in self._registry.activations.values()
                if context.parameter_authorizer.eligible(
                    self._registry.revision(revision),
                    self._dependency_manifests(self._registry.revision(revision)),
                ).allowed
            )
        return frozenset(
            revision
            for revision in self._registry.activations.values()
            if set(self._registry.revision(revision).permissions) <= context.granted_permissions
        )

    def _dependency_manifests(self, manifest: CapabilityManifest) -> tuple[CapabilityManifest, ...]:
        return tuple(
            self._registry.active(dependency.coordinate)
            for dependency in manifest.dependencies
            if dependency.coordinate in self._registry.activations
        )

    def _admit_idempotency(
        self,
        request: ExecutionRequest,
        context: ServiceContext,
        revision: str,
        side_effect: SideEffect,
    ) -> tuple[ExecutionResult | None, IdempotencySlot | None]:
        key = request.idempotency_key
        if key is None:
            return None, None
        if not isinstance(key, str) or not key or len(key) > 256:
            raise CapabilityHubError(
                code="invalid_idempotency_key",
                category=ErrorCategory.INPUT,
                safe_message="idempotency_key must contain 1 to 256 characters.",
            )
        slot = (
            context.reference_scope,
            request.task_id,
            revision,
            request.operation,
            key,
        )
        arguments_digest = _json_digest(
            {
                "arguments": dict(request.arguments),
                "side_effect": side_effect.value,
            }
        )
        existing: _IdempotencyRecord | IdempotencyRecord | None
        if self._idempotency_store is not None:
            existing = self._idempotency_store.reserve(slot, arguments_digest)
            if existing is None:
                return None, slot
            if existing.status == "in_progress":
                existing = self._idempotency_store.wait(
                    slot,
                    arguments_digest,
                    context.deadline_ms / 1_000,
                )
        else:
            with self._shared.lock:
                existing = self._shared.idempotency.get(slot)
                if existing is None:
                    self._shared.idempotency[slot] = _IdempotencyRecord(
                        arguments_digest, "in_progress"
                    )
                    return None, slot
        with self._shared.lock:
            if existing.arguments_digest != arguments_digest:
                raise CapabilityHubError(
                    code="idempotency_conflict",
                    category=ErrorCategory.CONFLICT,
                    safe_message="The idempotency key is already bound to different arguments.",
                )
            if existing.status == "complete" and existing.result is not None:
                return existing.result, slot
            if existing.status == "in_progress":
                code = "idempotency_in_progress"
            elif existing.status == "complete":
                code = "idempotency_result_unavailable"
            else:
                code = "idempotency_outcome_unknown"
            raise CapabilityHubError(
                code=code,
                category=ErrorCategory.CONFLICT,
                safe_message="The idempotent execution cannot be replayed safely yet.",
                retryable=existing.status == "in_progress",
            )

    def _complete_idempotency(
        self,
        slot: IdempotencySlot | None,
        result: ExecutionResult,
    ) -> None:
        if slot is None:
            return
        if self._idempotency_store is not None:
            self._idempotency_store.complete(slot, result)
            return
        with self._shared.lock:
            record = self._shared.idempotency.get(slot)
            if record is not None:
                record.status = "complete"
                record.result = result

    def _mark_idempotency_uncertain(self, slot: IdempotencySlot | None) -> None:
        if slot is None:
            return
        if self._idempotency_store is not None:
            self._idempotency_store.uncertain(slot)
            return
        with self._shared.lock:
            record = self._shared.idempotency.get(slot)
            if record is not None and record.status == "in_progress":
                record.status = "uncertain"

    @staticmethod
    def _normalize_result(
        result: ExecutionResult,
        manifest: CapabilityManifest,
        request: ExecutionRequest,
        provider_name: str,
    ) -> ExecutionResult:
        if (
            not isinstance(result, ExecutionResult)
            or result.capability_revision != manifest.identity.revision
            or result.operation != request.operation
            or result.provider != provider_name
            or result.portable_tokens < 0
        ):
            raise CapabilityHubError(
                code="invalid_provider_result",
                category=ErrorCategory.PROVIDER,
                safe_message="The provider returned an invalid result envelope.",
            )
        try:
            actual_tokens = measure_text(canonical_json(result.output)).portable_tokens
        except (TypeError, ValueError) as error:
            raise CapabilityHubError(
                code="invalid_provider_result",
                category=ErrorCategory.PROVIDER,
                safe_message="The provider returned output that is not valid JSON data.",
            ) from error
        return ExecutionResult(
            capability_revision=result.capability_revision,
            operation=result.operation,
            output=result.output,
            provider=result.provider,
            portable_tokens=actual_tokens,
            audit_id=result.audit_id,
        )

    def _emit(
        self,
        task_id: str,
        event_type: str,
        revision: str | None,
        outcome: str,
        *,
        portable_tokens: int = 0,
        payload_bytes: int = 0,
        reason_codes: tuple[str, ...] = (),
        metadata: dict[str, JsonValue] | None = None,
    ) -> None:
        with self._shared.lock:
            self._shared.sequence += 1
            sequence = self._shared.sequence
        self._audit.emit(
            AuditEvent(
                event_id=f"evt-{sequence:08d}",
                sequence=sequence,
                task_id=task_id,
                event_type=event_type,
                capability_revision=revision,
                outcome=outcome,
                portable_tokens=portable_tokens,
                payload_bytes=payload_bytes,
                reason_codes=reason_codes,
                metadata=metadata,
            )
        )


def _select_sections(
    manifest: CapabilityManifest, names: Iterable[str] | None
) -> tuple[SectionDescriptor, ...]:
    if names is None:
        return manifest.sections
    requested = frozenset(names)
    known = {section.name for section in manifest.sections}
    unknown = sorted(requested - known)
    if unknown:
        raise _reference(
            "unknown_section", "A requested section is not declared by the capability."
        )
    return tuple(section for section in manifest.sections if section.name in requested)


def _select_operations(
    manifest: CapabilityManifest, names: Iterable[str] | None
) -> tuple[OperationSpec, ...]:
    if names is None:
        return manifest.operations
    requested = frozenset(names)
    known = {operation.name for operation in manifest.operations}
    unknown = sorted(requested - known)
    if unknown:
        raise _reference(
            "unknown_operation", "A requested operation is not declared by the capability."
        )
    return tuple(operation for operation in manifest.operations if operation.name in requested)


def _loaded_response(
    manifest: CapabilityManifest,
    sections: tuple[SectionDescriptor, ...],
    operations: tuple[OperationSpec, ...],
    execution_ref: str,
    notices: tuple[CapabilityNotice, ...],
    rehydration_handles: tuple[RehydrationHandle, ...],
    omitted_notice_count: int,
) -> tuple[LoadedCapability, int]:
    portable_tokens = 0
    payload_bytes = 0
    all_omitted_sections = tuple(
        section.name for section in manifest.sections if section not in sections
    )
    all_omitted_operations = tuple(
        operation.name for operation in manifest.operations if operation not in operations
    )
    omitted = all_omitted_sections[:MAX_OMISSION_NAMES_PER_KIND]
    omitted_operations = all_omitted_operations[:MAX_OMISSION_NAMES_PER_KIND]
    total_omissions = len(all_omitted_sections) + len(all_omitted_operations)
    unhandled_omissions = max(0, total_omissions - len(rehydration_handles))
    for _ in range(4):
        loaded = LoadedCapability(
            revision=manifest.identity.revision,
            sections=sections,
            operations=operations,
            permissions=manifest.permissions,
            portable_tokens=portable_tokens,
            execution_ref=execution_ref,
            omitted_sections=omitted,
            omitted_operations=omitted_operations,
            notices=notices,
            rehydration_handles=rehydration_handles,
            omitted_section_count=len(all_omitted_sections),
            omitted_operation_count=len(all_omitted_operations),
            omitted_notice_count=omitted_notice_count,
            unhandled_omission_count=unhandled_omissions,
        )
        measured = measure_text(canonical_json(_loaded_dict(loaded)))
        if measured.portable_tokens == portable_tokens and measured.utf8_bytes == payload_bytes:
            return loaded, payload_bytes
        portable_tokens = measured.portable_tokens
        payload_bytes = measured.utf8_bytes
    return LoadedCapability(
        revision=manifest.identity.revision,
        sections=sections,
        operations=operations,
        permissions=manifest.permissions,
        portable_tokens=portable_tokens,
        execution_ref=execution_ref,
        omitted_sections=omitted,
        omitted_operations=omitted_operations,
        notices=notices,
        rehydration_handles=rehydration_handles,
        omitted_section_count=len(all_omitted_sections),
        omitted_operation_count=len(all_omitted_operations),
        omitted_notice_count=omitted_notice_count,
        unhandled_omission_count=unhandled_omissions,
    ), payload_bytes


def _loaded_dict(loaded: LoadedCapability) -> dict[str, JsonValue]:
    return {
        "execution_ref": loaded.execution_ref,
        "notices": [
            {
                "attributes": dict(notice.attributes),
                "code": notice.code,
                "kind": notice.kind,
            }
            for notice in loaded.notices
        ],
        "omitted_notice_count": loaded.omitted_notice_count,
        "omitted_operation_count": loaded.omitted_operation_count,
        "omitted_operations": list(loaded.omitted_operations),
        "omitted_section_count": loaded.omitted_section_count,
        "omitted_sections": list(loaded.omitted_sections),
        "operations": [
            {
                "input_schema": dict(operation.input_schema),
                "name": operation.name,
                "operation_type": operation.operation_type.value,
                "output_schema": dict(operation.output_schema),
                "requires_approval": operation.requires_approval,
                "side_effect": operation.side_effect.value,
            }
            for operation in loaded.operations
        ],
        "permissions": list(loaded.permissions),
        "portable_tokens": loaded.portable_tokens,
        "rehydration_handles": [
            {
                "expires_at": handle.expires_at,
                "kind": handle.kind.value,
                "reference": handle.reference,
                "selector_digest": handle.selector_digest,
            }
            for handle in loaded.rehydration_handles
        ],
        "revision": loaded.revision,
        "sections": [
            {
                "content": section.content,
                "media_type": section.media_type,
                "name": section.name,
                "portable_tokens": section.portable_tokens,
                "sensitive": section.sensitive,
            }
            for section in loaded.sections
        ],
        "unhandled_omission_count": loaded.unhandled_omission_count,
    }


def _manifest_notices(
    manifest: CapabilityManifest,
) -> tuple[tuple[CapabilityNotice, ...], int]:
    notices = [
        CapabilityNotice(
            kind="dependency",
            code="dependency.optional" if dependency.optional else "dependency.required",
            attributes={
                "coordinate": dependency.coordinate,
                "optional": dependency.optional,
                "version_constraint": dependency.version_constraint,
            },
        )
        for dependency in sorted(manifest.dependencies, key=lambda item: item.coordinate)
    ]
    notices.extend(
        CapabilityNotice(
            kind="conflict",
            code="conflict.declared",
            attributes={
                "type": conflict.conflict_type,
                "value_digest": "sha256:"
                + hashlib.sha256(conflict.value.encode("utf-8")).hexdigest(),
            },
        )
        for conflict in sorted(
            manifest.conflicts, key=lambda item: (item.conflict_type, item.value)
        )
    )
    return tuple(notices[:MAX_LOAD_NOTICES]), max(0, len(notices) - MAX_LOAD_NOTICES)


def _rehydration_selector(revision: str, kind: OmissionKind, name: str) -> str:
    payload = canonical_json({"kind": kind.value, "name": name, "revision": revision})
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rehydration_purpose(kind: OmissionKind, selector: str) -> str:
    return f"rehydration:{kind.value}:{selector}"


def _resolve_rehydration_target(
    manifest: CapabilityManifest, kind: OmissionKind, selector: str
) -> str:
    names = (
        (section.name for section in manifest.sections)
        if kind is OmissionKind.SECTION
        else (operation.name for operation in manifest.operations)
    )
    matches = [
        name
        for name in names
        if _rehydration_selector(manifest.identity.revision, kind, name) == selector
    ]
    if len(matches) != 1:
        raise _reference(
            "rehydration_target_invalid",
            "The rehydration handle does not identify one current omission target.",
        )
    return matches[0]


def enforce_dependency_decision(decision: DegradedDecision) -> None:
    """Fail closed with one stable, location-free dependency error contract."""

    if decision.outcome is not DecisionOutcome.DENY:
        return
    raise CapabilityHubError(
        code=f"dependency_{decision.operation.value}_denied",
        category=ErrorCategory.DEPENDENCY,
        safe_message="Required local capability dependencies are not available.",
        details={"reason_codes": decision.reasons},
    )


def _positive_budget(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _budget("invalid_output_budget", "Output budget must be a positive integer.")


def _reference(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.REFERENCE, safe_message=message)


def _policy_error(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.POLICY, safe_message=message)


def _budget(code: str, message: str, **details: JsonValue) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.BUDGET,
        safe_message=message,
        details=details,
    )


def _approval_scope(
    context: ServiceContext,
    operation: str,
    arguments: Mapping[str, JsonValue],
) -> str:
    arguments_digest = _json_digest(dict(arguments))
    actor_digest = hashlib.sha256(context.reference_scope.encode("utf-8")).hexdigest()
    return canonical_json(
        {
            "actor_sha256": actor_digest,
            "arguments_sha256": arguments_digest,
            "operation": operation,
        }
    )


def _json_digest(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _validate_arguments(operation: OperationSpec, arguments: Mapping[str, JsonValue]) -> None:
    schema = dict(operation.input_schema)
    if not schema or set(schema) == {"$ref"}:
        return
    try:
        Draft202012Validator(schema).validate(dict(arguments))
    except (SchemaError, ValidationError) as error:
        raise CapabilityHubError(
            code="invalid_operation_arguments",
            category=ErrorCategory.INPUT,
            safe_message="Execution arguments do not satisfy the operation schema.",
        ) from error


def _validate_provider_output(operation: OperationSpec, output: JsonValue) -> None:
    schema = dict(operation.output_schema)
    if not schema or set(schema) == {"$ref"}:
        return
    try:
        Draft202012Validator(schema).validate(output)
    except (SchemaError, ValidationError) as error:
        raise CapabilityHubError(
            code="invalid_provider_result",
            category=ErrorCategory.PROVIDER,
            safe_message="The provider output does not satisfy the operation schema.",
        ) from error
