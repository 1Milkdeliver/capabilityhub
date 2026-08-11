from __future__ import annotations

import json
import sys
from pathlib import Path

from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.runtime import local_execute


def _manifest(executable: str) -> dict[str, object]:
    return {
        "apiVersion": "capabilityhub.io/v1alpha1",
        "kind": "Capability",
        "metadata": {
            "namespace": "project",
            "name": "configured-cli",
            "version": "1.0.0",
            "digest": "sha256:" + ("a" * 64),
        },
        "spec": {
            "type": "cli",
            "summary": "A project-configured CLI used by the integration test.",
            "driver": {
                "name": "cli-process",
                "config": {
                    "executable": executable,
                    "operations": {
                        "run": {
                            "argv": [
                                "-c",
                                "import json; print(json.dumps({'ok': True}))",
                            ],
                            "output": "json",
                        }
                    },
                },
            },
            "operations": [{"name": "run", "operationType": "execute"}],
        },
    }


def test_project_driver_is_discovered_and_executed(tmp_path: Path) -> None:
    root = tmp_path / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    (root / "cli.json").write_text(json.dumps(_manifest(sys.executable)), encoding="utf-8")
    monitor = LocalCatalogMonitor(project=tmp_path, home=tmp_path / "home")

    generation = monitor.snapshot(force=True)
    revision = "project/configured-cli@1.0.0#sha256:" + ("a" * 64)
    assert generation.inventory["active_by_kind"]["cli"] == 2  # type: ignore[index]

    result = local_execute(revision, "run", {}, project_root=tmp_path, monitor=monitor)

    assert result["provider"] == "cli-process"
    assert result["output"] == {"ok": True}


def test_invalid_driver_is_counted_but_never_wired(tmp_path: Path) -> None:
    root = tmp_path / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    (root / "cli.json").write_text(json.dumps(_manifest("relative-command")), encoding="utf-8")

    generation = LocalCatalogMonitor(project=tmp_path, home=tmp_path / "home").snapshot(force=True)

    assert generation.inventory["invalid_count"] == 1
    assert generation.providers == ()
