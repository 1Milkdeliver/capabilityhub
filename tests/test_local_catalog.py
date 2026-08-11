from __future__ import annotations

from capabilityhub.local_catalog import discover_local_catalog
from capabilityhub.models import CapabilityKind


def test_local_catalog_discovers_skills_mcp_and_project_manifests(tmp_path) -> None:
    skill = tmp_path / ".codex" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: Demo skill\n---\nbody", encoding="utf-8")
    config = tmp_path / ".codex" / "config.toml"
    config.write_text(
        """
[mcp_servers.docs]
url = "https://secret.example/mcp?token=hidden"
[plugins."extra@personal"]
enabled = true
""",
        encoding="utf-8",
    )
    plugin_skill = (
        tmp_path
        / ".codex"
        / "plugins"
        / "cache"
        / "personal"
        / "extra"
        / "1.0.0"
        / "skills"
        / "extra"
        / "SKILL.md"
    )
    plugin_skill.parent.mkdir(parents=True)
    plugin_skill.write_text("---\nname: extra\n---\nbody", encoding="utf-8")
    project = tmp_path / "project"
    manifests = project / ".capabilityhub" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "api.json").write_text(
        """{
          "apiVersion":"capabilityhub.io/v1alpha1",
          "kind":"Capability",
          "metadata":{
            "namespace":"project",
            "name":"records",
            "version":"1.0.0",
            "digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000"
          },
          "spec":{
            "type":"api",
            "summary":"Read records",
            "provider":"fixture",
            "operations":[{"name":"read","operationType":"execute"}]
          }
        }""",
        encoding="utf-8",
    )

    catalog = discover_local_catalog(home=tmp_path, project=project)

    counts = {kind: 0 for kind in CapabilityKind}
    for manifest in catalog.manifests:
        counts[manifest.kind] += 1
        assert "secret.example" not in manifest.summary
        assert "hidden" not in manifest.summary
    assert counts[CapabilityKind.SKILL] == 2
    assert counts[CapabilityKind.MCP] == 1
    assert counts[CapabilityKind.API] == 1
