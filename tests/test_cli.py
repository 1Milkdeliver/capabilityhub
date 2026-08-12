from __future__ import annotations

import json

import pytest

from capabilityhub import runtime
from capabilityhub.budget_store import SqliteBudgetRepository
from capabilityhub.cli import build_parser, main
from capabilityhub.errors import CapabilityHubError, ErrorCategory


def test_cli_discover_skills(tmp_path, capsys) -> None:
    skill = tmp_path / "x"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: x\n---\nbody")
    assert main(["discover-skills", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_manifest_and_compatibility_commands_route(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runtime, "local_manifest_export", lambda *_args: {"kind": "Capability"})
    monkeypatch.setattr(runtime, "local_manifest_migrate", lambda *_args: {"report": {}})
    monkeypatch.setattr(
        runtime,
        "local_openapi_import",
        lambda *_args, **_kwargs: {"source_digest": "sha256:demo"},
    )
    monkeypatch.setattr(
        runtime,
        "local_compatibility",
        lambda **_kwargs: {"decision": {"compatible": False}},
    )

    assert main(["export-manifest", "manifest.json"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "Capability"
    assert (
        main(
            [
                "import-openapi",
                "api.json",
                "--operation-id",
                "readPet",
                "--allow-host",
                "api.example.com",
                "--name",
                "pets",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["source_digest"] == "sha256:demo"
    assert main(["migrate-manifest", "legacy.json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"report": {}}
    assert main(["compatibility", "--required-feature", "security.future"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["compatible"] is False


def test_activation_lock_commands_route_and_validate_usage(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runtime, "local_activation_lock", lambda *_args: {"lockDigest": "x"})
    monkeypatch.setattr(
        runtime,
        "local_activation_lock_verify",
        lambda *_args, **_kwargs: {"valid": True},
    )

    assert main(["activation-lock"]) == 0
    assert json.loads(capsys.readouterr().out) == {"lockDigest": "x"}
    assert main(["activation-lock", "verify", "lock.json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"valid": True}
    assert main(["activation-lock", "verify"]) == 2
    assert "activation-lock verify requires a path" in capsys.readouterr().err


def test_mcp_serve_accepts_an_explicit_project_root() -> None:
    args = build_parser().parse_args(["mcp-serve", "--project-root", "project with spaces"])

    assert args.command == "mcp-serve"
    assert args.project_root == "project with spaces"

    http = build_parser().parse_args(
        ["http-serve", "--project-root", ".", "--port", "8123", "--grant", "network"]
    )
    assert http.command == "http-serve"
    assert http.port == 8123
    assert http.grant == ["network"]


@pytest.mark.parametrize(
    ("command", "runtime_name", "payload"),
    [
        ("inventory", "local_inventory", {"active_total": 3}),
        ("health", "local_health", {"status": "ok"}),
        ("connections", "local_connections", {"network_probes_performed": 0}),
        ("audit", "local_audit", {"events": []}),
        ("loaded", "local_loaded", {"entries": []}),
        ("providers", "local_providers", {"entries": []}),
    ],
)
def test_json_cli_commands_route_without_extra_output(
    command, runtime_name, payload, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(runtime, runtime_name, lambda *_args, **_kwargs: payload)

    assert main([command]) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_search_cli_passes_filters_and_emits_compact_json(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}

    def search(query, **options):
        seen.update({"query": query, **options})
        return {"results": [], "total_matches": 0}

    monkeypatch.setattr(runtime, "local_search", search)

    assert main(["search", "pdf", "--kind", "skill", "--limit", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["total_matches"] == 0
    assert seen == {
        "query": "pdf",
        "kinds": ["skill"],
        "limit": 3,
        "project_root": None,
    }


def test_routing_cli_passes_filters_and_emits_explanation(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}

    def routing(query, **options):
        seen.update({"query": query, **options})
        return {"entries": [], "model_calls": 0}

    monkeypatch.setattr(runtime, "local_routing", routing)

    assert main(["routing", "pdf", "--kind", "skill", "--limit", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    assert seen == {
        "query": "pdf",
        "kinds": ["skill"],
        "limit": 3,
        "project_root": None,
    }


@pytest.mark.parametrize("limit", ["0", "-1", "51"])
def test_search_cli_rejects_out_of_range_limits(limit) -> None:
    with pytest.raises(SystemExit) as error:
        main(["search", "demo", "--limit", limit])

    assert error.value.code == 2


def test_inventory_rejects_missing_explicit_project_root(tmp_path) -> None:
    with pytest.raises(SystemExit) as error:
        main(["inventory", "--project-root", str(tmp_path / "missing")])

    assert error.value.code == 2


def test_load_execute_budget_and_benchmark_commands_route(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runtime, "local_load", lambda *_args, **_kwargs: {"loaded": True})
    monkeypatch.setattr(
        runtime, "local_execute_static", lambda *_args, **_kwargs: {"executed": True}
    )
    monkeypatch.setattr(runtime, "local_budget_report", lambda *_args: {"budget": True})
    monkeypatch.setattr(runtime, "local_benchmark", lambda **_kwargs: {"thresholds_passed": True})
    monkeypatch.setattr(runtime, "local_scale_benchmark", lambda: {"capability_count": 10_000})

    assert main(["load", "demo/item@1#digest"]) == 0
    assert json.loads(capsys.readouterr().out) == {"loaded": True}
    assert (
        main(
            [
                "execute",
                "demo/item@1#digest",
                "read",
                "--arguments",
                '{"id":1}',
                "--fixture-output",
                '{"ok":true}',
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"executed": True}
    assert main(["budget-report", "--portable-tokens", "12"]) == 0
    assert json.loads(capsys.readouterr().out) == {"budget": True}
    assert main(["benchmark"]) == 0
    assert json.loads(capsys.readouterr().out) == {"thresholds_passed": True}
    assert main(["benchmark", "--scale"]) == 0
    assert json.loads(capsys.readouterr().out) == {"capability_count": 10_000}
    assert main(["benchmark", "--scale", "--no-enforce"]) == 2
    assert "does not accept --no-enforce" in capsys.readouterr().err


def test_language_and_lifecycle_commands_route(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runtime, "local_preferences", lambda *_args: {"locale": "en"})
    monkeypatch.setattr(runtime, "local_set_locale", lambda *_args, **_kwargs: {"locale": "zh-CN"})
    monkeypatch.setattr(runtime, "local_lifecycle", lambda *_args: {"entries": []})
    monkeypatch.setattr(
        runtime,
        "local_set_lifecycle",
        lambda *_args, **_kwargs: {"state": "quarantined"},
    )

    assert main(["language"]) == 0
    assert json.loads(capsys.readouterr().out) == {"locale": "en"}
    assert main(["language", "set", "zh-CN", "--scope", "global"]) == 0
    assert json.loads(capsys.readouterr().out) == {"locale": "zh-CN"}
    assert main(["lifecycle"]) == 0
    assert json.loads(capsys.readouterr().out) == {"entries": []}
    assert main(["lifecycle", "set", "demo/tool", "quarantined"]) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "quarantined"}


def test_approval_commands_route_without_argument_disclosure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runtime, "local_approvals", lambda *_args, **_kwargs: {"count": 0})
    monkeypatch.setattr(
        runtime,
        "local_approval_request",
        lambda *_args, **_kwargs: {"approval_id": "apr_one", "status": "pending"},
    )
    monkeypatch.setattr(
        runtime,
        "local_approval_decide",
        lambda *_args, **_kwargs: {"approval_id": "apr_one", "status": "approved"},
    )

    assert main(["approvals", "list", "--status", "pending"]) == 0
    assert json.loads(capsys.readouterr().out) == {"count": 0}
    assert (
        main(
            [
                "approvals",
                "request",
                "demo/item@1#digest",
                "write",
                "--arguments",
                '{"secret":"SECRET-CANARY"}',
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "SECRET-CANARY" not in output
    assert json.loads(output)["status"] == "pending"
    assert main(["approvals", "approve", "apr_one"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "approved"


def test_configured_execute_rejects_unsafe_approval_shortcut(capsys) -> None:
    assert main(["execute", "demo/item@1#digest", "write", "--approved"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "invalid_command_arguments"


def test_context_commands_route(monkeypatch, capsys) -> None:
    monkeypatch.setattr(runtime, "local_context", lambda *_args: {"entries": []})
    monkeypatch.setattr(
        runtime,
        "local_context_action",
        lambda action, key, **_kwargs: {"action": action, "key": key},
    )

    assert main(["context"]) == 0
    assert json.loads(capsys.readouterr().out) == {"entries": []}
    assert main(["context", "pin", "demo/tool@1#digest::contract"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "action": "pin",
        "key": "demo/tool@1#digest::contract",
    }


def test_reasoning_command_routes_explicit_advice(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}

    def reasoning(task_id, **options):
        seen.update({"task_id": task_id, **options})
        return {"tier": "medium", "should_stop": False}

    monkeypatch.setattr(runtime, "local_reasoning", reasoning)

    assert (
        main(
            [
                "reasoning",
                "recommend",
                "task-one",
                "--risk",
                "reversible_write",
                "--attempt-id",
                "attempt-1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["tier"] == "medium"
    assert seen["risk"] == "reversible_write"
    assert seen["attempt_id"] == "attempt-1"


def test_secure_audit_command_routes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        runtime,
        "local_secure_audit",
        lambda *_args, **_kwargs: {"verification": {"valid": True}},
    )

    assert main(["secure-audit", "verify"]) == 0
    assert json.loads(capsys.readouterr().out)["verification"]["valid"] is True
    assert main(["secure-audit", "export"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_command_arguments"


def test_updates_commands_route(tmp_path, monkeypatch, capsys) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"trusted-local-artifact")
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "local_updates", lambda *_args, **_kwargs: {"states": []})
    monkeypatch.setattr(
        runtime,
        "local_update_action",
        lambda action, target, **kwargs: (
            seen.append(kwargs) or {"action": action, "target": target}
        ),
    )

    assert main(["updates"]) == 0
    assert json.loads(capsys.readouterr().out) == {"states": []}
    assert (
        main(
            [
                "updates",
                "health-pass",
                "demo/tool@2#digest",
                "--artifact",
                str(artifact),
                "--publisher",
                "local-test",
                "--artifact-registry",
                "local-test",
                "--trust-mode",
                "development",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "action": "health",
        "target": "demo/tool@2#digest",
    }
    assert seen[-1]["artifact"] == b"trusted-local-artifact"
    assert seen[-1]["trust_mode"] == "development"
    assert main(["updates", "activate", "demo/tool@2#digest"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_command_arguments"
    assert main(["updates", "pin", "demo/tool"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_command_arguments"


@pytest.mark.parametrize(
    "arguments",
    [
        ["language", "set"],
        ["language", "show", "en"],
        ["lifecycle", "set", "demo/tool"],
        ["lifecycle", "list", "demo/tool"],
    ],
)
def test_management_commands_reject_incomplete_shapes(arguments, capsys) -> None:
    assert main(arguments) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_command_arguments"


def test_cli_renders_structured_safe_runtime_errors(monkeypatch, capsys) -> None:
    def fail(*_args, **_kwargs):
        raise CapabilityHubError(
            code="stale_revision",
            category=ErrorCategory.REFERENCE,
            safe_message="The revision is stale.",
        )

    monkeypatch.setattr(runtime, "local_load", fail)

    assert main(["load", "missing"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "stale_revision"
    assert error["safe_message"] == "The revision is stale."


def test_budget_report_reads_durable_project_usage(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "budget-report",
                "--project-root",
                str(tmp_path),
                "--executions",
                "2",
            ]
        )
        == 0
    )
    configured = json.loads(capsys.readouterr().out)
    assert configured["limits"]["executions"] == 2

    ledger = SqliteBudgetRepository(tmp_path / ".capabilityhub" / "state.sqlite3").ledger(
        "local-cli", {}
    )
    ledger.spend({"executions": 1})

    assert main(["budget-report", "--project-root", str(tmp_path)]) == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported["used"]["executions"] == 1
    assert reported["remaining"]["executions"] == 1
    assert reported["persistent"] is True
