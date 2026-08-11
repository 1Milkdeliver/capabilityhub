from __future__ import annotations

import pytest

from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
)
from capabilityhub.projections import (
    ProjectionError,
    ProjectionPolicy,
    extract_projection_claims,
    resolve_projections,
)


def _manifest(
    name: str,
    digest: str,
    *,
    kind: CapabilityKind = CapabilityKind.API,
    operation: str = "search",
    permissions: tuple[str, ...] = ("network",),
    driver_name: str | None = None,
    config: dict[str, object] | None = None,
) -> CapabilityManifest:
    metadata = (
        {} if driver_name is None else {"driver": {"name": driver_name, "config": config or {}}}
    )
    return CapabilityManifest(
        CapabilityIdentity("demo", name, "1.0.0", "sha256:" + digest * 64),
        kind,
        "Projection fixture.",
        driver_name or "fixture",
        (OperationSpec(operation, OperationType.EXECUTE),),
        permissions=permissions,
        metadata=metadata,
    )


def test_extracts_all_automatic_claim_types_without_exposing_sensitive_values() -> None:
    url = "https://private.internal.example:8443/base"
    root = "C:\\private\\customer\\documents"
    manifest = _manifest(
        "first",
        "a",
        driver_name="http-api",
        config={
            "baseUrl": url,
            "port": 9_000,
            "root": root,
            "operations": {"search": {"method": "GET", "path": "/items"}},
        },
    )

    claims = extract_projection_claims(manifest)

    assert {claim.claim_type for claim in claims} == {
        "filesystem_root",
        "http_route",
        "identity",
        "permission_namespace",
        "port",
        "projection_name",
    }
    serialized = repr(claims)
    assert url not in serialized
    assert root not in serialized
    assert all(claim.resource_id.startswith("sha256:") for claim in claims)


def test_default_deny_detects_cross_capability_collision_without_leaking_route() -> None:
    route = "https://secret.example/internal"
    config = {
        "baseUrl": route,
        "operations": {"search": {"method": "POST", "path": "/private"}},
    }
    first = _manifest("first", "b", driver_name="http-api", config=config)
    second = _manifest("second", "c", driver_name="http-api", config=config)

    with pytest.raises(ProjectionError) as raised:
        resolve_projections((first, second))

    assert raised.value.code == "projection_conflict"
    assert route not in repr(raised.value.as_dict())
    assert "/private" not in repr(raised.value.as_dict())


def test_namespace_resolution_is_deterministic_for_namespaceable_claims() -> None:
    first = _manifest("first", "d")
    second = _manifest("second", "e")
    policy = ProjectionPolicy("namespace")

    forward = resolve_projections((first, second), policy)
    reversed_result = resolve_projections((second, first), policy)

    assert forward == reversed_result
    assert {collision.claim_type for collision in forward.collisions} == {
        "permission_namespace",
        "projection_name",
    }
    conflicted = [item for item in forward.decisions if item.action == "namespace"]
    assert len(conflicted) == 4
    assert len({item.effective_resource_id for item in conflicted}) == 4


def test_namespace_rejects_non_namespaceable_port_but_isolate_resolves_it() -> None:
    first = _manifest("first", "f", config={"port": 8_080}, driver_name="service")
    second = _manifest("second", "1", config={"port": 8_080}, driver_name="service")

    with pytest.raises(ProjectionError) as raised:
        resolve_projections((first, second), ProjectionPolicy("namespace"))
    assert raised.value.code == "projection_not_namespaceable"
    assert raised.value.details == {"claim_type": "port"}

    isolated = resolve_projections((first, second), ProjectionPolicy("isolate"))
    port_decisions = [item for item in isolated.decisions if item.claim.claim_type == "port"]
    assert {item.action for item in port_decisions} == {"isolate"}
    assert len({item.effective_resource_id for item in port_decisions}) == 2


def test_select_one_requires_and_honors_explicit_coordinate() -> None:
    first = _manifest("first", "2")
    second = _manifest("second", "3")
    with pytest.raises(ValueError, match="explicit coordinate"):
        ProjectionPolicy("select-one")

    result = resolve_projections(
        (first, second),
        ProjectionPolicy("select-one", selected_coordinate="demo/second"),
    )
    conflicting = [item for item in result.decisions if item.action != "allow"]
    assert {item.action for item in conflicting} == {"selected", "excluded"}
    assert all(
        (item.action == "selected") == (item.claim.coordinate == "demo/second")
        for item in conflicting
    )


def test_select_one_rejects_coordinate_not_present_in_every_collision() -> None:
    first = _manifest("first", "4", permissions=("alpha",))
    second = _manifest("second", "5", permissions=("alpha",))
    third = _manifest("third", "6", operation="other", permissions=("beta",))
    fourth = _manifest("fourth", "7", operation="other", permissions=("beta",))

    with pytest.raises(ProjectionError) as raised:
        resolve_projections(
            (first, second, third, fourth),
            ProjectionPolicy("select-one", selected_coordinate="demo/first"),
        )
    assert raised.value.code == "selected_coordinate_not_claimant"


def test_invalid_driver_projection_error_does_not_echo_url_or_path() -> None:
    sensitive = "https://user:password@private.example/path"
    manifest = _manifest(
        "invalid",
        "8",
        driver_name="http-api",
        config={"baseUrl": sensitive, "operations": {}},
    )

    with pytest.raises(ProjectionError) as raised:
        extract_projection_claims(manifest)

    assert raised.value.code == "invalid_projected_http_route"
    assert sensitive not in repr(raised.value.as_dict())
