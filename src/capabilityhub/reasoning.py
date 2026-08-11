"""Deterministic, budget-aware reasoning tier selection."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from threading import RLock

from .errors import CapabilityHubError, ErrorCategory
from .models import JsonValue, ReasoningDecision, ReasoningTier, SideEffect

_TIER_ORDER = {
    ReasoningTier.LOW: 0,
    ReasoningTier.MEDIUM: 1,
    ReasoningTier.HIGH: 2,
}
_RISK_FLOOR = {
    SideEffect.NONE: ReasoningTier.LOW,
    SideEffect.READ: ReasoningTier.LOW,
    SideEffect.REVERSIBLE_WRITE: ReasoningTier.MEDIUM,
    SideEffect.IRREVERSIBLE: ReasoningTier.HIGH,
}


class NoEligibleReasoningTier(CapabilityHubError):
    """No caller-supported tier satisfies the policy floor."""

    def __init__(self, *, minimum: ReasoningTier) -> None:
        super().__init__(
            code="no_eligible_reasoning_tier",
            category=ErrorCategory.BUDGET,
            safe_message="No eligible reasoning tier satisfies the required safety floor.",
            retryable=False,
            details={"minimum_tier": minimum.value},
        )


@dataclass(frozen=True, slots=True)
class TaskRoutingSnapshot:
    """Privacy-safe state used to explain bounded escalation behavior."""

    task_id: str
    current_tier: ReasoningTier | None
    escalations_used: int
    distinct_attempts: int


@dataclass(slots=True)
class _TaskState:
    current_tier: ReasoningTier | None = None
    escalations_used: int = 0
    attempt_counts: dict[tuple[str, str], int] = field(default_factory=dict)


def _as_tier(value: ReasoningTier | str) -> ReasoningTier:
    return value if isinstance(value, ReasoningTier) else ReasoningTier(value)


def _as_risk(value: SideEffect | str) -> SideEffect:
    return value if isinstance(value, SideEffect) else SideEffect(value)


def _fingerprint(value: JsonValue | str | None) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reason_code(reason: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", reason.strip().lower()).strip("_")
    return normalized or "unspecified"


class ReasoningRouter:
    """Select the cheapest eligible tier and bound retries/escalations per task.

    An equivalent attempt is the same normalized attempt with the same evidence.
    Once its allowed occurrence count is exceeded, the decision includes
    ``no_progress`` and ``stop`` and escalation is deliberately suppressed.
    """

    def __init__(
        self,
        *,
        policy_revision: str = "default",
        max_escalations: int = 2,
        max_equivalent_attempts: int = 1,
    ) -> None:
        if not policy_revision:
            raise ValueError("policy_revision must be non-empty")
        if isinstance(max_escalations, bool) or not isinstance(max_escalations, int):
            raise ValueError("max_escalations must be a non-negative integer")
        if max_escalations < 0:
            raise ValueError("max_escalations must be a non-negative integer")
        if (
            isinstance(max_equivalent_attempts, bool)
            or not isinstance(max_equivalent_attempts, int)
            or max_equivalent_attempts < 1
        ):
            raise ValueError("max_equivalent_attempts must be a positive integer")
        self.policy_revision = policy_revision
        self.max_escalations = max_escalations
        self.max_equivalent_attempts = max_equivalent_attempts
        self._tasks: dict[str, _TaskState] = {}
        self._lock = RLock()

    def route(
        self,
        *,
        task_id: str,
        eligible_tiers: Iterable[ReasoningTier | str] | None = None,
        risk: SideEffect | str = SideEffect.NONE,
        policy_minimum: ReasoningTier | str = ReasoningTier.LOW,
        escalation_reason: str | None = None,
        attempt_signature: JsonValue | str | None = None,
        evidence_signature: JsonValue | str | None = None,
    ) -> ReasoningDecision:
        """Return a deterministic tier decision with machine-readable reason codes."""

        if not task_id:
            raise ValueError("task_id must be non-empty")
        risk_value = _as_risk(risk)
        policy_floor = _as_tier(policy_minimum)
        risk_floor = _RISK_FLOOR[risk_value]
        minimum = max((risk_floor, policy_floor), key=_TIER_ORDER.__getitem__)
        eligible = self._normalize_eligible(eligible_tiers)
        candidates = tuple(tier for tier in eligible if _TIER_ORDER[tier] >= _TIER_ORDER[minimum])
        if not candidates:
            raise NoEligibleReasoningTier(minimum=minimum)

        with self._lock:
            state = self._tasks.setdefault(task_id, _TaskState())
            reasons: list[str] = []
            if _TIER_ORDER[risk_floor] > _TIER_ORDER[ReasoningTier.LOW]:
                reasons.append(f"risk_floor:{risk_floor.value}")
            if _TIER_ORDER[policy_floor] > _TIER_ORDER[ReasoningTier.LOW]:
                reasons.append(f"policy_floor:{policy_floor.value}")

            cheapest = candidates[0]
            current = state.current_tier if state.current_tier in candidates else cheapest
            if _TIER_ORDER[current] < _TIER_ORDER[cheapest]:
                current = cheapest

            no_progress = False
            if attempt_signature is not None:
                attempt_key = _fingerprint(attempt_signature)
                evidence_key = _fingerprint(evidence_signature)
                pair = (attempt_key, evidence_key)
                previous = state.attempt_counts.get(pair, 0)
                state.attempt_counts[pair] = previous + 1
                no_progress = previous >= self.max_equivalent_attempts

            if no_progress:
                reasons.extend(("equivalent_attempt", "no_progress", "stop"))
            elif escalation_reason is not None:
                reason = _reason_code(escalation_reason)
                if state.escalations_used >= self.max_escalations:
                    reasons.extend(("escalation_cap_reached", "stop"))
                else:
                    higher = next(
                        (tier for tier in candidates if _TIER_ORDER[tier] > _TIER_ORDER[current]),
                        None,
                    )
                    if higher is None:
                        reasons.extend(("highest_eligible_tier", "stop"))
                    else:
                        current = higher
                        state.escalations_used += 1
                        reasons.append(f"escalated:{reason}")
            else:
                reasons.append(
                    "cheapest_eligible" if current == cheapest else "retained_escalated_tier"
                )

            state.current_tier = current
            return ReasoningDecision(
                tier=current,
                reason_codes=tuple(reasons),
                escalations_used=state.escalations_used,
                policy_revision=self.policy_revision,
            )

    def snapshot(self, task_id: str) -> TaskRoutingSnapshot:
        with self._lock:
            state = self._tasks.get(task_id, _TaskState())
            return TaskRoutingSnapshot(
                task_id=task_id,
                current_tier=state.current_tier,
                escalations_used=state.escalations_used,
                distinct_attempts=len(state.attempt_counts),
            )

    def reset(self, task_id: str) -> None:
        """Forget completed task state so it cannot affect a future task."""

        with self._lock:
            self._tasks.pop(task_id, None)

    @staticmethod
    def _normalize_eligible(
        values: Iterable[ReasoningTier | str] | None,
    ) -> tuple[ReasoningTier, ...]:
        if values is None:
            return tuple(_TIER_ORDER)
        if isinstance(values, (ReasoningTier, str)):
            values = (values,)
        unique = {_as_tier(value) for value in values}
        return tuple(sorted(unique, key=_TIER_ORDER.__getitem__))
