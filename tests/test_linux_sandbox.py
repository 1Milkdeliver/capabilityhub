from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from capabilityhub.linux_sandbox import probe_linux_sandbox
from capabilityhub.models import CapabilityIdentity, ExecutionRequest, ExecutionResult
from capabilityhub.providers.base import ProviderContext
from capabilityhub.supervision import ProcessProviderSupervisor, WorkerResourceLimits

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel sandbox test")

IDENTITY = CapabilityIdentity("test", "linux-sandbox", "1", "sha256:" + "8" * 64)
REQUEST = ExecutionRequest("linux-sandbox-execution", "attack", {}, "task")
CONTEXT = ProviderContext("tenant", "principal", "session", 10_000, 1_000)


class _LinuxMaliciousProvider:
    name = "linux-malicious-fixture"

    def __init__(self, allowed: str, outside: str) -> None:
        self.allowed = allowed
        self.outside = outside

    def discover(self):
        return ()

    def execute(self, identity, request, context):
        del context
        allowed = Path(self.allowed)
        outside = Path(self.outside)
        (allowed / "provider.txt").write_text("allowed", encoding="utf-8")
        outside_denied = _write_denied(outside / "provider.txt")
        network_denied = _network_denied()
        child_result = allowed / "child.json"
        script = """
import json
import socket
import sys
from pathlib import Path

allowed = Path(sys.argv[1])
outside = Path(sys.argv[2])
(allowed / "child.txt").write_text("allowed", encoding="utf-8")
filesystem_denied = False
network_denied = False
try:
    (outside / "child.txt").write_text("escape", encoding="utf-8")
except PermissionError:
    filesystem_denied = True
try:
    socket.socket()
except PermissionError:
    network_denied = True
(allowed / "child.json").write_text(
    json.dumps({"filesystem": filesystem_denied, "network": network_denied}),
    encoding="utf-8",
)
"""
        subprocess.run(
            (sys.executable, "-c", script, str(allowed), str(outside)),
            check=True,
            timeout=5,
        )
        child = json.loads(child_result.read_text(encoding="utf-8"))
        return ExecutionResult(
            identity.revision,
            request.operation,
            {
                "allowed": (allowed / "provider.txt").is_file(),
                "filesystem_denied": outside_denied,
                "network_denied": network_denied,
                "child_filesystem_denied": child["filesystem"],
                "child_network_denied": child["network"],
            },
            self.name,
            16,
            "linux-sandbox-audit",
        )


def _write_denied(path: Path) -> bool:
    try:
        path.write_text("escape", encoding="utf-8")
    except PermissionError:
        return True
    return False


def _network_denied() -> bool:
    try:
        socket.socket()
    except PermissionError:
        return True
    return False


def test_linux_landlock_and_seccomp_confine_provider_and_descendant(tmp_path: Path) -> None:
    capabilities = probe_linux_sandbox()
    assert capabilities.filesystem, capabilities.reason_code
    assert capabilities.network, capabilities.reason_code
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    supervisor = ProcessProviderSupervisor(
        resource_limits=WorkerResourceLimits(
            require_filesystem_isolation=True,
            require_network_isolation=True,
            filesystem_root=str(allowed.resolve()),
        )
    )

    result = supervisor.execute(
        _LinuxMaliciousProvider(str(allowed), str(outside)),
        IDENTITY,
        REQUEST,
        CONTEXT,
    )

    assert result.output == {
        "allowed": True,
        "filesystem_denied": True,
        "network_denied": True,
        "child_filesystem_denied": True,
        "child_network_denied": True,
    }
    assert (allowed / "child.txt").read_text(encoding="utf-8") == "allowed"
    assert list(outside.iterdir()) == []
