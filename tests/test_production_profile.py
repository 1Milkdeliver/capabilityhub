from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from capabilityhub.production_profile import load_production_profile, profile_digest


def test_reference_profile_is_reproducible_and_fail_closed() -> None:
    first = load_production_profile("examples/production-reference.json")
    second = load_production_profile("examples/production-reference.json")

    assert profile_digest(first) == profile_digest(second)
    assert {item["plane"] for item in first["listeners"]} == {"data", "admin"}
    assert all(item["on_unknown"] == "deny" for item in first["dependencies"])
    assert first["external_credentials"] == "not-required-for-validation"
    assert first["supply_chain"]["checkpoint_observer"] == "persistent-required"


def test_reference_profile_rejects_permissive_dependency(tmp_path: Path) -> None:
    profile = load_production_profile("examples/production-reference.json")
    changed = deepcopy(profile)
    changed["dependencies"][2]["on_unknown"] = "allow"
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ValueError, match="fail closed"):
        load_production_profile(path)


def test_reference_profile_rejects_shared_plane_binding(tmp_path: Path) -> None:
    profile = load_production_profile("examples/production-reference.json")
    changed = deepcopy(profile)
    changed["listeners"][1]["bind"] = changed["listeners"][0]["bind"]
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ValueError, match="distinct"):
        load_production_profile(path)


def test_reference_profile_rejects_offline_supply_chain(tmp_path: Path) -> None:
    profile = load_production_profile("examples/production-reference.json")
    changed = deepcopy(profile)
    changed["supply_chain"]["online_checkpoint"] = "optional"
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ValueError, match="supply-chain"):
        load_production_profile(path)
