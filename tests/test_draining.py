from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from capabilityhub.draining import (
    DrainController,
    DrainOutcome,
    ForceRetirePolicy,
    LifecycleError,
    LifecycleState,
)


def test_revision_without_pins_transitions_accepting_draining_retired() -> None:
    controller = DrainController()
    initial = controller.register("skill/example", "revision-1")

    retired = controller.begin_drain("skill/example", "revision-1", reason="update")

    assert initial.state is LifecycleState.ACCEPTING
    assert retired.state is LifecycleState.RETIRED
    assert [event.action for event in controller.events] == [
        "registered",
        "revision_draining",
        "revision_retired",
    ]


def test_in_flight_pin_blocks_retirement_and_new_admission_until_release() -> None:
    controller = DrainController()
    controller.register("skill/example", "revision-1")
    pin = controller.admit("skill/example", "revision-1", "execution-1")

    draining = controller.begin_drain("skill/example", "revision-1")
    with pytest.raises(LifecycleError) as caught:
        controller.admit("skill/example", "revision-1", "execution-2")

    assert draining.state is LifecycleState.DRAINING
    assert draining.in_flight == 1
    assert caught.value.code == "lifecycle_not_accepting"
    assert controller.release(pin.pin_id) is True
    assert controller.snapshot("skill/example", "revision-1").state is LifecycleState.RETIRED
    assert controller.release(pin.pin_id) is False


def test_coordinate_drain_preserves_old_revision_pins_and_blocks_new_revisions() -> None:
    controller = DrainController()
    controller.register("skill/example", "revision-old")
    controller.register("skill/example", "revision-new")
    old_pin = controller.admit("skill/example", "revision-old", "old-execution")
    new_pin = controller.admit("skill/example", "revision-new", "new-execution")

    snapshot = controller.begin_drain("skill/example", reason="coordinate_update")

    assert snapshot.state is LifecycleState.DRAINING
    assert snapshot.in_flight == 2
    assert controller.snapshot("skill/example", "revision-old").in_flight == 1
    with pytest.raises(LifecycleError) as caught:
        controller.register("skill/example", "revision-later")
    assert caught.value.code == "lifecycle_coordinate_not_accepting"

    controller.release(new_pin.pin_id)
    assert controller.snapshot("skill/example").state is LifecycleState.DRAINING
    assert controller.snapshot("skill/example", "revision-old").in_flight == 1
    controller.release(old_pin.pin_id)
    assert controller.snapshot("skill/example").state is LifecycleState.RETIRED


def test_deadline_requests_only_declared_cancellable_pins_without_releasing_any() -> None:
    controller = DrainController(clock=lambda: 10)
    controller.register("api/example", "revision-1")
    cancellable = controller.admit("api/example", "revision-1", "cancel-me", cancellable=True)
    blocked = controller.admit("api/example", "revision-1", "do-not-cancel", cancellable=False)
    controller.begin_drain("api/example", "revision-1")

    waiting = controller.advance("api/example", "revision-1", deadline=11)
    requested = controller.advance("api/example", "revision-1", deadline=10)
    repeated = controller.advance("api/example", "revision-1", deadline=10)

    assert waiting.outcome is DrainOutcome.WAITING
    assert requested.outcome is DrainOutcome.CANCEL_REQUESTED
    assert requested.cancellation_requests == (cancellable.pin_id,)
    assert requested.snapshot.in_flight == 2
    assert requested.snapshot.cancellation_requested == 1
    assert repeated.outcome is DrainOutcome.BLOCKED
    assert repeated.cancellation_requests == ()
    assert controller.release(cancellable.pin_id) is True
    assert controller.release(blocked.pin_id) is True
    assert controller.snapshot("api/example", "revision-1").state is LifecycleState.RETIRED


