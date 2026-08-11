from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.local_runtime import LocalCatalogMonitor


def test_monitor_refreshes_only_when_local_inputs_change(tmp_path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monitor = LocalCatalogMonitor(
        home=home, project=project, refresh_interval_seconds=0
    )

    first = monitor.snapshot()
    unchanged = monitor.snapshot()
    assert first.inventory["generation"] == 1
    assert unchanged.inventory["generation"] == 1
    assert unchanged.registry is first.registry

    skill = home / ".codex" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: demo\ndescription: Refresh test\n---\nbody",
        encoding="utf-8",
    )
    refreshed = monitor.snapshot()

    assert refreshed.inventory["generation"] == 2
    assert refreshed.inventory["active_by_kind"]["skill"] == 1
    assert set(refreshed.inventory["active_by_kind"]) == {
        "api",
        "cli",
        "mcp",
        "rag",
        "skill",
    }
    assert refreshed.registry is not first.registry


def test_monitor_keeps_last_complete_generation_when_refresh_fails(
    tmp_path, monkeypatch
) -> None:
    monitor = LocalCatalogMonitor(
        home=tmp_path / "home", project=tmp_path, refresh_interval_seconds=0
    )
    complete = monitor.snapshot()
    monkeypatch.setattr(
        "capabilityhub.local_runtime.local_catalog_fingerprint",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("SECRET-CANARY")),
    )

    stale = monitor.snapshot()

    assert stale.registry is complete.registry
    assert stale.inventory["generation"] == complete.inventory["generation"]
    assert stale.inventory["status"] == "stale"
    assert stale.inventory["last_refresh_error_code"] == "catalog_refresh_failed"
    assert "SECRET-CANARY" not in str(stale.inventory)


def test_generation_inventory_copy_and_registry_are_read_only(tmp_path) -> None:
    monitor = LocalCatalogMonitor(home=tmp_path / "home", project=tmp_path)
    generation = monitor.snapshot()
    exposed = generation.inventory_json()
    exposed["active_by_kind"]["skill"] = 999

    assert generation.inventory_json()["active_by_kind"]["skill"] != 999
    with pytest.raises(CapabilityHubError, match="read-only"):
        generation.registry.activate("missing")


def test_refresh_window_coalesces_concurrent_fingerprint_checks(
    tmp_path, monkeypatch
) -> None:
    calls = 0

    def fingerprint(**_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "stable"

    monkeypatch.setattr(
        "capabilityhub.local_runtime.local_catalog_fingerprint", fingerprint
    )
    monitor = LocalCatalogMonitor(
        home=tmp_path / "home",
        project=tmp_path,
        refresh_interval_seconds=10,
    )
    monitor.snapshot()

    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(lambda _: monitor.snapshot(), range(20)))

    assert calls == 1
    assert {item.inventory["generation"] for item in snapshots} == {1}
