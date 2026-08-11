from __future__ import annotations

from capabilityhub.local_catalog import discover_local_catalog, local_catalog_fingerprint
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
    (manifests / "notes.yaml").write_text(
        """
apiVersion: capabilityhub.io/v1alpha1
kind: Capability
metadata:
  namespace: project
  name: notes
  version: 1.0.0
  digest: sha256:1111111111111111111111111111111111111111111111111111111111111111
spec:
  type: rag
  summary: Read project notes
  provider: fixture
  operations:
    - name: retrieve
""".strip(),
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
    assert counts[CapabilityKind.CLI] == 1
    assert counts[CapabilityKind.API] == 1
    assert counts[CapabilityKind.RAG] == 1
    cli = next(item for item in catalog.manifests if item.kind is CapabilityKind.CLI)
    assert cli.identity.coordinate == "capabilityhub/cli"
    assert {operation.name for operation in cli.operations} == {
        "validate",
        "export-manifest",
        "import-openapi",
        "migrate-manifest",
        "compatibility",
        "activation-lock",
        "discover-skills",
        "inventory",
        "search",
        "health",
        "connections",
        "loaded",
        "providers",
        "routing",
        "language",
        "lifecycle",
        "updates",
        "audit",
        "secure-audit",
        "load",
        "execute",
        "approvals",
        "context",
        "reasoning",
        "budget-report",
        "benchmark",
        "dashboard",
        "http-serve",
        "mcp-serve",
    }


def test_local_catalog_uses_stable_sources_and_reports_safe_exclusions(tmp_path) -> None:
    home = tmp_path / "home"
    user_skill = home / ".codex" / "skills" / "shared" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("---\nname: shared\n---\nuser body", encoding="utf-8")
    invalid = home / ".codex" / "skills" / "invalid" / "SKILL.md"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("---\nname: invalid\n---\n" + "x" * 300_000, encoding="utf-8")
    config = home / ".codex" / "config.toml"
    config.write_text("[mcp_servers.offline]\nenabled = false\ncommand = 'secret-value'\n")

    project = tmp_path / "project"
    project_skill = project / ".codex" / "skills" / "shared" / "SKILL.md"
    project_skill.parent.mkdir(parents=True)
    project_skill.write_text("---\nname: shared\n---\nproject body", encoding="utf-8")

    first_fingerprint = local_catalog_fingerprint(home=home, project=project)
    catalog = discover_local_catalog(home=home, project=project)
    skills = [item for item in catalog.manifests if item.kind is CapabilityKind.SKILL]

    assert [item.identity.coordinate for item in skills] == ["codex-project/shared"]
    assert catalog.conflict_count == 1
    assert catalog.invalid_count == 1
    assert catalog.skipped_count == 0
    assert "codex-mcp/offline" in catalog.inactive_coordinates
    assert all("secret-value" not in item.summary for item in catalog.manifests)

    project_skill.write_text("---\nname: shared\n---\nproject body changed", encoding="utf-8")
    assert local_catalog_fingerprint(home=home, project=project) != first_fingerprint
