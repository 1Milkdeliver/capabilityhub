from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import ClassVar

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


class _ConfiguredApiHandler(BaseHTTPRequestHandler):
    authorizations: ClassVar[list[str | None]] = []

    def do_GET(self) -> None:
        type(self).authorizations.append(self.headers.get("Authorization"))
        payload = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _configured_api() -> Iterator[str]:
    _ConfiguredApiHandler.authorizations = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ConfiguredApiHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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


def test_local_execute_runs_configured_cli_in_spawned_worker(tmp_path: Path) -> None:
    document = _manifest(sys.executable)
    config = document["spec"]["driver"]["config"]  # type: ignore[index]
    config["operations"]["run"]["argv"] = [  # type: ignore[index]
        "-c",
        "import json, os; print(json.dumps({'pid': os.getpid()}))",
    ]
    root = tmp_path / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    (root / "cli.json").write_text(json.dumps(document), encoding="utf-8")
    monitor = LocalCatalogMonitor(project=tmp_path, home=tmp_path / "home")
    revision = "project/configured-cli@1.0.0#sha256:" + ("a" * 64)

    result = local_execute(revision, "run", {}, monitor=monitor)

    assert result["output"]["pid"] != os.getpid()  # type: ignore[index]


def test_local_execute_reuses_durable_result_without_second_cli_side_effect(
    tmp_path: Path,
) -> None:
    counter = tmp_path / "provider-calls.txt"
    document = _manifest(sys.executable)
    config = document["spec"]["driver"]["config"]  # type: ignore[index]
    config["operations"]["run"]["argv"] = [  # type: ignore[index]
        "-c",
        (
            "import json, pathlib, sys; p=pathlib.Path(sys.argv[1]); "
            "p.write_text((p.read_text() if p.exists() else '')+'x'); "
            "print(json.dumps({'ok': True}))"
        ),
        str(counter),
    ]
    root = tmp_path / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    (root / "cli.json").write_text(json.dumps(document), encoding="utf-8")
    monitor = LocalCatalogMonitor(project=tmp_path, home=tmp_path / "home")
    revision = "project/configured-cli@1.0.0#sha256:" + ("a" * 64)

    first = local_execute(
        revision, "run", {}, monitor=monitor, idempotency_key="durable-local-key"
    )
    replay = local_execute(
        revision, "run", {}, monitor=monitor, idempotency_key="durable-local-key"
    )

    assert replay["output"] == first["output"] == {"ok": True}
    assert counter.read_text(encoding="utf-8") == "x"


def test_concurrent_local_execute_calls_use_independent_workers(tmp_path: Path) -> None:
    document = _manifest(sys.executable)
    config = document["spec"]["driver"]["config"]  # type: ignore[index]
    config["operations"]["run"]["argv"] = [  # type: ignore[index]
        "-c",
        "import json, os, time; time.sleep(.2); print(json.dumps({'pid': os.getpid()}))",
    ]
    root = tmp_path / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    (root / "cli.json").write_text(json.dumps(document), encoding="utf-8")
    monitor = LocalCatalogMonitor(
        project=tmp_path,
        home=tmp_path / "home",
        refresh_interval_seconds=60,
    )
    monitor.snapshot(force=True)
    revision = "project/configured-cli@1.0.0#sha256:" + ("a" * 64)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: local_execute(revision, "run", {}, monitor=monitor),
                range(2),
            )
        )

    pids = {result["output"]["pid"] for result in results}  # type: ignore[index]
    assert len(pids) == 2
    assert os.getpid() not in pids


