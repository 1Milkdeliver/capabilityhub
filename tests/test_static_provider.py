from __future__ import annotations

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    OperationSpec,
    OperationType,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.static import StaticFixture, StaticProvider


def _context() -> ProviderContext:
    return ProviderContext("tenant", "principal", "session", 1_000, 200)


@pytest.mark.parametrize("kind", list(CapabilityKind))
def test_static_provider_executes_deterministic_fixtures_for_every_kind(
    kind: CapabilityKind,
) -> None:
    identity = CapabilityIdentity("fixtures", kind.value, "1", f"digest-{kind.value}")
    manifest = CapabilityManifest(
        identity=identity,
        kind=kind,
        summary=f"A {kind.value} fixture.",
        provider="static",
        operations=(OperationSpec("run", OperationType.EXECUTE),),
    )
    provider = StaticProvider([StaticFixture(manifest, {"run": {"kind": kind.value}})])
    request = ExecutionRequest(identity.revision, "run", {"ignored": True}, "task-1")

    first = provider.execute(identity, request, _context())
    second = provider.execute(identity, request, _context())

    assert provider.discover() == (manifest,)
    assert first.output == {"kind": kind.value}
    assert first.audit_id == second.audit_id
    assert first.provider == "static"


def test_static_provider_rejects_unknown_operations() -> None:
    identity = CapabilityIdentity("fixtures", "one", "1", "digest")
    manifest = CapabilityManifest(
        identity=identity,
        kind=CapabilityKind.API,
        summary="fixture",
        provider="static",
        operations=(OperationSpec("get", OperationType.EXECUTE),),
    )
    provider = StaticProvider([StaticFixture(manifest, {"get": "ok"})])
    request = ExecutionRequest(identity.revision, "delete", {}, "task-1")

    with pytest.raises(CapabilityHubError, match="not declared"):
        provider.execute(identity, request, _context())
