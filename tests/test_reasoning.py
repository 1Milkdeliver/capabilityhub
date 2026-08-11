from __future__ import annotations

import pytest

from capabilityhub.models import ReasoningTier, SideEffect
from capabilityhub.reasoning import NoEligibleReasoningTier, ReasoningRouter


def test_router_selects_cheapest_eligible_tier_independent_of_input_order() -> None:
    router = ReasoningRouter(policy_revision="policy-1")
    decision = router.route(
        task_id="task",
        eligible_tiers=(ReasoningTier.HIGH, ReasoningTier.LOW, ReasoningTier.MEDIUM),
    )
    assert decision.tier is ReasoningTier.LOW
    assert decision.reason_codes == ("cheapest_eligible",)
    assert decision.policy_revision == "policy-1"


@pytest.mark.parametrize(
    ("risk", "expected"),
    [
        (SideEffect.NONE, ReasoningTier.LOW),
        (SideEffect.READ, ReasoningTier.LOW),
        (SideEffect.REVERSIBLE_WRITE, ReasoningTier.MEDIUM),
        (SideEffect.IRREVERSIBLE, ReasoningTier.HIGH),
    ],
)
def test_risk_floor_selects_the_cheapest_safe_tier(
    risk: SideEffect, expected: ReasoningTier
) -> None:
    decision = ReasoningRouter().route(task_id=risk.value, risk=risk)
    assert decision.tier is expected
    if expected is not ReasoningTier.LOW:
        assert f"risk_floor:{expected.value}" in decision.reason_codes


def test_no_eligible_tier_fails_closed() -> None:
    router = ReasoningRouter()
    with pytest.raises(NoEligibleReasoningTier) as raised:
        router.route(
            task_id="unsafe",
            risk=SideEffect.IRREVERSIBLE,
            eligible_tiers=(ReasoningTier.LOW, ReasoningTier.MEDIUM),
        )
    assert raised.value.details["minimum_tier"] == "high"


def test_escalation_is_stepwise_and_stops_at_the_task_cap() -> None:
    router = ReasoningRouter(max_escalations=1)
    first = router.route(task_id="task", escalation_reason="low selection margin")
    assert first.tier is ReasoningTier.MEDIUM
    assert first.reason_codes == ("escalated:low_selection_margin",)

    capped = router.route(task_id="task", escalation_reason="typed failure")
    assert capped.tier is ReasoningTier.MEDIUM
    assert capped.escalations_used == 1
    assert capped.reason_codes == ("escalation_cap_reached", "stop")


def test_equivalent_attempt_without_new_evidence_detects_no_progress() -> None:
    router = ReasoningRouter(max_escalations=2, max_equivalent_attempts=1)
    first = router.route(
        task_id="loop",
        attempt_signature={"tool": "search", "query": "same"},
        evidence_signature={"results": []},
    )
    assert "no_progress" not in first.reason_codes

    repeated = router.route(
        task_id="loop",
        escalation_reason="retry failed",
        attempt_signature={"query": "same", "tool": "search"},
        evidence_signature={"results": []},
    )
    assert repeated.tier is ReasoningTier.LOW
    assert repeated.escalations_used == 0
    assert repeated.reason_codes[-2:] == ("no_progress", "stop")


def test_new_evidence_allows_bounded_escalation_and_reset_clears_state() -> None:
    router = ReasoningRouter(max_escalations=1)
    router.route(task_id="task", attempt_signature="attempt", evidence_signature="old")
    changed = router.route(
        task_id="task",
        attempt_signature="attempt",
        evidence_signature="new",
        escalation_reason="new evidence",
    )
    assert changed.tier is ReasoningTier.MEDIUM
    assert changed.reason_codes == ("escalated:new_evidence",)

    router.reset("task")
    snapshot = router.snapshot("task")
    assert snapshot.current_tier is None
    assert snapshot.escalations_used == 0
