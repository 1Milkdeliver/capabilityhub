"""Deterministic, side-effect-free provider for tests and local fixtures."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import measure_text
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
)
from capabilityhub.providers.base import ProviderContext


@dataclass(frozen=True, slots=True)
class StaticFixture:
    """A manifest and its fixed operation outputs.

    Outputs are data, not callbacks: executing a fixture cannot start a process,
    reach a network, or otherwise create a side effect.
    """

    manifest: CapabilityManifest
    outputs: Mapping[str, JsonValue] = field(default_factory=dict)


class StaticProvider:
    """Reference provider that returns declared fixture values deterministically."""

    def __init__(
        self, fixtures: tuple[StaticFixture, ...] | list[StaticFixture], *, name: str = "static"
    ) -> None:
        self._name = name
        self._fixtures = tuple(fixtures)
        revisions = [fixture.manifest.identity.revision for fixture in self._fixtures]
        if len(revisions) != len(set(revisions)):
            raise ValueError("static fixture revisions must be unique")
        self._by_revision = {
            fixture.manifest.identity.revision: fixture for fixture in self._fixtures
        }

    @property
    def name(self) -> str:
        return self._name

    def discover(self) -> tuple[CapabilityManifest, ...]:
        return tuple(fixture.manifest for fixture in self._fixtures)

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        del context  # Fixture execution is deliberately independent of the host environment.
        fixture = self._by_revision.get(identity.revision)
        if fixture is None:
            raise CapabilityHubError(
                code="static_capability_not_found",
                category=ErrorCategory.REFERENCE,
                safe_message="The requested static capability is not registered.",
            )
        if fixture.manifest.operation(request.operation) is None:
            raise CapabilityHubError(
                code="static_operation_not_found",
                category=ErrorCategory.REFERENCE,
                safe_message="The requested operation is not declared by the capability.",
            )
        if request.operation not in fixture.outputs:
            raise CapabilityHubError(
                code="static_fixture_output_missing",
                category=ErrorCategory.PROVIDER,
                safe_message="The static fixture has no output for the requested operation.",
            )
        audit_material = json.dumps(
            {
                "revision": identity.revision,
                "operation": request.operation,
                "arguments": dict(request.arguments),
                "task_id": request.task_id,
                "idempotency_key": request.idempotency_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        serialized_output = json.dumps(
            fixture.outputs[request.operation],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ExecutionResult(
            capability_revision=identity.revision,
            operation=request.operation,
            output=fixture.outputs[request.operation],
            provider=self.name,
            portable_tokens=measure_text(serialized_output).portable_tokens,
            audit_id=f"static-{sha256(audit_material).hexdigest()[:16]}",
        )


StaticCapabilityProvider = StaticProvider
