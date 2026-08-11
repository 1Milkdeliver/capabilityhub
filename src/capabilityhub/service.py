"""Transport-neutral application service for CapabilityHub's three meta-tools."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from threading import RLock

from capabilityhub.audit import AuditEvent, AuditSink
from capabilityhub.budget import BudgetLedger, BudgetReservation
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import canonical_json, measure_text
from capabilityhub.models import (
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
    LoadedCapability,
    OperationSpec,
    SectionDescriptor,
)
from capabilityhub.policy import PolicyContext, PolicyOutcome, ReferencePolicy
from capabilityhub.providers.base import CapabilityProvider, ProviderContext
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.search import LexicalCapabilitySearch, SearchResponse


@dataclass(frozen=True, slots=True)
class ServiceContext:
    tenant_id: str
    principal_id: str
    session_id: str
    granted_permissions: frozenset[str] = frozenset()
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
        self._execution_ttl = execution_ref_ttl_seconds
        self._grants: dict[str, _ExecutionGrant] = {}
        self._sequence = 0
        self._lock = RLock()

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
    ) -> SearchResponse:
        try:
            response = self._search_engine.search(
                query,
                scope=context.reference_scope,
                kinds=kinds,
                limit=limit,
                max_output_tokens=max_output_tokens,
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
            metadata={"result_count": len(response.cards), "truncated": response.truncated},
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
            execution_ref = ""
            if manifest.kind is not CapabilityKind.SKILL:
                execution_ref = self._references.issue(
                    revision=manifest.identity.revision,
                    scope=context.reference_scope,
                    purpose="execution",
                    ttl_seconds=self._execution_ttl,
                )
            loaded, payload_bytes = _loaded_response(manifest, sections, operations, execution_ref)
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
                with self._lock:
                    self._grants[execution_ref] = _ExecutionGrant(
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
        try:
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
            decision = self._policy.decide(
                manifest,
                operation,
                PolicyContext(
                    context.granted_permissions,
                    approved=context.approved,
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
            provider = self._providers.get(manifest.provider)
            if provider is None:
                raise CapabilityHubError(
                    code="provider_not_configured",
                    category=ErrorCategory.PROVIDER,
                    safe_message="The capability's named provider is not configured.",
                )
            reservation = budget.reserve({"executions": 1, "portable_tokens": limit})
            result = provider.execute(
                manifest.identity,
                request,
                ProviderContext(
                    context.tenant_id,
                    context.principal_id,
                    context.session_id,
                    context.deadline_ms,
                    limit,
                ),
            )
            result = self._normalize_result(result, manifest, request, provider.name)
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
        except CapabilityHubError as error:
            if reservation is not None and reservation.active:
                reservation.reconcile({"executions": 1})
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

    def _active_manifest(self, revision: str) -> CapabilityManifest:
        manifest = self._registry.revision(revision)
        active_revision = self._registry.activations.get(manifest.identity.coordinate)
        if active_revision != revision:
            raise _reference(
                "stale_revision", "The referenced capability revision is no longer active."
            )
        return manifest

    def _grant(self, execution_ref: str) -> _ExecutionGrant:
        with self._lock:
            grant = self._grants.get(execution_ref)
        if grant is None:
            raise _reference(
                "unknown_execution_grant",
                "The execution reference was not issued by this service instance.",
            )
        return grant

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
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
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
) -> tuple[LoadedCapability, int]:
    portable_tokens = 0
    payload_bytes = 0
    omitted = tuple(section.name for section in manifest.sections if section not in sections)
    for _ in range(4):
        loaded = LoadedCapability(
            revision=manifest.identity.revision,
            sections=sections,
            operations=operations,
            permissions=manifest.permissions,
            portable_tokens=portable_tokens,
            execution_ref=execution_ref,
            omitted_sections=omitted,
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
    ), payload_bytes


def _loaded_dict(loaded: LoadedCapability) -> dict[str, JsonValue]:
    return {
        "execution_ref": loaded.execution_ref,
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
    }


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
