from __future__ import annotations

import pytest

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.manifest import API_VERSION, parse_manifest
from capabilityhub.models import CapabilityKind
from capabilityhub.registry import CapabilityRegistry


def manifest(
    name: str,
    *,
    kind: str = "api",
    version: str = "1.0.0",
    digest_char: str = "a",
    dependencies: list[dict[str, object]] | None = None,
    conflicts: list[dict[str, str]] | None = None,
):
    return parse_manifest(
        {
            "apiVersion": API_VERSION,
            "kind": "Capability",
            "metadata": {
                "namespace": "core",
                "name": name,
                "version": version,
                "digest": "sha256:" + digest_char * 64,
            },
            "spec": {
                "type": kind,
                "summary": "A test capability.",
                "provider": "fixture",
                "operations": [{"name": "run"}],
                "dependencies": dependencies or [],
                "conflicts": conflicts or [],
            },
        }
    )


def test_stores_immutable_revisions_and_indexes_every_kind() -> None:
    registry = CapabilityRegistry()
    registered = [
        manifest(kind, kind=kind, digest_char=chr(97 + index))
        for index, kind in enumerate(CapabilityKind)
    ]
    registry.register_many(registered)

    assert tuple(item.identity.revision for item in registry.by_kind("skill")) == (
        registered[0].identity.revision,
    )
    assert all(len(registry.by_kind(kind)) == 1 for kind in CapabilityKind)
    assert registry.revision(registered[2].identity.revision) is registered[2]
    with pytest.raises(TypeError):
        registry.revisions["new"] = registered[0]  # type: ignore[index]


def test_activation_uses_revision_pointer_and_requires_active_dependencies() -> None:
    dependency = manifest("dependency", digest_char="b")
    dependent = manifest(
        "dependent",
        digest_char="c",
        dependencies=[{"coordinate": "core/dependency", "version": "^1.0"}],
    )
    registry = CapabilityRegistry()
    registry.register_many((dependency, dependent))

    with pytest.raises(CapabilityHubError) as missing:
        registry.activate("core/dependent", dependent.identity.revision)
    assert missing.value.code == "missing_dependency"

    registry.activate("core/dependency", dependency.identity.revision)
    registry.activate("core/dependent", dependent.identity.revision)
    assert registry.active("core/dependent") is dependent
    assert registry.activations["core/dependent"] == dependent.identity.revision


def test_staged_validation_reports_deterministic_cycle() -> None:
    first = manifest("first", digest_char="d", dependencies=[{"coordinate": "core/second"}])
    second = manifest("second", digest_char="e", dependencies=[{"coordinate": "core/first"}])
    registry = CapabilityRegistry()
    registry.register_many((second, first))

    with pytest.raises(CapabilityHubError) as error:
        registry.validate_staged()

    assert error.value.category is ErrorCategory.DEPENDENCY
    assert error.value.code == "dependency_cycle"
    assert error.value.details["cycle"] == ("core/first", "core/second", "core/first")


def test_staged_validation_reports_missing_dependency() -> None:
    registry = CapabilityRegistry()
    registry.register(manifest("first", dependencies=[{"coordinate": "core/missing"}]))

    with pytest.raises(CapabilityHubError) as error:
        registry.validate_staged()

    assert error.value.code == "missing_dependency"
    assert error.value.details["dependency"] == "core/missing"


def test_conflicting_active_capabilities_are_rejected_without_pointer_change() -> None:
    first = manifest(
        "first", digest_char="f", conflicts=[{"type": "projection_name", "value": "search"}]
    )
    second = manifest(
        "second", digest_char="1", conflicts=[{"type": "projection_name", "value": "search"}]
    )
    registry = CapabilityRegistry()
    registry.register_many((first, second))
    registry.activate("core/first", first.identity.revision)

    with pytest.raises(CapabilityHubError) as error:
        registry.activate("core/second", second.identity.revision)

    assert error.value.category is ErrorCategory.CONFLICT
    assert error.value.code == "capability_conflict"
    assert "core/second" not in registry.activations
