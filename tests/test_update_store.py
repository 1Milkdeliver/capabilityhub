from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.update_store import SQLiteUpdateStore


def _activate_initial(store: SQLiteUpdateStore, revision: str = "demo/tool@1#sha256:1") -> None:
    store.stage("demo/tool", revision, expected_active_revision=None)
    store.record_health("demo/tool", revision, passed=True)
    store.activate(
        "demo/tool",
        revision,
        expected_active_revision=None,
        validate=lambda _: None,
    )


def test_health_failure_never_switches_active_pointer(tmp_path) -> None:
    store = SQLiteUpdateStore(tmp_path / "updates.sqlite3")
    _activate_initial(store)
    candidate = "demo/tool@2#sha256:2"

    store.stage("demo/tool", candidate, expected_active_revision="demo/tool@1#sha256:1")
    failed = store.record_health("demo/tool", candidate, passed=False)

    assert failed.active_revision == "demo/tool@1#sha256:1"
    with pytest.raises(CapabilityHubError, match="health-passed"):
        store.activate(
            "demo/tool",
            candidate,
            expected_active_revision="demo/tool@1#sha256:1",
            validate=lambda _: None,
        )
    assert store.state("demo/tool").active_revision == "demo/tool@1#sha256:1"


def test_bootstrap_records_catalog_pointer_once_without_overwrite(tmp_path) -> None:
    store = SQLiteUpdateStore(tmp_path / "updates.sqlite3")
    first = "demo/tool@1#sha256:1"

    assert store.bootstrap_active("demo/tool", first).active_revision == first
    assert store.bootstrap_active("demo/tool", first).active_revision == first
    with pytest.raises(CapabilityHubError) as conflict:
        store.bootstrap_active("demo/tool", "demo/tool@2#sha256:2")
    assert conflict.value.code == "active_revision_changed"


def test_activate_retains_previous_revision_and_rollback_swaps_pointers(tmp_path) -> None:
    store = SQLiteUpdateStore(tmp_path / "updates.sqlite3")
    first = "demo/tool@1#sha256:1"
    second = "demo/tool@2#sha256:2"
    _activate_initial(store, first)
    store.stage("demo/tool", second, expected_active_revision=first)
    store.record_health("demo/tool", second, passed=True)

    activated = store.activate(
        "demo/tool", second, expected_active_revision=first, validate=lambda _: None
    )
    rolled_back = store.rollback(
        "demo/tool", expected_active_revision=second, validate=lambda _: None
    )

    assert activated.active_revision == second
    assert activated.previous_revision == first
    assert rolled_back.active_revision == first
    assert rolled_back.previous_revision == second


def test_concurrent_activation_cas_prevents_lost_update(tmp_path) -> None:
    store = SQLiteUpdateStore(tmp_path / "updates.sqlite3")
    first = "demo/tool@1#sha256:1"
    second = "demo/tool@2#sha256:2"
    _activate_initial(store, first)
    store.stage("demo/tool", second, expected_active_revision=first)
    store.record_health("demo/tool", second, passed=True)

    def activate(_: int) -> str:
        try:
            store.activate(
                "demo/tool", second, expected_active_revision=first, validate=lambda _: None
            )
        except CapabilityHubError as error:
            return error.code
        return "activated"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(activate, range(2)))

    assert sorted(outcomes) == ["activated", "active_revision_changed"]
    assert store.state("demo/tool").active_revision == second


def test_in_flight_pin_stays_on_revision_across_activation(tmp_path) -> None:
    store = SQLiteUpdateStore(tmp_path / "updates.sqlite3")
    first = "demo/tool@1#sha256:1"
    second = "demo/tool@2#sha256:2"
    _activate_initial(store, first)
    pin = store.pin_active("demo/tool", "request-1")

    store.stage("demo/tool", second, expected_active_revision=first)
    store.record_health("demo/tool", second, passed=True)
    store.activate("demo/tool", second, expected_active_revision=first, validate=lambda _: None)

    assert pin.revision == first
    assert store.pins("demo/tool") == (pin,)
    assert store.release_pin("request-1")
    assert store.pins("demo/tool") == ()


def test_states_are_bounded_and_sorted_by_coordinate(tmp_path) -> None:
    store = SQLiteUpdateStore(tmp_path / "updates.sqlite3")
    store.stage("z/tool", "z/tool@1#sha256:z", expected_active_revision=None)
    store.stage("a/tool", "a/tool@1#sha256:a", expected_active_revision=None)
    store.stage("m/tool", "m/tool@1#sha256:m", expected_active_revision=None)

    assert [state.coordinate for state in store.states()] == ["a/tool", "m/tool", "z/tool"]
    assert [state.coordinate for state in store.states(limit=2)] == ["a/tool", "m/tool"]
    with pytest.raises(ValueError, match="states limit"):
        store.states(limit=0)
    with pytest.raises(ValueError, match="states limit"):
        store.states(limit=SQLiteUpdateStore.MAX_STATE_ROWS + 1)
