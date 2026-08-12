from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import ReasoningTier, SideEffect
from capabilityhub.orchestration import (
    AppliedReasoningRouter,
    ReasoningConstraints,
    ReasoningEndpoint,
    ReasoningOrchestrator,
    ReasoningWorkload,
)
from capabilityhub.reasoning import NoEligibleReasoningTier, ReasoningRouter
from capabilityhub.reasoning_store import SQLiteReasoningStore


def _orchestrator(*, limit: int = 5_000) -> ReasoningOrchestrator:
    return ReasoningOrchestrator(
        router=ReasoningRouter(policy_revision="routing-7", max_escalations=2),
        budget=BudgetLedger("task:test", {"reasoning_tokens": limit}),
    )


def test_recommendation_is_budget_aware_and_queryable() -> None:
    orchestrator = _orchestrator(limit=1_500)

    recommendation = orchestrator.recommend(
        task_id="task-1",
        risk=SideEffect.REVERSIBLE_WRITE,
    )

    assert recommendation.tier is ReasoningTier.MEDIUM
    assert recommendation.estimated_tokens == 1_024
    assert recommendation.budget_headroom == 476
    assert orchestrator.state("task-1") == {
        "budget": {
            "counter": "reasoning_tokens",
            "limit": 1_500,
            "remaining": 1_500,
            "reserved": 0,
            "scope": "task:test",
            "used": 0,
        },
        "current_tier": "medium",
        "distinct_attempts": 0,
        "escalations_used": 0,
        "last_recommendation": {
            "budget_headroom": 476,
            "budget_remaining": 1_500,
            "estimated_tokens": 1_024,
            "reason_codes": ["risk_floor:medium", "cheapest_eligible"],
            "should_stop": False,
            "tier": "medium",
        },
        "policy_revision": "routing-7",
        "recommendation_count": 1,
        "task_id": "task-1",
    }


def test_live_budget_usage_removes_unaffordable_tiers() -> None:
    orchestrator = _orchestrator(limit=1_500)
    orchestrator.budget.spend({"reasoning_tokens": 1_000})

    low = orchestrator.recommend(task_id="budget-low")
    assert low.tier is ReasoningTier.LOW
    assert low.budget_remaining == 500

    with pytest.raises(NoEligibleReasoningTier):
        orchestrator.recommend(
            task_id="budget-write",
            risk=SideEffect.REVERSIBLE_WRITE,
        )


def test_equivalent_attempt_stops_without_escalating_or_looping() -> None:
    orchestrator = _orchestrator()
    first = orchestrator.recommend(
        task_id="loop",
        attempt_signature={"tool": "search", "query": "same"},
        evidence_signature={"results": []},
    )
    repeated = orchestrator.recommend(
        task_id="loop",
        escalation_reason="retry failed",
        attempt_signature={"query": "same", "tool": "search"},
        evidence_signature={"results": []},
    )

    assert not first.should_stop
    assert repeated.should_stop
    assert repeated.tier is ReasoningTier.LOW
    assert repeated.escalations_used == 0
    assert repeated.reason_codes[-2:] == ("no_progress", "stop")
    state = orchestrator.state("loop")
    assert state["recommendation_count"] == 2
    assert state["distinct_attempts"] == 1


def test_reset_clears_router_and_observable_task_state() -> None:
    orchestrator = _orchestrator()
    orchestrator.recommend(task_id="done", attempt_signature="attempt")

    orchestrator.reset("done")

    state = orchestrator.state("done")
    assert state["current_tier"] is None
    assert state["distinct_attempts"] == 0
    assert state["recommendation_count"] == 0
    assert state["last_recommendation"] is None


def test_sqlite_state_restores_tier_and_escalation_after_restart(tmp_path) -> None:
    path = tmp_path / "reasoning.sqlite3"
    first = ReasoningOrchestrator(
        router=ReasoningRouter(policy_revision="policy", max_escalations=2),
        budget=BudgetLedger("task", {"reasoning_tokens": 5_000}),
        store=SQLiteReasoningStore(path),
    )
    first.recommend(task_id="restart")
    escalated = first.recommend(task_id="restart", escalation_reason="new evidence")
    assert escalated.tier is ReasoningTier.MEDIUM

    restarted = ReasoningOrchestrator(
        router=ReasoningRouter(policy_revision="policy", max_escalations=2),
        budget=BudgetLedger("task", {"reasoning_tokens": 5_000}),
        store=SQLiteReasoningStore(path),
    )
    retained = restarted.recommend(task_id="restart")

    assert retained.tier is ReasoningTier.MEDIUM
    assert retained.escalations_used == 1
    assert retained.reason_codes == ("retained_escalated_tier",)
    assert restarted.state("restart")["recommendation_count"] == 3


def test_sqlite_attempt_digests_stop_equivalent_retry_after_restart(tmp_path) -> None:
    path = tmp_path / "reasoning.sqlite3"
    first = ReasoningOrchestrator(
        router=ReasoningRouter(max_equivalent_attempts=1),
        budget=BudgetLedger("task", {"reasoning_tokens": 5_000}),
        store=SQLiteReasoningStore(path),
    )
    first.recommend(
        task_id="loop",
        attempt_signature={"query": "raw secret prompt"},
        evidence_signature={"result": "raw private evidence"},
    )

    restarted = ReasoningOrchestrator(
        router=ReasoningRouter(max_equivalent_attempts=1),
        budget=BudgetLedger("task", {"reasoning_tokens": 5_000}),
        store=SQLiteReasoningStore(path),
    )
    stopped = restarted.recommend(
        task_id="loop",
        escalation_reason="retry",
        attempt_signature={"query": "raw secret prompt"},
        evidence_signature={"result": "raw private evidence"},
    )

    assert stopped.should_stop
    assert stopped.escalations_used == 0
    assert stopped.reason_codes[-2:] == ("no_progress", "stop")
    database = path.read_bytes()
    assert b"raw secret prompt" not in database
    assert b"raw private evidence" not in database


