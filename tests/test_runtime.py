from __future__ import annotations

import json
from urllib.request import urlopen

import pytest

from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.runtime import (
    discover_skills,
    local_dashboard,
    local_health,
    local_inventory,
    local_search,
    validate,
)


def _get_json(url: str) -> dict[str, object]:
    with urlopen(url, timeout=2) as response:
        return json.loads(response.read())


def test_validate_and_discover_skills(tmp_path) -> None:
    manifest = {
        "apiVersion": "capabilityhub.io/v1alpha1",
        "kind": "Capability",
        "metadata": {
            "namespace": "local",
            "name": "x",
            "version": "1",
            "digest": "sha256:" + "0" * 64,
        },
        "spec": {
            "type": "api",
            "summary": "x",
            "provider": "static",
            "operations": [{"name": "read", "type": "execute"}],
        },
    }
    file = tmp_path / "x.json"
    file.write_text(json.dumps(manifest))
    assert validate([file]) == 1
    skill = tmp_path / "skills" / "x"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: x\n---\nbody")
    assert len(discover_skills([tmp_path / "skills"])) == 1


def test_local_inventory_search_and_health_share_safe_local_metadata(tmp_path) -> None:
    home = tmp_path / "home"
    skill = home / ".codex" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: Demo helper\n---\nbody", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monitor = LocalCatalogMonitor(home=home, project=project)

    inventory = local_inventory(monitor=monitor)
    result = local_search("demo", kinds=["skill"], monitor=monitor)
    health = local_health(project)

    assert inventory["active_by_kind"]["skill"] == 1
    assert result["total_matches"] == 1
    assert result["portable_tokens"] <= 900
    assert result["results"][0]["kind"] == "skill"
    assert "capability_ref" not in result["results"][0]
    assert health["catalog_loaded"] is False
    assert health["scope"] == "local_wiring"
    assert health["status"] == "ok"


def test_local_dashboard_serves_live_inventory_from_shared_monitor(tmp_path) -> None:
    home = tmp_path / "home"
    skill = home / ".codex" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\n---\nbody", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monitor = LocalCatalogMonitor(home=home, project=project)

    with local_dashboard(project, monitor=monitor) as server:
        payload = _get_json(f"{server.url}/api/status")
        second = home / ".codex" / "skills" / "second" / "SKILL.md"
        second.parent.mkdir(parents=True)
        second.write_text("---\nname: second\n---\nbody", encoding="utf-8")
        refreshed = _get_json(f"{server.url}/api/status")

    assert payload["inventory"]["active_by_kind"]["skill"] == 1
    assert payload["health"]["catalog_loaded"] is False
    assert payload["active_capabilities"] == []
    assert refreshed["inventory"]["active_by_kind"]["skill"] == 2
    assert refreshed["inventory"]["generation"] == 2


def test_local_monitor_rejects_a_different_explicit_project(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monitor = LocalCatalogMonitor(home=tmp_path / "home", project=first)

    with pytest.raises(ValueError, match="does not match"):
        local_inventory(second, monitor=monitor)
