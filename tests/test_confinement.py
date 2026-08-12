from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from capabilityhub.confinement import ConfinementBackend, confinement_status
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import (
    CapabilityIdentity,
    ExecutionRequest,
    ExecutionResult,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.supervision import ProcessProviderSupervisor, WorkerResourceLimits

IDENTITY = CapabilityIdentity("test", "malicious", "1", "sha256:" + "9" * 64)
REQUEST = ExecutionRequest("malicious-execution", "attack", {}, "task")
CONTEXT = ProviderContext("tenant", "principal", "session", 2_000, 100)


class _MaliciousProvider:
    name = "malicious-fixture"

    def __init__(self, marker: str, child_marker: str, port: int) -> None:
        self.marker = marker
        self.child_marker = child_marker
        self.port = port

    def discover(self):
        return ()

    def execute(self, identity, request, context):
        del context
        Path(self.marker).write_text("filesystem escaped", encoding="utf-8")
        with socket.create_connection(("127.0.0.1", self.port), timeout=1):
            pass
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('child')",
                self.child_marker,
            ]
        ).wait(timeout=2)
        return ExecutionResult(
            identity.revision,
            request.operation,
            {"unexpected": True},
            self.name,
            1,
            "malicious-audit",
        )


def test_production_confinement_denies_before_malicious_provider_spawn(tmp_path: Path) -> None:
    marker = tmp_path / "outside.txt"
    child_marker = tmp_path / "child.txt"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    port = int(listener.getsockname()[1])
    supervisor = ProcessProviderSupervisor(
        resource_limits=WorkerResourceLimits(
            require_filesystem_isolation=True,
            require_network_isolation=True,
        )
    )
    try:
        with pytest.raises(CapabilityHubError) as caught:
            supervisor.execute(
                _MaliciousProvider(str(marker), str(child_marker), port),
                IDENTITY,
                REQUEST,
                CONTEXT,
            )
        with pytest.raises(TimeoutError):
            listener.accept()
    finally:
        listener.close()

    assert caught.value.code == "provider_os_confinement_unavailable"
    assert caught.value.category is ErrorCategory.POLICY
    assert marker.exists() is False
    assert child_marker.exists() is False
    assert supervisor.active_count() == 0


def test_platform_backend_is_explicit_and_does_not_overclaim() -> None:
    status = confinement_status()

    expected = (
        ConfinementBackend.WINDOWS_JOB_ONLY
        if os.name == "nt"
        else ConfinementBackend.POSIX_RESOURCE_ONLY
        if os.name == "posix" and platform.system() == "Linux"
        else ConfinementBackend.UNSUPPORTED_PLATFORM
    )
    assert status.backend is expected
    if expected is not ConfinementBackend.POSIX_RESOURCE_ONLY:
        assert status.filesystem is False
        assert status.network is False
    assert status.reason_code
    assert "path" not in repr(status).lower()
