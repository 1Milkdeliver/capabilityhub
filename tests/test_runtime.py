from __future__ import annotations

import hashlib
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.protocol import protocol_handshake
from capabilityhub.runtime import (
    discover_skills,
    local_activation_lock,
    local_activation_lock_verify,
    local_audit,
    local_benchmark,
    local_budget_report,
    local_compatibility,
    local_connections,
    local_context,
    local_context_action,
    local_dashboard,
    local_execute_static,
    local_health,
    local_http_control,
    local_inventory,
    local_lifecycle,
    local_load,
    local_loaded,
    local_manifest_export,
    local_manifest_migrate,
    local_openapi_import,
    local_preferences,
    local_providers,
    local_reasoning,
    local_routing,
    local_scale_benchmark,
    local_search,
    local_secure_audit,
    local_set_lifecycle,
    local_set_locale,
    local_update_action,
    local_updates,
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

    exported = local_manifest_export(file)
    assert exported["apiVersion"] == "capabilityhub.io/v1alpha1"


def test_openapi_import_runtime_is_offline_and_explicit(tmp_path) -> None:
    source = tmp_path / "pets.json"
    source.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "Pets", "version": "1"},
                "servers": [{"url": "https://api.example.com"}],
                "paths": {
                    "/pets": {
                        "get": {
                            "operationId": "listPets",
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = local_openapi_import(
        source,
        operation_ids=["listPets"],
        allowed_hosts=["api.example.com"],
        namespace="demo",
        name="pets",
        version="1.0.0",
    )

    assert result["selected_operation_ids"] == ["listPets"]
    manifest = result["manifest"]
    assert isinstance(manifest, dict)
    assert manifest["spec"]["type"] == "api"
    assert manifest["spec"]["operations"][0]["name"] == "listpets"


def test_activation_lock_runtime_round_trip(tmp_path) -> None:
    home = tmp_path / "home"
    skill = home / ".codex" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\n---\nbody", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monitor = LocalCatalogMonitor(home=home, project=project)
    document = local_activation_lock(monitor=monitor)
    path = tmp_path / "activation-lock.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    result = local_activation_lock_verify(path, monitor=monitor)

    assert result["valid"] is True
    assert result["capability_count"] == 2


def test_manifest_migration_preview_and_compatibility_fail_closed(tmp_path) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "api_version": "capabilityhub.io/v1alpha0",
                "manifestKind": "capability",
                "metadata": {
                    "namespaceName": "demo",
                    "packageName": "old",
                    "version": "1",
                    "contentDigest": "sha256:" + "c" * 64,
                },
                "spec": {
                    "capabilityType": "api",
                    "description": "Old manifest.",
                    "providerName": "static",
                    "operations": [{"name": "read"}],
                },
            }
        ),
        encoding="utf-8",
    )

    migrated = local_manifest_migrate(legacy)
    decision = local_compatibility(required_features=["security.future-required"])

    assert migrated["report"]["changed"] is True
    assert migrated["document"]["apiVersion"] == "capabilityhub.io/v1alpha1"
    assert decision["decision"]["compatible"] is False
    assert decision["decision"]["reason_codes"] == ["unsupported_client_required_feature"]


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


