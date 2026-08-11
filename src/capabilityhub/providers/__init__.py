"""Provider adapter contracts and built-in reference providers."""

from capabilityhub.providers.base import CapabilityProvider, ProviderContext
from capabilityhub.providers.cli import CliInvocation, CliProcessFixture, CliProcessProvider
from capabilityhub.providers.http import HttpApiFixture, HttpApiProvider, HttpInvocation

__all__ = [
    "CapabilityProvider",
    "CliInvocation",
    "CliProcessFixture",
    "CliProcessProvider",
    "HttpApiFixture",
    "HttpApiProvider",
    "HttpInvocation",
    "ProviderContext",
]