def test_local_execute_hard_deadline_terminates_worker(tmp_path: Path) -> None:
    document = _manifest(sys.executable)
    config = document["spec"]["driver"]["config"]  # type: ignore[index]
    config["operations"]["run"]["argv"] = [  # type: ignore[index]
        "-c",
        "import time; time.sleep(5)",
    ]
    root = tmp_path / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    (root / "cli.json").write_text(json.dumps(document), encoding="utf-8")
    monitor = LocalCatalogMonitor(project=tmp_path, home=tmp_path / "home")
    revision = "project/configured-cli@1.0.0#sha256:" + ("a" * 64)
    started = monotonic()

    with pytest.raises(CapabilityHubError) as caught:
        local_execute(revision, "run", {}, monitor=monitor, deadline_ms=100)

    assert caught.value.code == "provider_worker_timeout"
    assert monotonic() - started < 2


def test_invalid_driver_is_counted_but_never_wired(tmp_path: Path) -> None:
    root = tmp_path / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    (root / "cli.json").write_text(json.dumps(_manifest("relative-command")), encoding="utf-8")

    generation = LocalCatalogMonitor(project=tmp_path, home=tmp_path / "home").snapshot(force=True)

    assert generation.inventory["invalid_count"] == 1
    assert generation.providers == ()


@pytest.mark.skipif(os.name != "nt", reason="encrypted worker secrets require Windows DPAPI")
def test_cli_environment_alias_is_injected_only_into_spawned_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "CHILD-ENV-CANARY-83"
    monkeypatch.setenv("PRIVATE_CHILD_TOKEN", secret)
    document = _manifest(sys.executable)
    config = document["spec"]["driver"]["config"]  # type: ignore[index]
    config["environmentFrom"] = {"TOKEN": "PRIVATE_CHILD_TOKEN"}  # type: ignore[index]
    config["operations"]["run"]["argv"] = [  # type: ignore[index]
        "-c",
        "import json, os; print(json.dumps({'has_token': bool(os.environ.get('TOKEN'))}))",
    ]
    root = tmp_path / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    (root / "cli.json").write_text(json.dumps(document), encoding="utf-8")

    monitor = LocalCatalogMonitor(
        project=tmp_path,
        home=tmp_path / "home",
    )
    generation = monitor.snapshot(force=True)
    revision = "project/configured-cli@1.0.0#sha256:" + ("a" * 64)
    result = local_execute(revision, "run", {}, monitor=monitor)

    assert generation.inventory["invalid_count"] == 0
    assert result["output"] == {"has_token": True}
    assert secret not in repr(generation.inventory)
    assert secret not in repr(result)


@pytest.mark.skipif(os.name != "nt", reason="encrypted worker secrets require Windows DPAPI")
def test_configured_http_alias_crosses_encrypted_worker_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "RUNTIME-BROKER-CANARY-31"
    monkeypatch.setenv("CAPABILITYHUB_API_TOKEN", secret)
    with _configured_api() as base_url:
        document = {
            "apiVersion": "capabilityhub.io/v1alpha1",
            "kind": "Capability",
            "metadata": {
                "namespace": "project",
                "name": "configured-api",
                "version": "1.0.0",
                "digest": "sha256:" + ("c" * 64),
            },
            "spec": {
                "type": "api",
                "summary": "Configured HTTP API with a brokered header alias.",
                "driver": {
                    "name": "http-api",
                    "config": {
                        "baseUrl": base_url,
                        "headerEnvironment": {
                            "Authorization": "CAPABILITYHUB_API_TOKEN"
                        },
                        "operations": {"read": {"method": "GET", "path": "/safe"}},
                    },
                },
                "operations": [
                    {
                        "name": "read",
                        "operationType": "execute",
                        "requiresApproval": False,
                    }
                ],
            },
        }
        root = tmp_path / ".capabilityhub" / "manifests"
        root.mkdir(parents=True)
        (root / "api.json").write_text(json.dumps(document), encoding="utf-8")
        monitor = LocalCatalogMonitor(project=tmp_path, home=tmp_path / "home")
        revision = "project/configured-api@1.0.0#sha256:" + ("c" * 64)

        result = local_execute(revision, "read", {}, monitor=monitor)

    assert result["output"] == {"ok": True}
    assert _ConfiguredApiHandler.authorizations == [secret]
    assert secret not in repr(result)


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
