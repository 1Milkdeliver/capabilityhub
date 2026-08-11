from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "capabilityhub"


def test_helpme_is_the_plugin_entry_and_menu_is_progressive() -> None:
    manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    skill = (PLUGIN / "skills" / "helpme" / "SKILL.md").read_text(encoding="utf-8")

    assert manifest["interface"]["defaultPrompt"] == ["/helpme", "/myskills"]
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
    reference_root_items = _group_items(reference["root"]["groups"])
    assert "/myskills" in reference_root_items
    assert "/helpme providers" in reference_root_items
    assert "/helpme routing" in reference_root_items
    assert "/helpme mcp" in reference_root_items

    for catalog in catalogs.values():
        assert set(catalog["topics"]) == expected_topics
        root_items = _group_items(catalog["root"]["groups"])
        assert set(root_items) == set(reference_root_items)
        assert set(catalog["language"]) == set(reference["language"])
        for topic in expected_topics:
            assert set(catalog["topics"][topic]) == set(reference["topics"][topic])
        descriptions = list(root_items.values())
        descriptions.extend(catalog["language"].values())
        for topic in catalog["topics"].values():
            descriptions.extend(topic.values())
        assert all(
            (text.startswith("(") and text.endswith(")"))
            or (text.startswith("\uff08") and text.endswith("\uff09"))
            for text in descriptions
        )


def test_myskills_catalogs_match_and_keep_professional_terms_visible() -> None:
    skill_root = PLUGIN / "skills" / "myskills"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    catalogs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in (skill_root / "references" / "locales").glob("*.json")
    }
    assert set(catalogs) == {"en", "zh-CN"}
    assert "Never intercept native" in skill
    reference_items = _group_items(catalogs["en"]["groups"])
    for command in (
        "/myskills list",
        "/myskills loaded",
        "/myskills providers",
        "/myskills routing <name>",
        "/myskills lifecycle <name>",
        "/myskills risks <name>",
        "/myskills conflicts",
    ):
        assert command in reference_items
    for catalog in catalogs.values():
        items = _group_items(catalog["groups"])
        assert set(items) == set(reference_items)
        assert all(
            (text.startswith("(") and text.endswith(")"))
            or (text.startswith("\uff08") and text.endswith("\uff09"))
            for text in items.values()
        )


def _group_items(groups: list[dict[str, object]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for group in groups:
        items = group["items"]
        assert isinstance(items, dict)
        assert all(isinstance(key, str) and isinstance(value, str) for key, value in items.items())
        merged.update(items)
    return merged
