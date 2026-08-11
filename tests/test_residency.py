import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.residency import ContextInventory, ResidentSection


def section(
    key: str,
    tokens: int,
    *,
    pinned: bool = False,
    reuse: int = 1,
    reload_cost: int = 1,
) -> ResidentSection:
    return ResidentSection(
        key=key,
        revision="test/item@1#sha256:abc",
        section="contract",
        portable_tokens=tokens,
        pinned=pinned,
        reuse_score=reuse,
        reload_cost=reload_cost,
    )


def test_inventory_evicts_low_value_unpinned_material() -> None:
    inventory = ContextInventory(10)
    inventory.add(section("cheap", 6))
    inventory.add(section("valuable", 4, reuse=10, reload_cost=10))

    evictions = inventory.add(section("new", 4))

    assert [event.key for event in evictions] == ["cheap"]
    assert {entry.key for entry in inventory.entries} == {"valuable", "new"}
    assert inventory.used_portable_tokens == 8


def test_inventory_preserves_pins_and_rolls_back_failed_add() -> None:
    inventory = ContextInventory(5)
    inventory.add(section("required", 5, pinned=True))

    with pytest.raises(CapabilityHubError) as error:
        inventory.add(section("also-required", 1, pinned=True))

    assert error.value.code == "context_budget_exhausted"
    assert [entry.key for entry in inventory.entries] == ["required"]


def test_access_updates_recency_tie_breaker() -> None:
    inventory = ContextInventory(4)
    inventory.add(section("first", 2))
    inventory.add(section("second", 2))
    inventory.access("first")

    evictions = inventory.add(section("third", 2))

    assert evictions[0].key == "second"
