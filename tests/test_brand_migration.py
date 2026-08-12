from __future__ import annotations

import tomllib
from pathlib import Path

import capabilityhub
import capsift
from capabilityhub.cli import build_parser

ROOT = Path(__file__).parents[1]


def test_capsift_is_the_distribution_and_primary_command() -> None:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert document["project"]["name"] == "capsift"
    assert document["project"]["scripts"]["capsift"] == "capabilityhub.cli:main"


def test_legacy_command_and_import_remain_compatible() -> None:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert document["project"]["scripts"]["capabilityhub"] == "capabilityhub.cli:main"
    assert capsift.__version__ == capabilityhub.__version__ == "0.2.0"
    assert build_parser().prog == "capsift"


def test_plugin_uses_new_brand_without_duplicate_legacy_skill_bundle() -> None:
    assert (ROOT / "plugins" / "capsift" / ".codex-plugin" / "plugin.json").is_file()
    assert not (ROOT / "plugins" / "capabilityhub").exists()
