"""Versioned v1alpha1 feature handshake and fail-closed compatibility decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

V1ALPHA1 = "capabilityhub.io/v1alpha1"
V1ALPHA1_FEATURES = (
    "manifest.deterministic-json-export",
    "manifest.extension-preservation",
    "manifest.explicit-migration-report",
    "security.required-features-fail-closed",
)
MINIMUM_DEPRECATION_DAYS = 180


@dataclass(frozen=True, slots=True)
class FeatureHandshake:
    api_versions: tuple[str, ...]
    supported_features: tuple[str, ...]
    required_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _unique_non_empty(self.api_versions, "api_versions")
        _unique_non_empty(self.supported_features, "supported_features")
        _unique_non_empty(self.required_features, "required_features", allow_empty=True)
        missing = set(self.required_features) - set(self.supported_features)
        if missing:
            raise ValueError("required_features must also be advertised as supported")


@dataclass(frozen=True, slots=True)
class CompatibilityDecision:
    compatible: bool
    selected_api_version: str | None
    enabled_features: tuple[str, ...]
    unsupported_client_required: tuple[str, ...]
    unsupported_server_required: tuple[str, ...]
    ignored_client_optional: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VersionLifecycle:
    """Published support dates for one transport API version."""

    version: str
    introduced_on: date
    deprecated_on: date | None = None
    sunset_on: date | None = None
    migration_target: str | None = None

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("version must be non-empty")
        if (self.deprecated_on is None) != (self.sunset_on is None):
            raise ValueError("deprecation and sunset dates must be declared together")
        if self.deprecated_on is not None and self.sunset_on is not None:
            if self.deprecated_on < self.introduced_on:
                raise ValueError("deprecation cannot precede introduction")
            if (self.sunset_on - self.deprecated_on).days < MINIMUM_DEPRECATION_DAYS:
                raise ValueError("deprecation window is shorter than policy")
            if not self.migration_target:
                raise ValueError("deprecated versions require a migration target")


@dataclass(frozen=True, slots=True)
class VersionSupportDecision:
    version: str
    accepted: bool
    deprecated: bool
    sunset: bool
    migration_target: str | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class CompatibilityPolicy:
    """Deterministic release support policy used by every client boundary."""

    versions: tuple[VersionLifecycle, ...]

    def __post_init__(self) -> None:
        if not self.versions or len({item.version for item in self.versions}) != len(
            self.versions
        ):
            raise ValueError("compatibility versions must be non-empty and unique")

    def assess(self, version: str, *, as_of: date) -> VersionSupportDecision:
        lifecycle = next((item for item in self.versions if item.version == version), None)
        if lifecycle is None or as_of < lifecycle.introduced_on:
            return VersionSupportDecision(
                version, False, False, False, None, "api_version_unsupported"
            )
        deprecated = lifecycle.deprecated_on is not None and as_of >= lifecycle.deprecated_on
        sunset = lifecycle.sunset_on is not None and as_of >= lifecycle.sunset_on
        return VersionSupportDecision(
            version,
            not sunset,
            deprecated,
            sunset,
            lifecycle.migration_target,
            "api_version_sunset"
            if sunset
            else "api_version_deprecated"
            if deprecated
            else "api_version_supported",
        )


RELEASE_COMPATIBILITY_POLICY = CompatibilityPolicy(
    (VersionLifecycle(V1ALPHA1, date(2026, 8, 11)),)
)


def v1alpha1_handshake(
    *,
    extra_supported: tuple[str, ...] = (),
    extra_required: tuple[str, ...] = (),
) -> FeatureHandshake:
    """Publish the deterministic local compatibility surface for v1alpha1."""

    supported = tuple(dict.fromkeys((*V1ALPHA1_FEATURES, *extra_supported, *extra_required)))
    return FeatureHandshake((V1ALPHA1,), supported, extra_required)


def decide_compatibility(
    client: FeatureHandshake, server: FeatureHandshake
) -> CompatibilityDecision:
    """Select a shared version and reject every unknown required semantic."""

    server_versions = set(server.api_versions)
    selected = next(
        (version for version in client.api_versions if version in server_versions), None
    )
    client_supported = set(client.supported_features)
    server_supported = set(server.supported_features)
    unsupported_client = tuple(sorted(set(client.required_features) - server_supported))
    unsupported_server = tuple(sorted(set(server.required_features) - client_supported))
    ignored_optional = tuple(
        sorted((client_supported - set(client.required_features)) - server_supported)
    )
    reasons: list[str] = []
    if selected is None:
        reasons.append("no_shared_api_version")
    if unsupported_client:
        reasons.append("unsupported_client_required_feature")
    if unsupported_server:
        reasons.append("unsupported_server_required_feature")
    compatible = not reasons
    if compatible:
        reasons.append("compatible")
    return CompatibilityDecision(
        compatible=compatible,
        selected_api_version=selected,
        enabled_features=tuple(sorted(client_supported & server_supported)) if selected else (),
        unsupported_client_required=unsupported_client,
        unsupported_server_required=unsupported_server,
        ignored_client_optional=ignored_optional,
        reason_codes=tuple(reasons),
    )


def _unique_non_empty(values: tuple[str, ...], label: str, *, allow_empty: bool = False) -> None:
    if not values and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"{label} entries must be non-empty strings")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} entries must be unique")
