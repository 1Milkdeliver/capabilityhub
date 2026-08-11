from __future__ import annotations

import json

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.state import (
    global_config_path,
    project_config_path,
    resolved_preferences,
    set_lifecycle,
    set_locale,
)


def test_preferences_preserve_unknown_keys_and_project_overrides_global(tmp_path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    global_path = global_config_path(home)
    global_path.parent.mkdir(parents=True)
    global_path.write_text(
        json.dumps({"locale": "en", "unrelated": {"keep": True}}), encoding="utf-8"
    )

    set_locale("zh-CN", scope="project", home=home, project=project)
    set_lifecycle("demo/tool", "disabled", scope="global", home=home, project=project)
    set_lifecycle("demo/tool", "quarantined", scope="project", home=home, project=project)

    resolved = resolved_preferences(home=home, project=project)
    assert resolved["locale"] == "zh-CN"
    assert resolved["capabilities"] == {"demo/tool": "quarantined"}
    assert json.loads(global_path.read_text(encoding="utf-8"))["unrelated"] == {"keep": True}


def test_project_enabled_state_overrides_global_disabled_state(tmp_path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    set_lifecycle("demo/tool", "disabled", scope="global", home=home, project=project)
    set_lifecycle("demo/tool", "quarantined", scope="project", home=home, project=project)

    set_lifecycle("demo/tool", "enabled", scope="project", home=home, project=project)

    assert resolved_preferences(home=home, project=project)["capabilities"] == {
        "demo/tool": "enabled"
    }
    assert json.loads(project_config_path(project).read_text(encoding="utf-8"))["capabilities"][
        "demo/tool"
    ] == {"state": "enabled"}


@pytest.mark.parametrize("locale", ["fr", "中文", ""])
def test_invalid_locale_is_rejected_without_writing(tmp_path, locale) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(CapabilityHubError, match="Locale must"):
        set_locale(locale, scope="project", project=project)

    assert not project_config_path(project).exists()
