from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "capabilityhub"


def test_helpme_is_the_plugin_entry_and_menu_is_progressive() -> None:
    manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    skill = (PLUGIN / "skills" / "helpme" / "SKILL.md").read_text(encoding="utf-8")

    assert manifest["interface"]["defaultPrompt"] == ["/helpme"]
    assert "name: helpme" in skill
    for topic in ("status", "dashboard", "budget", "providers", "mcp", "benchmark", "security"):
        assert f"/helpme {topic}" in skill
    assert "只加载你选择的主题" in skill
    assert "Never discover or preload" in skill
