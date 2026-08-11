from __future__ import annotations

import json

import pytest

import capabilityhub.context_state as context_state_module
from capabilityhub.context_state import LocalContextState
from capabilityhub.errors import CapabilityHubError
from capabilityhub.residency import ResidentSection


def _section(
    key: str,
    tokens: int,
    *,
    pinned: bool = False,
    reuse_score: int = 1,
) -> ResidentSection:
    return ResidentSection(
        key=key,
        revision=f"test/{key}@1#sha256:abc",
        section="contract",
        portable_tokens=tokens,
        pinned=pinned,
        reuse_score=reuse_score,
    )


def test_context_state_persists_inventory_and_generation(tmp_path) -> None:
    path = tmp_path / "state" / "context.json"
    state = LocalContextState(path, max_portable_tokens=10)

    state.add(_section("first", 4))
    state.add(_section("second", 3, pinned=True))
    restored = LocalContextState(path, max_portable_tokens=10).snapshot()

    assert restored.generation == 2
    assert restored.used_portable_tokens == 7
    assert [(item.key, item.pinned) for item in restored.entries] == [
        ("first", False),
        ("second", True),
    ]
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_context_state_rejects_corrupt_state_without_leaking_content(tmp_path) -> None:
    path = tmp_path / "context.json"
    path.write_text("SECRET-CANARY {", encoding="utf-8")

    with pytest.raises(CapabilityHubError) as caught:
        LocalContextState(path, max_portable_tokens=10)

    assert caught.value.code == "context_state_invalid"
    assert "SECRET-CANARY" not in str(caught.value.as_dict())


def test_access_order_survives_restart_and_drives_eviction(tmp_path) -> None:
    path = tmp_path / "context.json"
    state = LocalContextState(path, max_portable_tokens=4)
    state.add(_section("first", 2))
    state.add(_section("second", 2))
    state.access("first")

    restored = LocalContextState(path, max_portable_tokens=4)
    evictions = restored.add(_section("third", 2))

    assert [item.key for item in evictions] == ["second"]
    assert [item.key for item in restored.snapshot().entries] == ["first", "third"]


def test_failed_pinned_add_restores_partially_evicted_state_and_file(tmp_path) -> None:
    path = tmp_path / "context.json"
    state = LocalContextState(path, max_portable_tokens=7)
    state.add(_section("required", 5, pinned=True))
    state.add(_section("evictable", 2))

    with pytest.raises(CapabilityHubError) as caught:
        state.add(_section("also-required", 3, pinned=True))

    assert caught.value.code == "context_budget_exhausted"
    assert [item.key for item in state.snapshot().entries] == ["evictable", "required"]
    restored = LocalContextState(path, max_portable_tokens=7).snapshot()
    assert [item.key for item in restored.entries] == ["evictable", "required"]


def test_disk_failure_rolls_back_memory_and_emits_no_event(tmp_path, monkeypatch) -> None:
    path = tmp_path / "context.json"
    state = LocalContextState(path, max_portable_tokens=10)
    state.add(_section("saved", 2))
    events_before = state.events

    def fail_replace(source, destination) -> None:
        del source, destination
        raise OSError("SECRET-CANARY")

    monkeypatch.setattr(context_state_module.os, "replace", fail_replace)
    with pytest.raises(CapabilityHubError) as caught:
        state.add(_section("not-saved", 2))

    assert caught.value.code == "context_state_write_failed"
    assert [item.key for item in state.snapshot().entries] == ["saved"]
    assert state.events == events_before
    restored = LocalContextState(path, max_portable_tokens=10).snapshot()
    assert [item.key for item in restored.entries] == ["saved"]


def test_context_events_are_bounded_and_report_evictions(tmp_path) -> None:
    state = LocalContextState(tmp_path / "context.json", max_portable_tokens=4, event_limit=2)
    state.add(_section("first", 2))
    state.add(_section("second", 2))
    evictions = state.add(_section("third", 2))

    assert [item.key for item in evictions] == ["first"]
    assert [(event.sequence, event.action) for event in state.events] == [
        (2, "add"),
        (3, "add"),
    ]
    assert state.events[-1].evictions == evictions
