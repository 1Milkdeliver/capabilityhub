"""Deterministic automatic conflict projections for inert capability manifests."""

from __future__ import annotations

import hashlib
import json
import ntpath
import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import urlsplit

from .errors import CapabilityHubError, ErrorCategory
from .models import CapabilityManifest

ProjectionStrategy = Literal["deny", "namespace", "isolate", "select-one"]
ProjectionAction = Literal["allow", "namespace", "isolate", "selected", "excluded"]
_NAMESPACEABLE = frozenset(("identity", "projection_name", "permission_namespace"))
_HTTP_METHODS = frozenset(("GET", "POST", "PUT", "PATCH", "DELETE"))


class ProjectionError(CapabilityHubError):
    """Safe projection failure containing no URL or filesystem path."""

    def __init__(self, code: str, *, claim_type: str | None = None) -> None:
        details: dict[str, object] = {}
        if claim_type is not None:
            details["claim_type"] = claim_type
        super().__init__(
            code=code,
            category=ErrorCategory.CONFLICT,
            safe_message="Capability resource projections could not be resolved safely.",
            retryable=False,
            details=details,
        )


@dataclass(frozen=True, slots=True)
class ProjectionPolicy:
    strategy: ProjectionStrategy = "deny"
    selected_coordinate: str | None = None

    def __post_init__(self) -> None:
        if self.strategy not in ("deny", "namespace", "isolate", "select-one"):
            raise ValueError("unknown projection conflict strategy")
        if self.strategy == "select-one":
            if not self.selected_coordinate:
                raise ValueError("select-one requires an explicit coordinate")
        elif self.selected_coordinate is not None:
            raise ValueError("selected_coordinate is valid only for select-one")


@dataclass(frozen=True, slots=True)
class ProjectionClaim:
    claim_type: str
    resource_id: str
    coordinate: str
    revision: str
    kind: str


@dataclass(frozen=True, slots=True)
class ProjectionCollision:
    claim_type: str
    resource_id: str
    coordinates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectionDecision:
    claim: ProjectionClaim
    action: ProjectionAction
    effective_resource_id: str


@dataclass(frozen=True, slots=True)
class ProjectionResolution:
    strategy: ProjectionStrategy
    claims: tuple[ProjectionClaim, ...]
    collisions: tuple[ProjectionCollision, ...]
    decisions: tuple[ProjectionDecision, ...]


def extract_projection_claims(manifest: CapabilityManifest) -> tuple[ProjectionClaim, ...]:
    """Extract normalized claims without interpreting or executing driver material."""

    coordinate = manifest.identity.coordinate
    values: set[tuple[str, str]] = {
        ("identity", coordinate.casefold()),
        *(("projection_name", operation.name.casefold()) for operation in manifest.operations),
        *(("permission_namespace", permission.casefold()) for permission in manifest.permissions),
    }
    driver = manifest.metadata.get("driver")
    if driver is not None:
        if not isinstance(driver, Mapping):
            raise ProjectionError("invalid_projection_driver")
        name = driver.get("name")
        config = driver.get("config", {})
        if not isinstance(name, str) or not name or not isinstance(config, Mapping):
            raise ProjectionError("invalid_projection_driver")
        values.update(_driver_claims(name, cast(Mapping[str, object], config)))
    return tuple(
        ProjectionClaim(
            claim_type=claim_type,
            resource_id=_resource_id(claim_type, normalized),
            coordinate=coordinate,
            revision=manifest.identity.revision,
            kind=manifest.kind.value,
        )
        for claim_type, normalized in sorted(values)
    )


