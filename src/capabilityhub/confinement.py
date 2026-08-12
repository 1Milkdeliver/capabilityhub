"""OS confinement capability detection and fail-closed execution admission."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from enum import StrEnum

from capabilityhub.errors import CapabilityHubError, ErrorCategory


class ConfinementBackend(StrEnum):
    WINDOWS_JOB_ONLY = "windows-job-only"
    POSIX_RESOURCE_ONLY = "posix-resource-only"
    UNSUPPORTED_PLATFORM = "unsupported-platform"


@dataclass(frozen=True, slots=True)
class ConfinementStatus:
    backend: ConfinementBackend
    filesystem: bool
    network: bool
    process_tree: bool
    reason_code: str


def confinement_status() -> ConfinementStatus:
    """Report only boundaries the current supervisor can actually enforce."""

    if os.name == "nt":
        return ConfinementStatus(
            ConfinementBackend.WINDOWS_JOB_ONLY,
            filesystem=False,
            network=False,
            process_tree=True,
            reason_code="windows_job_has_no_fs_network_boundary",
        )
    if os.name == "posix" and platform.system() in {"Linux", "Darwin"}:
        return ConfinementStatus(
            ConfinementBackend.POSIX_RESOURCE_ONLY,
            filesystem=False,
            network=False,
            process_tree=True,
            reason_code="posix_limits_have_no_fs_network_boundary",
        )
    return ConfinementStatus(
        ConfinementBackend.UNSUPPORTED_PLATFORM,
        filesystem=False,
        network=False,
        process_tree=False,
        reason_code="os_confinement_backend_unavailable",
    )


def require_confinement(*, filesystem: bool, network: bool) -> ConfinementStatus:
    """Fail before provider creation unless every requested boundary is real."""

    status = confinement_status()
    missing = (filesystem and not status.filesystem) or (network and not status.network)
    if missing:
        raise CapabilityHubError(
            code="provider_os_confinement_unavailable",
            category=ErrorCategory.POLICY,
            safe_message="The required provider OS confinement boundary is unavailable.",
            details={
                "backend": status.backend.value,
                "filesystem_required": filesystem,
                "network_required": network,
                "reason_code": status.reason_code,
            },
        )
    return status
