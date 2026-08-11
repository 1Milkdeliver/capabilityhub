from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.manifest import (
    API_VERSION,
    MAX_SUMMARY_CHARS,
    parse_manifest,
    parse_manifest_json,
)
from capabilityhub.models import CapabilityKind, OperationType


def document(kind: str = "api") -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "kind": "Capability",
        "metadata": {
            "namespace": "community",
            "name": f"{kind}-search",
            "version": "1.2.3",
            "digest": "sha256:" + "a" * 64,
        },
        "spec": {
            "type": kind,
            "summary": "Search an authorized provider.",
            "driver": {"name": f"{kind}-driver"},
            "operations": [{"name": "search", "inputSchema": {"type": "object"}}],
            "sections": {"contract": {"ref": "artifact://contract", "tokens": 4}},
            "dependencies": [],
            "conflicts": [],
        },
    }


@pytest.mark.parametrize("kind", ["skill", "mcp", "cli", "api", "rag"])
def test_parses_all_capability_kinds(kind: str) -> None:
    manifest = parse_manifest(document(kind))

    assert manifest.kind is CapabilityKind(kind)
    assert manifest.identity.coordinate == f"community/{kind}-search"
    assert manifest.operation("search") is not None
    assert manifest.operation("search").operation_type is (
        OperationType.RETRIEVE if kind == "rag" else OperationType.EXECUTE
    )


def test_accepts_architecture_shaped_sections_and_operations() -> None:
    raw = document()
    spec = raw["spec"]
    assert isinstance(spec, dict)
    spec["operations"] = [
        {
            "name": "search",
            "inputSchemaRef": "#/schemas/Search",
            "outputSchemaRef": "#/schemas/Result",
            "sideEffect": "none",
        }
    ]
    spec["permissions"] = {"network": {"hosts": ["issues.example.org"]}}

    manifest = parse_manifest(raw)

    assert manifest.operation("search").input_schema == {"$ref": "#/schemas/Search"}
    assert manifest.sections[0].content == "artifact://contract"
    assert manifest.permissions == ("network",)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda raw: raw.__setitem__("apiVersion", "v1"), "unsupported_api_version"),
        (lambda raw: raw["metadata"].__setitem__("digest", "sha256:ABC"), "invalid_digest"),  # type: ignore[index]
        (
            lambda raw: raw["spec"].__setitem__("summary", "x" * (MAX_SUMMARY_CHARS + 1)),
            "invalid_manifest_field",
        ),  # type: ignore[index]
        (
            lambda raw: raw["spec"].__setitem__("operations", [{"name": "same"}, {"name": "same"}]),
            "duplicate_operation",
        ),  # type: ignore[index]
        (
            lambda raw: raw["spec"].__setitem__(
                "sections", {"same": {"content": "a"}, "same ": {"content": "b"}}
            ),
            "invalid_name",
        ),  # type: ignore[index]
    ],
)
def test_rejects_invalid_manifests(mutate: object, code: str) -> None:
    raw = document()
    mutate(raw)  # type: ignore[operator]

    with pytest.raises(CapabilityHubError) as error:
        parse_manifest(raw)

    assert error.value.code == code
    assert error.value.category is ErrorCategory.INPUT


def test_json_rejects_non_json_constants() -> None:
    with pytest.raises(CapabilityHubError, match="valid JSON"):
        parse_manifest_json('{"apiVersion": NaN}')


def test_models_are_immutable() -> None:
    manifest = parse_manifest(json.loads(json.dumps(document())))

    with pytest.raises(FrozenInstanceError):
        manifest.summary = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        manifest.metadata["new"] = "value"  # type: ignore[index]