def resolve_projections(
    manifests: Iterable[CapabilityManifest],
    policy: ProjectionPolicy | None = None,
) -> ProjectionResolution:
    """Detect cross-coordinate collisions and apply one deterministic strategy."""

    selected_policy = policy or ProjectionPolicy()
    ordered_manifests = sorted(
        manifests,
        key=lambda item: (item.identity.coordinate, item.identity.revision),
    )
    claims = tuple(
        sorted(
            (
                claim
                for manifest in ordered_manifests
                for claim in extract_projection_claims(manifest)
            ),
            key=_claim_order,
        )
    )
    groups: dict[tuple[str, str], list[ProjectionClaim]] = {}
    for claim in claims:
        groups.setdefault((claim.claim_type, claim.resource_id), []).append(claim)
    collisions = tuple(
        ProjectionCollision(claim_type, resource_id, coordinates)
        for (claim_type, resource_id), grouped in sorted(groups.items())
        if len(coordinates := tuple(sorted({claim.coordinate for claim in grouped}))) > 1
    )
    if collisions and selected_policy.strategy == "deny":
        raise ProjectionError("projection_conflict", claim_type=collisions[0].claim_type)
    collision_keys = {(item.claim_type, item.resource_id) for item in collisions}
    if selected_policy.strategy == "namespace":
        blocked = next(
            (item for item in collisions if item.claim_type not in _NAMESPACEABLE),
            None,
        )
        if blocked is not None:
            raise ProjectionError(
                "projection_not_namespaceable",
                claim_type=blocked.claim_type,
            )
    if selected_policy.strategy == "select-one":
        assert selected_policy.selected_coordinate is not None
        if any(selected_policy.selected_coordinate not in item.coordinates for item in collisions):
            raise ProjectionError("selected_coordinate_not_claimant")

    decisions: list[ProjectionDecision] = []
    for claim in claims:
        key = (claim.claim_type, claim.resource_id)
        if key not in collision_keys:
            action: ProjectionAction = "allow"
            effective = claim.resource_id
        elif selected_policy.strategy == "namespace":
            action = "namespace"
            effective = _derived_resource_id("namespace", claim)
        elif selected_policy.strategy == "isolate":
            action = "isolate"
            effective = _derived_resource_id("isolate", claim)
        else:
            selected = claim.coordinate == selected_policy.selected_coordinate
            action = "selected" if selected else "excluded"
            effective = claim.resource_id if selected else _derived_resource_id("excluded", claim)
        decisions.append(ProjectionDecision(claim, action, effective))
    return ProjectionResolution(selected_policy.strategy, claims, collisions, tuple(decisions))


def _driver_claims(name: str, config: Mapping[str, object]) -> set[tuple[str, str]]:
    claims: set[tuple[str, str]] = set()
    port = config.get("port")
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            raise ProjectionError("invalid_projected_port")
        claims.add(("port", str(port)))
    root = config.get("root")
    if root is not None:
        if not isinstance(root, str) or not root:
            raise ProjectionError("invalid_projected_filesystem_root")
        claims.add(("filesystem_root", _normalize_path(root)))
    if name == "http-api":
        claims.update(_http_claims(config))
    return claims


def _http_claims(config: Mapping[str, object]) -> set[tuple[str, str]]:
    base_url = config.get("baseUrl")
    operations = config.get("operations")
    if not isinstance(base_url, str) or not isinstance(operations, Mapping):
        raise ProjectionError("invalid_projected_http_route")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except ValueError as error:
        raise ProjectionError("invalid_projected_http_route") from error
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ProjectionError("invalid_projected_http_route")
    host = parsed.hostname.casefold().encode("idna").decode("ascii")
    base_path = parsed.path or "/"
    claims: set[tuple[str, str]] = set()
    for raw in operations.values():
        if not isinstance(raw, Mapping):
            raise ProjectionError("invalid_projected_http_route")
        method = raw.get("method")
        path = raw.get("path")
        if (
            not isinstance(method, str)
            or method.upper() not in _HTTP_METHODS
            or not isinstance(path, str)
            or not path.startswith("/")
            or "://" in path
            or "?" in path
        ):
            raise ProjectionError("invalid_projected_http_route")
        route_path = posixpath.normpath(base_path.rstrip("/") + "/" + path.lstrip("/"))
        normalized = json.dumps(
            [parsed.scheme.casefold(), host, port, method.upper(), route_path],
            separators=(",", ":"),
        )
        claims.add(("http_route", normalized))
    return claims


def _normalize_path(value: str) -> str:
    if "\\" in value or (len(value) >= 2 and value[1] == ":"):
        return ntpath.normcase(ntpath.normpath(value))
    return posixpath.normpath(value)


def _resource_id(claim_type: str, normalized: str) -> str:
    encoded = f"capabilityhub-projection-v1\0{claim_type}\0{normalized}".encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _derived_resource_id(action: str, claim: ProjectionClaim) -> str:
    return _resource_id(
        f"{action}:{claim.claim_type}",
        f"{claim.resource_id}\0{claim.coordinate}",
    )


def _claim_order(claim: ProjectionClaim) -> tuple[str, str, str, str]:
    return claim.claim_type, claim.resource_id, claim.coordinate, claim.revision
