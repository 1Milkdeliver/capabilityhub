"""Local, queryable orchestration for budget-aware reasoning advice."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType

from .budget import BudgetLedger
from .errors import CapabilityHubError, ErrorCategory
from .models import JsonValue, ReasoningTier, SideEffect
from .reasoning import ReasoningRouter
from .reasoning_store import SQLiteReasoningStore, StoredReasoningState

DEFAULT_TIER_TOKEN_ESTIMATES: Mapping[ReasoningTier, int] = MappingProxyType(
    {
        ReasoningTier.LOW: 256,
        ReasoningTier.MEDIUM: 1_024,
        ReasoningTier.HIGH: 4_096,
    }
)


@dataclass(frozen=True, slots=True)
class ReasoningRecommendation:
    """One observable advisory decision; no model execution is implied."""

    task_id: str
    tier: ReasoningTier
    reason_codes: tuple[str, ...]
    estimated_tokens: int
    budget_remaining: int | None
    budget_headroom: int | None
    escalations_used: int
    policy_revision: str
    should_stop: bool


@dataclass(frozen=True, slots=True)
class ReasoningEndpoint:
    """A configured endpoint eligibility fact; no invocation is performed here."""

    name: str
    tier: ReasoningTier
    cost_units: int | None = None
    latency_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 128:
            raise ValueError("reasoning endpoint name is invalid")
        for value, label in ((self.cost_units, "cost_units"), (self.latency_ms, "latency_ms")):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"reasoning endpoint {label} must be non-negative")


@dataclass(frozen=True, slots=True)
class ReasoningConstraints:
    """Caller and transport ceilings applied before deterministic tier routing."""

    maximum_tier: ReasoningTier = ReasoningTier.HIGH
    eligible_endpoints: frozenset[str] | None = None
    maximum_cost_units: int | None = None
    maximum_latency_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.maximum_tier, ReasoningTier):
            raise TypeError("maximum_tier must be a reasoning tier")
        for value, label in (
            (self.maximum_cost_units, "maximum_cost_units"),
            (self.maximum_latency_ms, "maximum_latency_ms"),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{label} must be non-negative")


@dataclass(frozen=True, slots=True)
class ReasoningWorkload:
    risk: SideEffect = SideEffect.NONE
    dependency_count: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.dependency_count, bool)
            or not isinstance(self.dependency_count, int)
            or self.dependency_count < 0
        ):
            raise ValueError("dependency_count must be non-negative")


@dataclass(frozen=True, slots=True)
class AppliedReasoningDecision:
    task_id: str
    operation: str
    tier: ReasoningTier
    endpoint: str
    reason_codes: tuple[str, ...]
    estimated_tokens: int
    budget_remaining: int | None
    should_stop: bool

    def safe_summary(self) -> dict[str, JsonValue]:
        return {
            "budget_remaining": self.budget_remaining,
            "endpoint": self.endpoint,
            "estimated_tokens": self.estimated_tokens,
            "operation": self.operation,
            "reason_codes": list(self.reason_codes),
            "should_stop": self.should_stop,
            "tier": self.tier.value,
        }


BudgetProvider = Callable[[str], BudgetLedger]


class AppliedReasoningRouter:
    """Apply task-level tier advice as a real endpoint admission decision."""

    def __init__(
        self,
        *,
        budget_provider: BudgetProvider,
        endpoints: Iterable[ReasoningEndpoint],
        router: ReasoningRouter | None = None,
    ) -> None:
        selected = tuple(endpoints)
        if not selected or len({endpoint.name for endpoint in selected}) != len(selected):
            raise ValueError("reasoning endpoints must be non-empty and uniquely named")
        self._budget_provider = budget_provider
        self._endpoints = selected
        self._router = router or ReasoningRouter(policy_revision="adapter-reasoning-v1")
        self._failures: dict[str, tuple[str, str]] = {}
        self._last: dict[str, AppliedReasoningDecision] = {}
        self._outcomes: dict[str, str | None] = {}
        self._lock = RLock()

    def decide(
        self,
        *,
        task_id: str,
        operation: str,
        workload: ReasoningWorkload,
        constraints: ReasoningConstraints | None = None,
    ) -> AppliedReasoningDecision:
        constraints = constraints or ReasoningConstraints()
        endpoints = self._eligible_endpoints(constraints)
        tiers = tuple(dict.fromkeys(endpoint.tier for endpoint in endpoints))
        minimum = (
            ReasoningTier.MEDIUM
            if workload.dependency_count >= 3
            and workload.risk in {SideEffect.NONE, SideEffect.READ}
            else ReasoningTier.LOW
        )
        with self._lock:
            failure = self._failures.get(task_id)
        orchestrator = ReasoningOrchestrator(
            router=self._router,
            budget=self._budget_provider(task_id),
        )
        recommendation = orchestrator.recommend(
            task_id=task_id,
            eligible_tiers=tiers,
            risk=workload.risk,
            policy_minimum=minimum,
            escalation_reason=("previous_typed_failure" if failure is not None else None),
            attempt_signature=(failure[0] if failure is not None else None),
            evidence_signature=(failure[1] if failure is not None else None),
        )
        endpoint = min(
            (item for item in endpoints if item.tier is recommendation.tier),
            key=lambda item: (
                item.cost_units is None,
                item.cost_units or 0,
                item.latency_ms is None,
                item.latency_ms or 0,
                item.name,
            ),
        )
        reasons = list(recommendation.reason_codes)
        if workload.dependency_count >= 3:
            reasons.append("dependency_floor:medium")
        reasons.append("endpoint_eligible")
        decision = AppliedReasoningDecision(
            task_id,
            operation,
            recommendation.tier,
            endpoint.name,
            tuple(dict.fromkeys(reasons)),
            recommendation.estimated_tokens,
            recommendation.budget_remaining,
            recommendation.should_stop,
        )
        with self._lock:
            self._last[task_id] = decision
        if decision.should_stop:
            raise CapabilityHubError(
                code="reasoning_no_progress",
                category=ErrorCategory.BUDGET,
                safe_message="Equivalent failed attempts reached the reasoning stop condition.",
            )
        return decision

    def record_result(
        self,
        *,
        task_id: str,
        operation: str,
        error_code: str | None,
    ) -> None:
        with self._lock:
            if error_code is None:
                self._failures.pop(task_id, None)
            else:
                self._failures[task_id] = (operation, error_code)
            self._outcomes[task_id] = error_code

    def state(self, task_id: str) -> dict[str, JsonValue] | None:
        with self._lock:
            decision = self._last.get(task_id)
            outcome_known = task_id in self._outcomes
            error_code = self._outcomes.get(task_id)
        if decision is None:
            return None
        summary = decision.safe_summary()
        summary["application"] = {
            "applied": outcome_known,
            "error_code": error_code,
            "outcome": (
                "pending" if not outcome_known else "success" if error_code is None else "error"
            ),
        }
        return summary

    def _eligible_endpoints(
        self, constraints: ReasoningConstraints
    ) -> tuple[ReasoningEndpoint, ...]:
        ceiling = _TIER_POSITION[constraints.maximum_tier]
        selected = tuple(
            endpoint
            for endpoint in self._endpoints
            if _TIER_POSITION[endpoint.tier] <= ceiling
            and (
                constraints.eligible_endpoints is None
                or endpoint.name in constraints.eligible_endpoints
            )
            and (
                constraints.maximum_cost_units is None
                or (
                    endpoint.cost_units is not None
                    and endpoint.cost_units <= constraints.maximum_cost_units
                )
            )
            and (
                constraints.maximum_latency_ms is None
                or (
                    endpoint.latency_ms is not None
                    and endpoint.latency_ms <= constraints.maximum_latency_ms
                )
            )
        )
        if not selected:
            raise CapabilityHubError(
                code="no_eligible_reasoning_endpoint",
                category=ErrorCategory.BUDGET,
                safe_message="No reasoning endpoint satisfies the caller constraints.",
            )
        return selected


class ReasoningOrchestrator:
    """Join the reasoning router to a live budget ledger and compact state view.

    Recommendations do not spend or reserve budget. The caller remains responsible
    for reserve/execute/reconcile around the eventual model call. This class only
    prevents advice for tiers whose estimate cannot fit the currently available
    budget.
    """

    def __init__(
        self,
        *,
        router: ReasoningRouter,
        budget: BudgetLedger,
        budget_counter: str = "reasoning_tokens",
        tier_token_estimates: Mapping[ReasoningTier | str, int] | None = None,
        store: SQLiteReasoningStore | None = None,
    ) -> None:
        if not budget_counter:
            raise ValueError("budget_counter must be non-empty")
        estimates = self._normalize_estimates(tier_token_estimates)
        if not (
            estimates[ReasoningTier.LOW]
            <= estimates[ReasoningTier.MEDIUM]
            <= estimates[ReasoningTier.HIGH]
        ):
            raise ValueError("tier token estimates must be non-decreasing")
        self.router = router
        self.budget = budget
        self.budget_counter = budget_counter
        self.tier_token_estimates = MappingProxyType(estimates)
        self.store = store
        self._last: dict[str, ReasoningRecommendation] = {}
        self._recommendation_counts: dict[str, int] = {}
        self._lock = RLock()

    def recommend(
        self,
        *,
        task_id: str,
        eligible_tiers: Iterable[ReasoningTier | str] | None = None,
        risk: SideEffect | str = SideEffect.NONE,
        policy_minimum: ReasoningTier | str = ReasoningTier.LOW,
        escalation_reason: str | None = None,
        attempt_signature: JsonValue | str | None = None,
        evidence_signature: JsonValue | str | None = None,
    ) -> ReasoningRecommendation:
        """Return the cheapest safe tier that fits the ledger's current headroom."""

        snapshot = self.budget.snapshot()
        remaining = snapshot.remaining.get(self.budget_counter)
        requested = self._normalize_eligible(eligible_tiers)
        affordable = tuple(
            tier
            for tier in requested
            if remaining is None or self.tier_token_estimates[tier] <= remaining
        )
        if self.store is not None:
            return self._recommend_persisted(
                task_id=task_id,
                affordable=affordable,
                remaining=remaining,
                risk=risk,
                policy_minimum=policy_minimum,
                escalation_reason=escalation_reason,
                attempt_signature=attempt_signature,
                evidence_signature=evidence_signature,
            )
        decision = self.router.route(
            task_id=task_id,
            eligible_tiers=affordable,
            risk=risk,
            policy_minimum=policy_minimum,
            escalation_reason=escalation_reason,
            attempt_signature=attempt_signature,
            evidence_signature=evidence_signature,
        )
        estimate = self.tier_token_estimates[decision.tier]
        recommendation = ReasoningRecommendation(
            task_id=task_id,
            tier=decision.tier,
            reason_codes=decision.reason_codes,
            estimated_tokens=estimate,
            budget_remaining=remaining,
            budget_headroom=None if remaining is None else remaining - estimate,
            escalations_used=decision.escalations_used,
            policy_revision=decision.policy_revision,
            should_stop="stop" in decision.reason_codes,
        )
        with self._lock:
            self._last[task_id] = recommendation
            self._recommendation_counts[task_id] = self._recommendation_counts.get(task_id, 0) + 1
        return recommendation

    def state(self, task_id: str) -> dict[str, JsonValue]:
        """Return a compact, privacy-safe view of current local orchestration state."""

        if not task_id:
            raise ValueError("task_id must be non-empty")
        budget = self.budget.snapshot()
        if self.store is None:
            routing = self.router.snapshot(task_id)
            with self._lock:
                last = self._last.get(task_id)
                count = self._recommendation_counts.get(task_id, 0)
            current_tier = routing.current_tier
            escalations_used = routing.escalations_used
            distinct_attempts = routing.distinct_attempts
            last_payload = self._recommendation_payload(last) if last is not None else None
        else:
            persisted = self.store.read(task_id)
            current_tier = persisted.tier
            escalations_used = persisted.escalations_used
            distinct_attempts = len(persisted.attempt_counts)
            count = persisted.recommendation_count
            last_payload = (
                dict(persisted.last_recommendation)
                if persisted.last_recommendation is not None
                else None
            )
        limit = budget.limits.get(self.budget_counter)
        remaining = budget.remaining.get(self.budget_counter)
        return {
            "budget": {
                "counter": self.budget_counter,
                "limit": limit,
                "remaining": remaining,
                "reserved": budget.reserved.get(self.budget_counter, 0),
                "scope": budget.scope,
                "used": budget.used.get(self.budget_counter, 0),
            },
            "current_tier": (current_tier.value if current_tier is not None else None),
            "distinct_attempts": distinct_attempts,
            "escalations_used": escalations_used,
            "last_recommendation": last_payload,
            "policy_revision": self.router.policy_revision,
            "recommendation_count": count,
            "task_id": task_id,
        }

    def reset(self, task_id: str) -> None:
        """Clear task-local router and observable recommendation state."""

        self.router.reset(task_id)
        if self.store is not None:
            self.store.reset(task_id)
        with self._lock:
            self._last.pop(task_id, None)
            self._recommendation_counts.pop(task_id, None)

    def _recommend_persisted(
        self,
        *,
        task_id: str,
        affordable: tuple[ReasoningTier, ...],
        remaining: int | None,
        risk: SideEffect | str,
        policy_minimum: ReasoningTier | str,
        escalation_reason: str | None,
        attempt_signature: JsonValue | str | None,
        evidence_signature: JsonValue | str | None,
    ) -> ReasoningRecommendation:
        assert self.store is not None
        baseline = ReasoningRouter(
            policy_revision=self.router.policy_revision,
            max_escalations=0,
            max_equivalent_attempts=self.router.max_equivalent_attempts,
        ).route(
            task_id=task_id,
            eligible_tiers=affordable,
            risk=risk,
            policy_minimum=policy_minimum,
        )
        safe_tiers = tuple(
            tier for tier in affordable if _TIER_POSITION[tier] >= _TIER_POSITION[baseline.tier]
        )
        attempt_pair = (
            (_signature_digest(attempt_signature), _signature_digest(evidence_signature))
            if attempt_signature is not None
            else None
        )

        def update(
            state: StoredReasoningState,
        ) -> tuple[StoredReasoningState, ReasoningRecommendation]:
            counts = dict(state.attempt_counts)
            no_progress = False
            if attempt_pair is not None:
                previous = counts.get(attempt_pair, 0)
                counts[attempt_pair] = previous + 1
                no_progress = previous >= self.router.max_equivalent_attempts

            current = state.tier if state.tier in safe_tiers else baseline.tier
            reasons = [code for code in baseline.reason_codes if code != "cheapest_eligible"]
            escalations = state.escalations_used
            if no_progress:
                reasons.extend(("equivalent_attempt", "no_progress", "stop"))
            elif escalation_reason is not None:
                if escalations >= self.router.max_escalations:
                    reasons.extend(("escalation_cap_reached", "stop"))
                else:
                    higher = next(
                        (
                            tier
                            for tier in safe_tiers
                            if _TIER_POSITION[tier] > _TIER_POSITION[current]
                        ),
                        None,
                    )
                    if higher is None:
                        reasons.extend(("highest_eligible_tier", "stop"))
                    else:
                        current = higher
                        escalations += 1
                        reasons.append(f"escalated:{_reason_code(escalation_reason)}")
            else:
                reasons.append(
                    "cheapest_eligible" if current == baseline.tier else "retained_escalated_tier"
                )

            estimate = self.tier_token_estimates[current]
            recommendation = ReasoningRecommendation(
                task_id=task_id,
                tier=current,
                reason_codes=tuple(reasons),
                estimated_tokens=estimate,
                budget_remaining=remaining,
                budget_headroom=None if remaining is None else remaining - estimate,
                escalations_used=escalations,
                policy_revision=self.router.policy_revision,
                should_stop="stop" in reasons,
            )
            replacement = StoredReasoningState(
                task_id=task_id,
                tier=current,
                escalations_used=escalations,
                recommendation_count=state.recommendation_count + 1,
                attempt_counts=counts,
                last_recommendation=self._recommendation_payload(recommendation),
            )
            return replacement, recommendation

        return self.store.transact(task_id, update)

    @staticmethod
    def _recommendation_payload(
        recommendation: ReasoningRecommendation,
    ) -> dict[str, JsonValue]:
        return {
            "budget_headroom": recommendation.budget_headroom,
            "budget_remaining": recommendation.budget_remaining,
            "estimated_tokens": recommendation.estimated_tokens,
            "reason_codes": list(recommendation.reason_codes),
            "should_stop": recommendation.should_stop,
            "tier": recommendation.tier.value,
        }

    @staticmethod
    def _normalize_estimates(
        values: Mapping[ReasoningTier | str, int] | None,
    ) -> dict[ReasoningTier, int]:
        raw = DEFAULT_TIER_TOKEN_ESTIMATES if values is None else values
        normalized: dict[ReasoningTier, int] = {}
        for key, value in raw.items():
            tier = key if isinstance(key, ReasoningTier) else ReasoningTier(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("tier token estimates must be positive integers")
            normalized[tier] = value
        if set(normalized) != set(ReasoningTier):
            raise ValueError("tier token estimates must define low, medium, and high")
        return normalized

    @staticmethod
    def _normalize_eligible(
        values: Iterable[ReasoningTier | str] | None,
    ) -> tuple[ReasoningTier, ...]:
        if values is None:
            return tuple(ReasoningTier)
        if isinstance(values, (ReasoningTier, str)):
            values = (values,)
        requested = {
            value if isinstance(value, ReasoningTier) else ReasoningTier(value) for value in values
        }
        return tuple(tier for tier in ReasoningTier if tier in requested)


_TIER_POSITION = {
    ReasoningTier.LOW: 0,
    ReasoningTier.MEDIUM: 1,
    ReasoningTier.HIGH: 2,
}


def _signature_digest(value: JsonValue | str | None) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reason_code(reason: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", reason.strip().lower()).strip("_")
    return normalized or "unspecified"
