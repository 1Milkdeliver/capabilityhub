from __future__ import annotations

import json

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
