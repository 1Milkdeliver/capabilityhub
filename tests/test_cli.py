from __future__ import annotations

import json

import pytest

from capabilityhub import runtime
from capabilityhub.cli import build_parser, main


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
    ],
)
def test_json_cli_commands_route_without_extra_output(
    command, runtime_name, payload, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(runtime, runtime_name, lambda _project: payload)

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
