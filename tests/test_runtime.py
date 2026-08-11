from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.runtime import (
    discover_skills,
    local_audit,
    local_benchmark,
    local_budget_report,
    local_connections,
    local_dashboard,
    local_execute_static,
    local_health,
    local_inventory,
    local_lifecycle,
    local_load,
    local_preferences,
    local_search,
    local_set_lifecycle,
    local_set_locale,
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
    monitor = LocalCatalogMonitor(home=home, project=project, refresh_interval_seconds=0)

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

    connections = local_connections(monitor=monitor)
    states = {item["kind"]: item for item in connections["connections"]}
    assert states["skill"]["state"] == "indexed"
    assert states["mcp"]["state"] == "not_configured"
    assert connections["network_probes_performed"] == 0


def test_local_dashboard_serves_live_inventory_from_shared_monitor(tmp_path) -> None:
    home = tmp_path / "home"
    skill = home / ".codex" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\n---\nbody", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monitor = LocalCatalogMonitor(home=home, project=project, refresh_interval_seconds=0)

    with local_dashboard(project, monitor=monitor) as server:
        payload = _get_json(f"{server.url}/api/status")
        query = urlencode({"q": "demo", "kind": "skill", "limit": 5})
        searched = _get_json(f"{server.url}/api/search?{query}")
        csrf = payload["dashboard"]["csrf_token"]
        mutation = Request(
            f"{server.url}/api/lifecycle",
            data=b'{"coordinate":"codex-user/demo","state":"disabled"}',
            headers={"Content-Type": "application/json", "X-CapabilityHub-CSRF": csrf},
            method="POST",
        )
        with urlopen(mutation, timeout=2) as response:
            changed = json.loads(response.read())
        second = home / ".codex" / "skills" / "second" / "SKILL.md"
        second.parent.mkdir(parents=True)
        second.write_text("---\nname: second\n---\nbody", encoding="utf-8")
        refreshed = _get_json(f"{server.url}/api/status")

    assert payload["inventory"]["active_by_kind"]["skill"] == 1
    assert payload["health"]["catalog_loaded"] is False
    assert payload["active_capabilities"] == []
    assert payload["connections"]["scope"] == "configuration_only"
    assert searched["total_matches"] == 1
    assert changed["active"] is False
    assert refreshed["inventory"]["active_by_kind"]["skill"] == 1
    assert refreshed["inventory"]["generation"] >= 3


def test_language_and_lifecycle_persist_and_refresh_inventory(tmp_path) -> None:
    home = tmp_path / "home"
    skill = home / ".codex" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\n---\nbody", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monitor = LocalCatalogMonitor(home=home, project=project, refresh_interval_seconds=10)
    coordinate = "codex-user/demo"

    assert local_inventory(monitor=monitor)["active_by_kind"]["skill"] == 1
    saved = local_set_locale("zh-CN", scope="project", monitor=monitor)
    assert saved["saved"] is True
    assert local_preferences(monitor=monitor)["locale"] == "zh-CN"

    disabled = local_set_lifecycle(coordinate, "disabled", monitor=monitor)
    assert disabled["active"] is False
    assert local_inventory(monitor=monitor)["active_by_kind"]["skill"] == 0
    assert local_lifecycle(monitor=monitor)["entries"] == [
        {"active": False, "coordinate": coordinate, "state": "disabled"}
    ]

    enabled = local_set_lifecycle(coordinate, "enabled", monitor=monitor)
    assert enabled["active"] is True
    assert local_inventory(monitor=monitor)["active_by_kind"]["skill"] == 1


def test_local_load_records_durable_redacted_project_audit(tmp_path) -> None:
    project = tmp_path / "project"
    manifest_root = project / ".capabilityhub" / "manifests"
    manifest_root.mkdir(parents=True)
    manifest = {
        "apiVersion": "capabilityhub.io/v1alpha1",
        "kind": "Capability",
        "metadata": {
            "namespace": "local",
            "name": "audit-demo",
            "version": "1",
            "digest": "sha256:" + "9" * 64,
        },
        "spec": {
            "type": "api",
            "summary": "Audit demo",
            "provider": "static",
            "operations": [{"name": "read", "type": "execute"}],
        },
    }
    (manifest_root / "audit.json").write_text(json.dumps(manifest), encoding="utf-8")
    monitor = LocalCatalogMonitor(home=tmp_path / "home", project=project)
    revision = "local/audit-demo@1#sha256:" + "9" * 64

    local_load(revision, monitor=monitor)
    audit = local_audit(limit=10, monitor=monitor)

    assert audit["stored"] == 1
    assert audit["events"][0]["event_type"] == "load"
    assert "arguments" not in str(audit)


def test_local_monitor_rejects_a_different_explicit_project(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monitor = LocalCatalogMonitor(home=tmp_path / "home", project=first)

    with pytest.raises(ValueError, match="does not match"):
        local_inventory(second, monitor=monitor)


def test_health_marks_invalid_codex_config_degraded_without_loading_catalog(
    tmp_path,
) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("invalid = [", encoding="utf-8")

    health = local_health(tmp_path, home=tmp_path)

    checks = {item["check"]: item["status"] for item in health["checks"]}
    assert checks["codex_config"] == "invalid"
    assert health["status"] == "degraded"
    assert health["catalog_loaded"] is False


def test_local_load_and_static_execute_use_service_policy_and_budget(tmp_path) -> None:
    project = tmp_path / "project"
    manifests = project / ".capabilityhub" / "manifests"
    manifests.mkdir(parents=True)
    document = {
        "apiVersion": "capabilityhub.io/v1alpha1",
        "kind": "Capability",
        "metadata": {
            "namespace": "demo",
            "name": "records",
            "version": "1",
            "digest": "sha256:" + "b" * 64,
        },
        "spec": {
            "type": "api",
            "summary": "Static records fixture",
            "provider": "static",
            "operations": [
                {
                    "name": "read",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}},
                        "required": ["id"],
                    },
                    "outputSchema": {"type": "object", "required": ["name"]},
                }
            ],
            "sections": {"contract": {"content": "read one record", "tokens": 4}},
        },
    }
    (manifests / "records.json").write_text(json.dumps(document), encoding="utf-8")
    monitor = LocalCatalogMonitor(
        home=tmp_path / "home", project=project, refresh_interval_seconds=0
    )
    revision = "demo/records@1#sha256:" + "b" * 64

    loaded = local_load(
        revision,
        section_names=["contract"],
        operation_names=["read"],
        monitor=monitor,
    )
    executed = local_execute_static(
        revision,
        "read",
        {"id": 7},
        {"name": "demo"},
        monitor=monitor,
    )

    assert loaded["sections"][0]["content"] == "read one record"
    assert loaded["execution_ref"] is None
    assert executed["output"] == {"name": "demo"}
    assert executed["budget"]["used"]["executions"] == 1

    local_execute_static(
        revision,
        "read",
        {"id": 8},
        {"name": "private"},
        idempotency_key="durable-key",
        monitor=monitor,
    )
    with pytest.raises(CapabilityHubError) as replay:
        local_execute_static(
            revision,
            "read",
            {"id": 8},
            {"name": "private"},
            idempotency_key="durable-key",
            monitor=monitor,
        )
    assert replay.value.code == "idempotency_result_unavailable"
    assert b"private" not in (project / ".capabilityhub" / "state.sqlite3").read_bytes()


def test_budget_report_and_benchmark_are_machine_readable() -> None:
    budget = local_budget_report({"portable_tokens": 12})
    benchmark = local_benchmark()

    assert budget["remaining"]["portable_tokens"] == 12
    assert benchmark["thresholds_passed"] is True
    assert benchmark["capability_count"] == 100
