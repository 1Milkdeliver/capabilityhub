from __future__ import annotations

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.manifest_yaml import (
    YamlLimits,
    load_manifest_yaml,
    manifest_yaml_to_canonical_json,
)


def test_safe_yaml_load_returns_deterministic_json_model() -> None:
    source = """
kind: Capability
apiVersion: capabilityhub.io/v1alpha1
metadata:
  name: demo
  enabled: true
  count: 2
"""

    document = load_manifest_yaml(source)

    assert document["metadata"] == {"name": "demo", "enabled": True, "count": 2}
    assert manifest_yaml_to_canonical_json(source) == (
        '{"apiVersion":"capabilityhub.io/v1alpha1","kind":"Capability",'
        '"metadata":{"count":2,"enabled":true,"name":"demo"}}'
    )


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("value: !python/object:builtins.object {}", "yaml_tag_forbidden"),
        ("base: &base [1, 2]\ncopy: *base", "yaml_alias_forbidden"),
        ("first: 1\n---\nsecond: 2", "yaml_multiple_documents"),
        ("when: 2026-08-12", "yaml_non_json_value"),
        ("1: value", "yaml_non_string_key"),
    ],
)
def test_yaml_only_or_executable_features_are_rejected(source: str, code: str) -> None:
    with pytest.raises(CapabilityHubError) as caught:
        load_manifest_yaml(source)

    assert caught.value.code == code


def test_alias_amplification_is_rejected_before_construction() -> None:
    source = "a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]"

    with pytest.raises(CapabilityHubError) as caught:
        load_manifest_yaml(source)

    assert caught.value.code == "yaml_alias_forbidden"


@pytest.mark.parametrize(
    ("source", "limits", "code"),
    [
        ("key: value", YamlLimits(max_bytes=4), "yaml_size_limit"),
        ("a:\n  b:\n    c: value", YamlLimits(max_depth=2), "yaml_depth_limit"),
        ("a: 1\nb: 2", YamlLimits(max_nodes=3), "yaml_node_limit"),
    ],
)
def test_yaml_resource_limits_fail_closed(source: str, limits: YamlLimits, code: str) -> None:
    with pytest.raises(CapabilityHubError) as caught:
        load_manifest_yaml(source, limits=limits)

    assert caught.value.code == code


def test_yaml_requires_utf8_and_object_root() -> None:
    with pytest.raises(CapabilityHubError, match="UTF-8"):
        load_manifest_yaml(b"\xff")
    with pytest.raises(CapabilityHubError) as caught:
        load_manifest_yaml("- one\n- two")
    assert caught.value.code == "yaml_root_not_object"
