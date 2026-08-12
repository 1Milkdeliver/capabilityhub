from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.activation_lock import export_activation_lock
from capabilityhub.errors import CapabilityHubError
from capabilityhub.local_catalog import discover_local_catalog
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ConflictSpec,
    OperationSpec,
    OperationType,
)
from capabilityhub.projections import ProjectionError, ProjectionPolicy, resolve_projections
from capabilityhub.registry import CapabilityRegistry


def _manifest(name: str, digest: str, *, secret: str = "private") -> CapabilityManifest:
    return CapabilityManifest(
        CapabilityIdentity("demo", name, "1", "sha256:" + digest * 64),
        CapabilityKind.API,
        "Projection admission fixture.",
        "fixture",
        (OperationSpec("shared-route", OperationType.EXECUTE),),
        permissions=("network.http",),
        metadata={
            "driver": {
                "name": "http-api",
                "config": {
                    "baseUrl": f"https://{secret}.example.test/base",
                    "port": 8123,
                    "root": f"C:\\{secret}\\records",
                    "operations": {
                        "shared-route": {"method": "GET", "path": "/private"}
                    },
                },
            }
        },
    )


def test_registry_deny_rejects_multi_projection_before_activation_without_leak() -> None:
    first = _manifest("first", "a", secret="SECRET-CANARY")
    second = _manifest("second", "b", secret="SECRET-CANARY")
    registry = CapabilityRegistry(projection_policy=ProjectionPolicy("deny"))

    with pytest.raises(ProjectionError) as denied:
        registry.register_many((first, second))

    assert registry.revisions == {}
    assert registry.activations == {}
    assert denied.value.details["claim_type"] in {
        "filesystem_root",
        "http_route",
        "permission_namespace",
        "port",
        "projection_name",
    }
    assert "SECRET-CANARY" not in str(denied.value.as_dict())
    assert "/private" not in str(denied.value.as_dict())


def test_namespace_rejects_physical_claims_and_isolate_admits_all() -> None:
    manifests = (_manifest("first", "c"), _manifest("second", "d"))
    with pytest.raises(ProjectionError) as denied:
        CapabilityRegistry(projection_policy=ProjectionPolicy("namespace")).register_many(
            manifests
        )
    assert denied.value.code == "projection_not_namespaceable"

    registry = CapabilityRegistry(projection_policy=ProjectionPolicy("isolate"))
    registry.register_many(manifests)
    registry.activate("demo/first")
    registry.activate("demo/second")

    assert set(registry.activations) == {"demo/first", "demo/second"}
    assert {item.claim_type for item in registry.projection_resolution.collisions} == {
        "filesystem_root",
        "http_route",
        "permission_namespace",
        "port",
        "projection_name",
    }
    assert all(
        decision.action in {"allow", "isolate"}
        for decision in registry.projection_resolution.decisions
    )


def test_select_one_never_exposes_loser_and_activation_lock_matches_winner() -> None:
    first = _manifest("first", "e")
    second = _manifest("second", "f")
    registry = CapabilityRegistry(
        projection_policy=ProjectionPolicy("select-one", "demo/second")
    )
    registry.register_many((first, second))

    with pytest.raises(CapabilityHubError) as excluded:
        registry.activate("demo/first")
    assert excluded.value.code == "projection_coordinate_excluded"
    assert tuple(excluded.value.details) == ("projection_digest",)
    assert registry.activations == {}

    registry.activate("demo/second")
    lock = export_activation_lock(registry)
    assert set(lock["capabilities"]) == {"demo/second"}


def test_projection_resolution_is_deterministic_under_concurrent_readers() -> None:
    manifests = (_manifest("first", "1"), _manifest("second", "2"))

    def resolve(_index: int) -> tuple[str, object]:
        result = resolve_projections(manifests, ProjectionPolicy("isolate"))
        return result.digest, result.decisions

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(resolve, range(100)))

    assert len(set(result[0] for result in results)) == 1
    assert all(result[1] == results[0][1] for result in results)


def test_explicit_conflict_exposes_digest_only() -> None:
    secret = "SECRET-CONFLICT-VALUE"
    first = _manifest("first", "3")
    second = _manifest("second", "4")
    first = CapabilityManifest(
        first.identity,
        first.kind,
        first.summary,
        first.provider,
        first.operations,
        conflicts=(ConflictSpec("route", secret),),
    )
    second = CapabilityManifest(
        second.identity,
        second.kind,
        second.summary,
        second.provider,
        second.operations,
        conflicts=(ConflictSpec("route", secret),),
    )
    registry = CapabilityRegistry()
    registry.register_many((first, second))
    registry.activate("demo/first")

    with pytest.raises(CapabilityHubError) as denied:
        registry.activate("demo/second")

    assert denied.value.code == "capability_conflict"
    assert "value" not in denied.value.details
    assert denied.value.details["value_digest"].startswith("sha256:")
    assert secret not in str(denied.value.as_dict())


def test_local_catalog_applies_select_one_before_returning_manifests(tmp_path) -> None:
    project = tmp_path / "project"
    root = project / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    for name, digest in (("first", "5"), ("second", "6")):
        manifest = _manifest(name, digest)
        document = {
            "apiVersion": "capabilityhub.io/v1alpha1",
            "kind": "Capability",
            "metadata": {
                "namespace": "demo",
                "name": name,
                "version": "1",
                "digest": manifest.identity.digest,
                "driver": manifest.metadata["driver"],
            },
            "spec": {
                "type": "api",
                "summary": manifest.summary,
                "provider": "fixture",
                "permissions": ["network.http"],
                "operations": [{"name": "shared-route"}],
            },
        }
        (root / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")

    catalog = discover_local_catalog(
        home=tmp_path / "home",
        project=project,
        projection_policy=ProjectionPolicy("select-one", "demo/second"),
    )

    coordinates = {manifest.identity.coordinate for manifest in catalog.manifests}
    assert "demo/first" not in coordinates
    assert "demo/second" in coordinates
    assert "demo/first" in catalog.inactive_coordinates
    assert catalog.projection_resolution is not None
    assert catalog.projection_resolution.digest.startswith("sha256:")
