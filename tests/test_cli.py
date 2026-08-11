from __future__ import annotations

import json

import pytest

from capabilityhub import runtime
from capabilityhub.cli import build_parser, main
from capabilityhub.errors import CapabilityHubError, ErrorCategory


def test_cli_discover_skills(tmp_path, capsys) -> None:
    skill = tmp_path / "x"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: x\n---\nbody")
    assert main(["discover-skills", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_mcp_serve_accepts_an_explicit_project_root() -> None:
    args = build_parser().parse_args(["mcp-serve", "--project-root", "project with spaces"])

    assert args.command == "mcp-serve"
    assert args.project_root == "project with spaces"


@pytest.mark.parametrize(
    ("command", "runtime_name", "payload"),
    [
        ("inventory", "local_inventory", {"active_total": 3}),
        ("health", "local_health", {"status": "ok"}),
        ("connections", "local_connections", {"network_probes_performed": 0}),
        ("audit", "local_audit", {"events": []}),
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
