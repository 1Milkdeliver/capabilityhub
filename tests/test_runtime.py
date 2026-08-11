from __future__ import annotations

import json

from capabilityhub.runtime import discover_skills, validate


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
