"""Small deny-by-default reference policy for release 0.1."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from capabilityhub.models import CapabilityManifest, OperationSpec, SideEffect


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicyContext:
    granted_permissions: frozenset[str]
    approved: bool = False
    allow_irreversible: bool = False


class ReferencePolicy:
    """Evaluates declared scopes and side effects without executing provider code."""

    def decide(
        self,
        manifest: CapabilityManifest,
        operation: OperationSpec,
        context: PolicyContext,
    ) -> PolicyDecision:
        missing = sorted(set(manifest.permissions) - context.granted_permissions)
        if missing:
            return PolicyDecision(PolicyOutcome.DENY, ("missing_permission", *missing))
        if operation.side_effect is SideEffect.IRREVERSIBLE and not context.allow_irreversible:
            return PolicyDecision(PolicyOutcome.DENY, ("irreversible_disallowed",))
        if operation.requires_approval and not context.approved:
            return PolicyDecision(PolicyOutcome.APPROVAL_REQUIRED, ("operation_requires_approval",))
        if operation.side_effect is SideEffect.REVERSIBLE_WRITE and not context.approved:
            return PolicyDecision(PolicyOutcome.APPROVAL_REQUIRED, ("write_requires_approval",))
        return PolicyDecision(PolicyOutcome.ALLOW, ("policy_allow",))

