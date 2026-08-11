from __future__ import annotations

import hashlib

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import ExecutionRequest
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.skill import SkillProvider


def test_skill_provider_discovers_safe_frontmatter_and_sectioned_content(tmp_path) -> None:
    skill = tmp_path / "analysis" / "SKILL.md"
    skill.parent.mkdir()
    content = """---
name: csv-analyzer
description: Analyze a CSV file safely
version: 1.2.0
license: MIT
allowed-tools:
  - filesystem.read
tags: [data, csv]
unsafe-object: !!python/object:bad
metadata:
  nested: ignored
multiline: >
  ignored safely
---
# CSV analyzer

Read a supplied CSV file and summarize it.
"""
    skill.write_text(content, encoding="utf-8")

    manifest = SkillProvider([tmp_path]).discover()[0]

    assert manifest.identity.coordinate == "skills/csv-analyzer"
    assert manifest.identity.version == "1.2.0"
    assert manifest.permissions == ("filesystem.read",)
    assert manifest.tags == ("data", "csv")
    assert manifest.sections[0].name == "instructions"
    assert manifest.sections[0].content.startswith("# CSV analyzer")
    assert (
        manifest.metadata["content_digest"]
        == f"sha256:{hashlib.sha256(skill.read_bytes()).hexdigest()}"
    )
    assert manifest.metadata["provenance"] == "skill://" + tmp_path.name + "/analysis/SKILL.md"
    assert "unsafe-object" not in manifest.metadata


def test_skill_provider_does_not_import_or_execute_scripts(tmp_path) -> None:
    package = tmp_path / "safe"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: safe\n---\nUse only documented steps.\n", encoding="utf-8"
    )
    (package / "scripts").mkdir()
    (package / "scripts" / "danger.py").write_text(
        "raise RuntimeError('executed')", encoding="utf-8"
    )

    provider = SkillProvider([tmp_path])
    manifest = provider.discover()[0]

    assert manifest.operations[0].name == "load"
    with pytest.raises(CapabilityHubError, match="cannot be executed"):
        provider.execute(
            manifest.identity,
            ExecutionRequest(manifest.identity.revision, "load", {}, "task"),
            ProviderContext("t", "p", "s", 1_000, 50),
        )


def test_skill_provider_rejects_oversized_and_escaping_files(tmp_path) -> None:
    oversized = tmp_path / "large" / "SKILL.md"
    oversized.parent.mkdir()
    oversized.write_text("x" * 40, encoding="utf-8")
    with pytest.raises(ValueError, match="size limit"):
        SkillProvider([tmp_path], max_file_bytes=16).discover()

    outside = tmp_path.parent / "outside-skill"
    outside.mkdir(exist_ok=True)
    (outside / "SKILL.md").write_text("---\nname: outside\n---\nbody", encoding="utf-8")
    escaped_directory = tmp_path / "escaped"
    escaped_directory.mkdir()
    link = escaped_directory / "SKILL.md"
    try:
        link.symlink_to(outside / "SKILL.md")
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    with pytest.raises(ValueError, match="escapes"):
        SkillProvider([tmp_path]).discover()


def test_skill_provider_can_skip_invalid_entries_for_read_only_inventory(tmp_path) -> None:
    good = tmp_path / "good" / "SKILL.md"
    good.parent.mkdir()
    good.write_text("---\nname: good\n---\nbody", encoding="utf-8")
    bad = tmp_path / "bad" / "SKILL.md"
    bad.parent.mkdir()
    bad.write_text("x" * 40, encoding="utf-8")

    manifests = SkillProvider(
        [tmp_path], max_file_bytes=32, skip_invalid=True
    ).discover()

    assert [item.identity.name for item in manifests] == ["good"]