def test_sqlite_attempt_update_is_atomic_across_orchestrators(tmp_path) -> None:
    path = tmp_path / "reasoning.sqlite3"

    def recommend(_: int) -> bool:
        orchestrator = ReasoningOrchestrator(
            router=ReasoningRouter(max_equivalent_attempts=1),
            budget=BudgetLedger("task", {"reasoning_tokens": 5_000}),
            store=SQLiteReasoningStore(path),
        )
        return orchestrator.recommend(
            task_id="concurrent",
            attempt_signature={"operation": "same"},
            evidence_signature={"result": "same"},
        ).should_stop

    with ThreadPoolExecutor(max_workers=8) as pool:
        stopped = list(pool.map(recommend, range(20)))

    assert stopped.count(False) == 1
    assert stopped.count(True) == 19
    state = SQLiteReasoningStore(path).read("concurrent")
    assert state.recommendation_count == 20
    assert tuple(state.attempt_counts.values()) == (20,)


def test_applied_router_enforces_dependency_floor_and_endpoint_boundaries() -> None:
    budget = BudgetLedger("task", {"reasoning_tokens": 2_000})
    applied = AppliedReasoningRouter(
        budget_provider=lambda _task: budget,
        endpoints=(
            ReasoningEndpoint("low-fast", ReasoningTier.LOW, 1, 10),
            ReasoningEndpoint("medium-slow", ReasoningTier.MEDIUM, 3, 30),
            ReasoningEndpoint(
                "medium-fast", ReasoningTier.MEDIUM, 2, 20, "model-medium", "medium"
            ),
            ReasoningEndpoint("high", ReasoningTier.HIGH, 9, 90),
        ),
    )

    decision = applied.decide(
        task_id="dependencies",
        operation="capability.execute",
        workload=ReasoningWorkload(dependency_count=3),
        constraints=ReasoningConstraints(
            maximum_tier=ReasoningTier.MEDIUM,
            maximum_cost_units=2,
            maximum_latency_ms=25,
        ),
    )

    assert decision.tier is ReasoningTier.MEDIUM
    assert decision.endpoint == "medium-fast"
    assert "dependency_floor:medium" in decision.reason_codes
    policy = applied.request_policy(
        decision,
        constraints=ReasoningConstraints(
            maximum_tier=ReasoningTier.MEDIUM,
            eligible_endpoints=frozenset({"medium-fast"}),
            maximum_cost_units=2,
            maximum_latency_ms=25,
        ),
    )
    assert (policy.model, policy.effort) == ("model-medium", "medium")
    with pytest.raises(CapabilityHubError) as caught:
        applied.request_policy(
            replace(decision, endpoint="high"),
            constraints=ReasoningConstraints(maximum_tier=ReasoningTier.MEDIUM),
        )
    assert caught.value.code == "reasoning_policy_mismatch"
    assert budget.snapshot().used["reasoning_tokens"] == 0


def test_applied_router_fails_closed_when_risk_exceeds_explicit_ceiling() -> None:
    applied = AppliedReasoningRouter(
        budget_provider=lambda _task: BudgetLedger(
            "task", {"reasoning_tokens": 10_000}
        ),
        endpoints=(
            ReasoningEndpoint("low", ReasoningTier.LOW),
            ReasoningEndpoint("medium", ReasoningTier.MEDIUM),
            ReasoningEndpoint("high", ReasoningTier.HIGH),
        ),
    )

    with pytest.raises(NoEligibleReasoningTier):
        applied.decide(
            task_id="irreversible",
            operation="capability.execute",
            workload=ReasoningWorkload(risk=SideEffect.IRREVERSIBLE),
            constraints=ReasoningConstraints(maximum_tier=ReasoningTier.MEDIUM),
        )


def test_applied_router_escalates_once_then_stops_equivalent_failure_loop() -> None:
    applied = AppliedReasoningRouter(
        budget_provider=lambda _task: BudgetLedger(
            "task", {"reasoning_tokens": 10_000}
        ),
        endpoints=tuple(
            ReasoningEndpoint(tier.value, tier) for tier in ReasoningTier
        ),
    )
    first = applied.decide(
        task_id="retry",
        operation="capability.execute",
        workload=ReasoningWorkload(),
    )
    assert first.tier is ReasoningTier.LOW
    applied.record_result(
        task_id="retry",
        operation="capability.execute",
        error_code="typed_failure",
    )
    escalated = applied.decide(
        task_id="retry",
        operation="capability.execute",
        workload=ReasoningWorkload(),
    )
    assert escalated.tier is ReasoningTier.MEDIUM
    applied.record_result(
        task_id="retry",
        operation="capability.execute",
        error_code="typed_failure",
    )
    with pytest.raises(CapabilityHubError) as caught:
        applied.decide(
            task_id="retry",
            operation="capability.execute",
            workload=ReasoningWorkload(),
        )
    assert caught.value.code == "reasoning_no_progress"
