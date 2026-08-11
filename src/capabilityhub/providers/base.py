"""Capability provider protocol.

Real gateways and runtimes remain external. Adapters implement this narrow boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
)


@dataclass(frozen=True, slots=True)
class ProviderContext:
    tenant_id: str
    principal_id: str
    session_id: str
    deadline_ms: int
    max_output_tokens: int


class CapabilityProvider(Protocol):
    @property
    def name(self) -> str: ...

    def discover(self) -> tuple[CapabilityManifest, ...]: ...

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult: ...

