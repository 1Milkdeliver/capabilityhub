from __future__ import annotations

import hashlib

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.lifecycle import StagedUpdateManager
from capabilityhub.manifest import API_VERSION, parse_manifest
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.supply_chain import (
    ArtifactMaterial,
    SupplyChainPolicy,
    SupplyChainVerifier,
)
from capabilityhub.update_store import SQLiteUpdateStore


def _manifest(
    version: str,
    digest: str,
    *,
    name: str = "tool",
    dependencies: list[dict[str, object]] | None = None,
):
    artifact_digest = "sha256:" + hashlib.sha256(digest.encode()).hexdigest()
    return parse_manifest(
        {
            "apiVersion": API_VERSION,
            "kind": "Capability",
            "metadata": {
                "namespace": "demo",
                "name": name,
                "version": version,
                "digest": artifact_digest,
                "artifact_fixture": digest,
            },
            "spec": {
                "type": "api",
                "summary": "Lifecycle fixture.",
                "provider": "fixture",
                "operations": [{"name": "run"}],
                "dependencies": dependencies or [],
            },
        }
    )


def _manager(tmp_path, *manifests) -> StagedUpdateManager:
    registry = CapabilityRegistry()
    registry.register_many(manifests)
    policy = SupplyChainPolicy(
        environment="development",
        trusted_publishers=frozenset({"local-development"}),
        trusted_registries=frozenset({"local-development"}),
    )

    def acquire(revision: str) -> ArtifactMaterial:
        artifact = registry.revision(revision).metadata["artifact_fixture"]
        assert isinstance(artifact, str)
        return ArtifactMaterial(artifact.encode(), "local-development", "local-development")

    return StagedUpdateManager(
        registry=registry,
        store=SQLiteUpdateStore(tmp_path / "updates.sqlite3"),
        verifier=SupplyChainVerifier(policy, clock=lambda: 500),
        artifact_acquirer=acquire,
    )


def _activate(
    manager: StagedUpdateManager,
    revision: str,
    expected: str | None,
) -> None:
    manager.stage(revision, expected_active_revision=expected)
    manager.record_health(revision, passed=True)
    manager.activate(revision, expected_active_revision=expected)


def test_stage_health_activate_and_rollback_use_registered_revisions(tmp_path) -> None:
    first = _manifest("1.0.0", "a")
    second = _manifest("2.0.0", "b")
    manager = _manager(tmp_path, first, second)

    _activate(manager, first.identity.revision, None)
    manager.stage(second.identity.revision, expected_active_revision=first.identity.revision)
    pending = manager.state("demo/tool")
    manager.record_health(second.identity.revision, passed=True)
    activated = manager.activate(
        second.identity.revision,
        expected_active_revision=first.identity.revision,
    )
    rolled_back = manager.rollback(
        "demo/tool",
        expected_active_revision=second.identity.revision,
    )

    assert pending.health_status == "pending"
    assert activated.active_revision == second.identity.revision
    assert activated.previous_revision == first.identity.revision
    assert rolled_back.active_revision == first.identity.revision


def test_registry_dependency_failure_rolls_back_pointer_transaction(tmp_path) -> None:
    dependent = _manifest(
        "1.0.0",
        "c",
        dependencies=[{"coordinate": "demo/missing", "version": "*"}],
    )
    manager = _manager(tmp_path, dependent)
    manager.stage(dependent.identity.revision, expected_active_revision=None)
    manager.record_health(dependent.identity.revision, passed=True)

    with pytest.raises(CapabilityHubError) as raised:
        manager.activate(dependent.identity.revision, expected_active_revision=None)

    assert raised.value.code == "missing_dependency"
    state = manager.state("demo/tool")
    assert state.active_revision is None
    assert state.staged_revision == dependent.identity.revision
    assert state.health_status == "passed"


def test_manager_exposes_in_flight_revision_pins(tmp_path) -> None:
    first = _manifest("1.0.0", "d")
    second = _manifest("2.0.0", "e")
    manager = _manager(tmp_path, first, second)
    _activate(manager, first.identity.revision, None)

    pin = manager.pin_active("demo/tool", "call-1")
    _activate(manager, second.identity.revision, first.identity.revision)

    assert pin.revision == first.identity.revision
    assert manager.pins("demo/tool") == (pin,)
    assert manager.release_pin("call-1")


def test_staged_graph_includes_registry_activations_not_yet_in_store(tmp_path) -> None:
    dependency = _manifest("1.0.0", "f", name="dependency")
    dependent = _manifest(
        "1.0.0",
        "1",
        name="dependent",
        dependencies=[{"coordinate": "demo/dependency", "version": "*"}],
    )
    manager = _manager(tmp_path, dependency, dependent)
    manager.registry.activate("demo/dependency", dependency.identity.revision)

    _activate(manager, dependent.identity.revision, None)

    assert manager.state("demo/dependent").active_revision == dependent.identity.revision
    assert manager.states() == (manager.state("demo/dependent"),)
