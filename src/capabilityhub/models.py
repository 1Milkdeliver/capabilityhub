"""Provider-neutral domain models used by CapabilityHub's control core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class CapabilityKind(StrEnum):
    SKILL = "skill"
    MCP = "mcp"
    CLI = "cli"
    API = "api"
    RAG = "rag"


class OperationType(StrEnum):
    EXPAND = "expand"
    EXECUTE = "execute"
    RETRIEVE = "retrieve"


class SideEffect(StrEnum):
    NONE = "none"
    READ = "read"
    REVERSIBLE_WRITE = "reversible_write"
    IRREVERSIBLE = "irreversible"


class ReasoningTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class CapabilityIdentity:
    namespace: str
    name: str
    version: str
    digest: str

    @property
    def coordinate(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def revision(self) -> str:
        return f"{self.coordinate}@{self.version}#{self.digest}"


@dataclass(frozen=True, slots=True)
class SectionDescriptor:
    name: str
    media_type: str
    content: str
    portable_tokens: int
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class OperationSpec:
    name: str
    operation_type: OperationType
    input_schema: Mapping[str, JsonValue] = field(default_factory=dict)
    output_schema: Mapping[str, JsonValue] = field(default_factory=dict)
    side_effect: SideEffect = SideEffect.NONE
    requires_approval: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))
        object.__setattr__(self, "output_schema", MappingProxyType(dict(self.output_schema)))

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        """Rebuild read-only schema mappings across a spawned worker boundary."""

        return (
            type(self),
            (
                self.name,
                self.operation_type,
                dict(self.input_schema),
                dict(self.output_schema),
                self.side_effect,
                self.requires_approval,
            ),
        )


@dataclass(frozen=True, slots=True)
class DependencySpec:
    coordinate: str
    version_constraint: str = "*"
    optional: bool = False


@dataclass(frozen=True, slots=True)
class ConflictSpec:
    conflict_type: str
    value: str


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    identity: CapabilityIdentity
    kind: CapabilityKind
    summary: str
    provider: str
    operations: tuple[OperationSpec, ...]
    sections: tuple[SectionDescriptor, ...] = ()
    permissions: tuple[str, ...] = ()
    dependencies: tuple[DependencySpec, ...] = ()
    conflicts: tuple[ConflictSpec, ...] = ()
    tags: tuple[str, ...] = ()
    trust_tier: str = "unverified"
    source: str = "local"
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        """Rebuild immutable metadata across a spawned worker boundary."""

        return (
            type(self),
            (
                self.identity,
                self.kind,
                self.summary,
                self.provider,
                self.operations,
                self.sections,
                self.permissions,
                self.dependencies,
                self.conflicts,
                self.tags,
                self.trust_tier,
                self.source,
                dict(self.metadata),
            ),
        )

    def section(self, name: str) -> SectionDescriptor | None:
        return next((section for section in self.sections if section.name == name), None)

    def operation(self, name: str) -> OperationSpec | None:
        return next((operation for operation in self.operations if operation.name == name), None)


@dataclass(frozen=True, slots=True)
class SearchCard:
    capability_ref: str
    revision: str
    kind: CapabilityKind
    summary: str
    operations: tuple[str, ...]
    risk: SideEffect
    trust_tier: str
    estimated_load_tokens: int
    match_reason: tuple[str, ...]


class OmissionKind(StrEnum):
    SECTION = "section"
    OPERATION = "operation"


@dataclass(frozen=True, slots=True)
class CapabilityNotice:
    kind: str
    code: str
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class RehydrationHandle:
    kind: OmissionKind
    selector_digest: str
    reference: str
    expires_at: int

    def __post_init__(self) -> None:
        if not isinstance(self.selector_digest, str):
            raise ValueError("rehydration handle fields are invalid")
        digest = self.selector_digest.removeprefix("sha256:")
        if (
            not isinstance(self.kind, OmissionKind)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(self.reference, str)
            or not self.reference
            or not isinstance(self.expires_at, int)
            or isinstance(self.expires_at, bool)
            or self.expires_at <= 0
        ):
            raise ValueError("rehydration handle fields are invalid")


@dataclass(frozen=True, slots=True)
class LoadedCapability:
    revision: str
    sections: tuple[SectionDescriptor, ...]
    operations: tuple[OperationSpec, ...]
    permissions: tuple[str, ...]
    portable_tokens: int
    execution_ref: str
    omitted_sections: tuple[str, ...] = ()
    omitted_operations: tuple[str, ...] = ()
    notices: tuple[CapabilityNotice, ...] = ()
    rehydration_handles: tuple[RehydrationHandle, ...] = ()
    omitted_section_count: int = 0
    omitted_operation_count: int = 0
    omitted_notice_count: int = 0
    unhandled_omission_count: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    execution_ref: str
    operation: str
    arguments: Mapping[str, JsonValue]
    task_id: str
    approval_ref: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        """Rebuild immutable arguments across a spawned worker boundary."""

        return (
            type(self),
            (
                self.execution_ref,
                self.operation,
                dict(self.arguments),
                self.task_id,
                self.approval_ref,
                self.idempotency_key,
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    capability_revision: str
    operation: str
    output: JsonValue
    provider: str
    portable_tokens: int
    audit_id: str


@dataclass(frozen=True, slots=True)
class ReasoningDecision:
    tier: ReasoningTier
    reason_codes: tuple[str, ...]
    escalations_used: int
    policy_revision: str


def maximum_side_effect(operations: tuple[OperationSpec, ...]) -> SideEffect:
    order = {
        SideEffect.NONE: 0,
        SideEffect.READ: 1,
        SideEffect.REVERSIBLE_WRITE: 2,
        SideEffect.IRREVERSIBLE: 3,
    }
    if not operations:
        return SideEffect.NONE
    return max((operation.side_effect for operation in operations), key=order.__getitem__)
