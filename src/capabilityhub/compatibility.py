"""Versioned v1alpha1 feature handshake and fail-closed compatibility decisions."""

from __future__ import annotations

from dataclasses import dataclass

V1ALPHA1 = "capabilityhub.io/v1alpha1"
V1ALPHA1_FEATURES = (
    "manifest.deterministic-json-export",
    "manifest.extension-preservation",
    "manifest.explicit-migration-report",
    "security.required-features-fail-closed",
)


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
