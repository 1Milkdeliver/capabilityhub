from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.runtime import (
    local_approval_decide,
    local_approval_request,
    local_approvals,
    local_execute,
)


def _manifest(executable: str, *, requires_approval: bool = False) -> dict[str, object]:
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
            "operations": [
                {
                    "name": "run",
                    "operationType": "execute",
                    "requiresApproval": requires_approval,
                }
            ],
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


def test_configured_execution_consumes_one_durable_exact_approval(tmp_path: Path) -> None:
    document = _manifest(sys.executable, requires_approval=True)
    root = tmp_path / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    (root / "cli.json").write_text(json.dumps(document), encoding="utf-8")
    monitor = LocalCatalogMonitor(project=tmp_path, home=tmp_path / "home")
    revision = "project/configured-cli@1.0.0#sha256:" + ("a" * 64)

    requested = local_approval_request(revision, "run", {}, monitor=monitor)
    approval_id = str(requested["approval_id"])
    assert local_approvals(tmp_path, status="pending")["count"] == 1
    local_approval_decide(approval_id, "approve", project_root=tmp_path)

    result = local_execute(
        revision,
        "run",
        {},
        approval_id=approval_id,
        project_root=tmp_path,
        monitor=monitor,
    )

    assert result["output"] == {"ok": True}
    assert local_approvals(tmp_path, status="consumed")["count"] == 1
    with pytest.raises(CapabilityHubError) as replay:
        local_execute(
            revision,
            "run",
            {},
            approval_id=approval_id,
            project_root=tmp_path,
            monitor=monitor,
        )
    assert replay.value.code == "approval_already_consumed"
