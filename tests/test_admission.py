from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from capabilityhub.admission import install_validated, validate_for_admission
from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
)
from capabilityhub.registry import CapabilityRegistry


def _manifest(kind: CapabilityKind, driver: str, config: dict[str, object]) -> CapabilityManifest:
    operation = "retrieve" if kind is CapabilityKind.RAG else "run"
    return CapabilityManifest(
        CapabilityIdentity("test", kind.value, "1", "sha256:" + kind.value[0] * 64),
        kind,
        "validated",
        driver,
        (OperationSpec(operation, OperationType.EXECUTE),),
        permissions=("filesystem.read",),
        metadata={"driver": {"name": driver, "config": config}, "extensions": {}},
    )


@pytest.mark.parametrize(
    "manifest",
    [
        _manifest(
            CapabilityKind.CLI,
            "cli-process",
            {"executable": "relative", "operations": {"run": {}}},
        ),
        _manifest(CapabilityKind.MCP, "mcp-stdio", {"command": "/bin/tool", "tools": {}}),
        _manifest(
            CapabilityKind.API,
            "http-api",
            {
                "baseUrl": "file:///bad",
                "operations": {"run": {"method": "GET", "path": "/"}},
            },
        ),
        _manifest(CapabilityKind.RAG, "local-rag", {"root": "../escape"}),
    ],
)
def test_invalid_builtin_driver_configs_fail_before_registry_mutation(
    manifest: CapabilityManifest,
) -> None:
    registry = CapabilityRegistry()
    with pytest.raises(CapabilityHubError):
        install_validated(registry, (manifest,))
    assert registry.revisions == {}
    assert registry.activations == {}


def test_unknown_required_security_and_permissions_fail_closed() -> None:
    valid = CapabilityManifest(
        CapabilityIdentity("test", "skill", "1", "sha256:" + "a" * 64),
        CapabilityKind.SKILL,
        "skill",
        "skill",
        (OperationSpec("expand", OperationType.EXPAND),),
        metadata={"extensions": {"requiredFeatures": ["security.future"]}},
    )
    with pytest.raises(CapabilityHubError, match="admission"):
        validate_for_admission((valid,))
    with pytest.raises(CapabilityHubError):
        validate_for_admission((replace(valid, permissions=("unknown.root",)),))


def test_concurrent_failed_installs_never_mutate_shared_registry(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    invalid = _manifest(
        CapabilityKind.CLI,
        "cli-process",
        {"executable": "relative", "operations": {"run": {}}},
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _attempt(registry, invalid, tmp_path), range(32)))
    assert results == [False] * 32
    assert registry.revisions == {}


def _attempt(registry: CapabilityRegistry, manifest: CapabilityManifest, project: Path) -> bool:
    try:
        install_validated(registry, (manifest,), project=project)
    except CapabilityHubError:
        return False
    return True