def test_force_retire_requires_explicit_policy_records_reason_and_keeps_pin() -> None:
    controller = DrainController()
    controller.register("cli/example", "revision-1")
    pin = controller.admit("cli/example", "revision-1", "execution-1")
    controller.begin_drain("cli/example", "revision-1")

    with pytest.raises(LifecycleError) as denied:
        controller.force_retire(
            "cli/example",
            "revision-1",
            policy=ForceRetirePolicy.DENY,
            reason="operator_override",
        )
    retired = controller.force_retire(
        "cli/example",
        "revision-1",
        policy=ForceRetirePolicy.ALLOW_IN_FLIGHT,
        reason="operator_override",
    )

    assert denied.value.code == "lifecycle_force_retire_denied"
    assert retired.state is LifecycleState.RETIRED
    assert retired.in_flight == 1
    assert controller.release(pin.pin_id) is True
    forced = [event for event in controller.events if event.action == "revision_force_retired"]
    assert len(forced) == 1
    assert forced[0].reason == "operator_override"
    assert forced[0].in_flight == 1


def test_force_retire_is_rejected_before_drain() -> None:
    controller = DrainController()
    controller.register("mcp/example", "revision-1")

    with pytest.raises(LifecycleError) as caught:
        controller.force_retire(
            "mcp/example",
            "revision-1",
            policy=ForceRetirePolicy.ALLOW_IN_FLIGHT,
            reason="operator_override",
        )

    assert caught.value.code == "lifecycle_not_draining"
    assert controller.snapshot("mcp/example", "revision-1").state is LifecycleState.ACCEPTING


def test_state_pins_and_events_are_bounded_with_redacted_events() -> None:
    controller = DrainController(
        max_coordinates=1,
        max_revisions=1,
        max_in_flight=1,
        event_limit=2,
    )
    controller.register("SECRET-COORDINATE", "SECRET-REVISION")
    controller.admit("SECRET-COORDINATE", "SECRET-REVISION", "SECRET-PIN")
    with pytest.raises(LifecycleError) as pin_capacity:
        controller.admit("SECRET-COORDINATE", "SECRET-REVISION", "another-pin")
    with pytest.raises(LifecycleError) as state_capacity:
        controller.register("other-coordinate", "other-revision")
    controller.begin_drain("SECRET-COORDINATE", "SECRET-REVISION", reason="safe_reason")
    controller.advance("SECRET-COORDINATE", "SECRET-REVISION", deadline=0, now=0)

    assert pin_capacity.value.code == "lifecycle_pin_capacity"
    assert state_capacity.value.code == "lifecycle_state_capacity"
    assert len(controller.events) == 2
    serialized = str(controller.events)
    assert "SECRET-COORDINATE" not in serialized
    assert "SECRET-REVISION" not in serialized
    assert "SECRET-PIN" not in serialized


def test_concurrent_admission_and_drain_are_atomic() -> None:
    controller = DrainController(max_in_flight=200)
    controller.register("skill/example", "revision-1")
    barrier = Barrier(41)

    def admit(index: int) -> str | None:
        barrier.wait()
        try:
            return controller.admit("skill/example", "revision-1", f"execution-{index}").pin_id
        except LifecycleError as error:
            assert error.code == "lifecycle_not_accepting"
            return None

    def drain() -> None:
        barrier.wait()
        controller.begin_drain("skill/example", "revision-1")

    with ThreadPoolExecutor(max_workers=41) as pool:
        admissions = [pool.submit(admit, index) for index in range(40)]
        drain_future = pool.submit(drain)
        accepted = [pin for future in admissions if (pin := future.result()) is not None]
        drain_future.result()

    snapshot = controller.snapshot("skill/example", "revision-1")
    assert snapshot.in_flight == len(accepted)
    assert snapshot.state in {LifecycleState.DRAINING, LifecycleState.RETIRED}
    with pytest.raises(LifecycleError):
        controller.admit("skill/example", "revision-1", "definitely-late")
    for pin_id in accepted:
        assert controller.release(pin_id) is True
    assert controller.snapshot("skill/example", "revision-1").state is LifecycleState.RETIRED


def test_errors_and_reasons_are_stable_and_do_not_echo_targets() -> None:
    controller = DrainController()
    with pytest.raises(LifecycleError) as missing:
        controller.snapshot("SECRET-CAPABILITY")
    assert missing.value.code == "lifecycle_coordinate_not_found"
    assert "SECRET-CAPABILITY" not in str(missing.value.as_dict())

    controller.register("skill/example", "revision-1")
    with pytest.raises(ValueError) as reason:
        controller.begin_drain("skill/example", "revision-1", reason="SECRET reason")
    assert str(reason.value) == "reason must be a stable lowercase reason code"