def test_local_http_control_runs_real_service_search(tmp_path) -> None:
    home = tmp_path / "home"
    skill = home / ".codex" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: Demo helper\n---\nbody", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monitor = LocalCatalogMonitor(home=home, project=project)
    server, access = local_http_control(monitor=monitor)
    handshake = protocol_handshake()
    body = json.dumps(
        {
            "request_id": "request-1",
            "correlation_id": "correlation-1",
            "operation": "capability.search",
            "payload": {"query": "demo", "task_id": "task-1"},
            "handshake": {
                "api_versions": list(handshake.api_versions),
                "supported_features": list(handshake.supported_features),
                "required_features": list(handshake.required_features),
            },
        }
    ).encode()
    request = Request(
        access.url,
        data=body,
        headers={
            "Authorization": f"Bearer {access.bearer_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as raw_response:
            response = json.loads(raw_response.read())
    finally:
        server.close()

    assert response["ok"] is True
    assert response["result"]["total_matches"] == 1


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
    assert payload["approvals"]["count"] == 0
    assert payload["context"]["entries"] == []
    assert payload["reasoning"]["current_tier"] is None
    assert payload["updates"]["states"] == []
    assert payload["secure_audit"]["configured"] is False
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


def test_staged_update_activation_and_rollback_change_live_catalog_pointer(tmp_path) -> None:
    project = tmp_path / "project"
    root = project / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    artifacts: dict[str, bytes] = {}
    revisions: dict[str, str] = {}
    for version in ("1.0.0", "2.0.0"):
        artifact = f"artifact-{version}".encode()
        artifacts[version] = artifact
        digest = "sha256:" + hashlib.sha256(artifact).hexdigest()
        document = {
            "apiVersion": "capabilityhub.io/v1alpha1",
            "kind": "Capability",
            "metadata": {
                "namespace": "demo",
                "name": "updatable",
                "version": version,
                "digest": digest,
            },
            "spec": {
                "type": "api",
                "summary": f"Updatable {version}",
                "provider": "static",
                "operations": [{"name": "read"}],
            },
        }
        (root / f"v{version[0]}.json").write_text(json.dumps(document), encoding="utf-8")
        revisions[version] = f"demo/updatable@{version}#{digest}"
    monitor = LocalCatalogMonitor(project=project, home=tmp_path / "home")
    first = revisions["1.0.0"]
    second = revisions["2.0.0"]
    assert monitor.snapshot().registry.activations["demo/updatable"] == second

    trust = {
        "artifact": artifacts["1.0.0"],
        "publisher": "local-test",
        "artifact_registry": "local-test",
        "trust_mode": "development",
    }
    local_update_action("stage", first, monitor=monitor, **trust)
    local_update_action("health", first, health_passed=True, monitor=monitor, **trust)
    local_update_action("activate", first, monitor=monitor, **trust)
    assert monitor.snapshot().registry.activations["demo/updatable"] == first
    assert local_updates(monitor=monitor)["states"][0]["previous_revision"] == second

    local_update_action("rollback", "demo/updatable", monitor=monitor)
    assert monitor.snapshot().registry.activations["demo/updatable"] == second


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
    loaded = local_loaded(monitor=monitor)
    providers = local_providers(monitor=monitor)
    routing = local_routing("audit demo", monitor=monitor)

    assert audit["stored"] == 1
    assert audit["events"][0]["event_type"] == "load"
    assert "arguments" not in str(audit)
    assert loaded["entries"][0]["revision"] == revision
    assert loaded["entries"][0]["provider"] == "static"
    assert any(item["provider"] == "static" for item in providers["entries"])
    assert routing["model_calls"] == 0
    assert routing["entries"][0]["revision"] == revision


def test_opt_in_secure_audit_is_used_by_real_local_load(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CAPABILITYHUB_AUDIT_KEY", "secure-local-test-key-32-bytes")
    project = tmp_path / "project"
    manifest_root = project / ".capabilityhub" / "manifests"
    manifest_root.mkdir(parents=True)
    document = {
        "apiVersion": "capabilityhub.io/v1alpha1",
        "kind": "Capability",
        "metadata": {
            "namespace": "local",
            "name": "secure-audit-demo",
            "version": "1",
            "digest": "sha256:" + "7" * 64,
        },
        "spec": {
            "type": "api",
            "summary": "Secure audit demo",
            "provider": "static",
            "operations": [{"name": "read"}],
        },
    }
    (manifest_root / "secure.json").write_text(json.dumps(document), encoding="utf-8")
    revision = "local/secure-audit-demo@1#sha256:" + "7" * 64

    local_load(revision, project_root=project)
    viewed = local_audit(project, limit=10)
    verified = local_secure_audit("verify", project_root=project)
    rotated = local_secure_audit("rotate", project_root=project)
    destination = tmp_path / "export.jsonl"
    exported = local_secure_audit(
        "export",
        source=str(rotated["archive"]),
        destination=destination,
        project_root=project,
    )

    assert viewed["stored"] == 1
    assert verified["verification"]["valid"] is True
    assert exported["exported"] is True
    assert destination.is_file()
    assert b"secure-local-test-key" not in destination.read_bytes()


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
    resident = local_context(project)
    assert resident["entries"][0]["section"] == "contract"
    key = resident["entries"][0]["key"]
    assert local_context_action("pin", key, project_root=project)["entries"][0]["pinned"]
    assert not local_context_action("unpin", key, project_root=project)["entries"][0]["pinned"]
    assert executed["output"] == {"name": "demo"}
    assert executed["budget"]["used"]["executions"] == 1
    persisted = local_budget_report(project_root=project)
    assert persisted["used"]["loads"] == 2
    assert persisted["used"]["executions"] == 1

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

    current = local_budget_report(project_root=project)
    local_budget_report({"loads": current["used"]["loads"]}, project)
    with pytest.raises(CapabilityHubError) as exhausted:
        local_load(revision, monitor=monitor)
    assert exhausted.value.code == "budget_exhausted"


def test_budget_report_and_benchmark_are_machine_readable(tmp_path) -> None:
    budget = local_budget_report({"portable_tokens": 12}, tmp_path)
    benchmark = local_benchmark()

    assert budget["remaining"]["portable_tokens"] == 12
    assert budget["persistent"] is True
    assert budget["storage"] == "sqlite"
    assert benchmark["thresholds_passed"] is True
    assert benchmark["capability_count"] == 100


def test_scale_benchmark_reports_bounded_scope_and_concurrency() -> None:
    report = local_scale_benchmark()

    assert report["capability_count"] == 10_000
    assert report["concurrent_read_target"] >= 100
    assert "not a 1m-document RAG claim" in report["scope_limits"]


def test_local_reasoning_persists_budget_aware_anti_loop_state(tmp_path) -> None:
    first = local_reasoning(
        "task-one",
        action="recommend",
        attempt_id="SECRET-ATTEMPT",
        evidence_id="SECRET-EVIDENCE",
        project_root=tmp_path,
    )
    repeated = local_reasoning(
        "task-one",
        action="recommend",
        escalation_reason="retry failed",
        attempt_id="SECRET-ATTEMPT",
        evidence_id="SECRET-EVIDENCE",
        project_root=tmp_path,
    )
    state = local_reasoning("task-one", project_root=tmp_path)

    assert first["tier"] == "low"
    assert repeated["should_stop"] is True
    assert state["recommendation_count"] == 2
    database = (tmp_path / ".capabilityhub" / "state.sqlite3").read_bytes()
    assert b"SECRET-ATTEMPT" not in database
    assert b"SECRET-EVIDENCE" not in database
