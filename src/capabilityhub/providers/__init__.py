"""Provider adapter contracts and built-in reference providers."""

from capabilityhub.providers.base import CapabilityProvider, ProviderContext
from capabilityhub.providers.cli import CliInvocation, CliProcessFixture, CliProcessProvider

__all__ = [
    "CapabilityProvider",
    "CliInvocation",
    "CliProcessFixture",
    "CliProcessProvider",
    "ProviderContext",
]
