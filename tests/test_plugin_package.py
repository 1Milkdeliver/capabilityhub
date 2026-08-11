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
    assert "static message catalogs" in skill
    assert "Do not discover or preload" in skill


def test_helpme_locale_catalogs_have_matching_parenthesized_menus() -> None:
    locale_dir = PLUGIN / "skills" / "helpme" / "references" / "locales"
    catalogs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in locale_dir.glob("*.json")
    }
    assert set(catalogs) == {"en", "zh-CN"}

    expected_topics = {
        "overview",
        "capabilities",
        "consumption",
        "runtime",
        "security",
        "evaluation",
        "settings",
        "about",
    }
    reference = catalogs["en"]
    assert set(reference["topics"]) == expected_topics
    assert "/helpme language" in reference["root"]["items"]

    for catalog in catalogs.values():
        assert set(catalog["topics"]) == expected_topics
        assert set(catalog["root"]["items"]) == set(reference["root"]["items"])
        assert set(catalog["language"]) == set(reference["language"])
        for topic in expected_topics:
            assert set(catalog["topics"][topic]) == set(reference["topics"][topic])
        descriptions = list(catalog["root"]["items"].values())
        descriptions.extend(catalog["language"].values())
        for topic in catalog["topics"].values():
            descriptions.extend(topic.values())
        assert all(
            (text.startswith("(") and text.endswith(")"))
            or (text.startswith("\uff08") and text.endswith("\uff09"))
            for text in descriptions
        )
