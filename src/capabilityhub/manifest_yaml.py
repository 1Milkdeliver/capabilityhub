"""Bounded YAML ingestion that produces inert, canonical JSON-compatible data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml  # type: ignore[import-untyped]

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import canonical_json
from capabilityhub.models import JsonValue

_JSON_TAGS = {
    "tag:yaml.org,2002:map",
    "tag:yaml.org,2002:seq",
    "tag:yaml.org,2002:str",
    "tag:yaml.org,2002:null",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
}


@dataclass(frozen=True, slots=True)
class YamlLimits:
    max_bytes: int = 1_048_576
    max_depth: int = 64
    max_nodes: int = 10_000

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_depth, self.max_nodes) <= 0:
            raise ValueError("YAML limits must be positive")


def load_manifest_yaml(
    source: str | bytes, *, limits: YamlLimits | None = None
) -> dict[str, JsonValue]:
    """Safely load one bounded YAML document as a JSON-compatible object.

    Aliases are rejected entirely, which removes both recursive aliases and
    alias-amplification bombs before object construction.
    """

    selected = limits or YamlLimits()
    text = _text(source)
    if len(text.encode("utf-8")) > selected.max_bytes:
        raise _yaml_error("yaml_size_limit", "The YAML manifest exceeds the size limit.")
    _inspect_events(text, selected)
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _yaml_error("invalid_yaml", "The YAML manifest is invalid.") from exc
    if not isinstance(loaded, Mapping):
        raise _yaml_error("yaml_root_not_object", "The YAML manifest root must be an object.")
    document = _json_object(loaded)
    # Canonical serialization is also a final proof that no YAML-only object survived.
    canonical_json(document)
    return document


def manifest_yaml_to_canonical_json(
    source: str | bytes, *, limits: YamlLimits | None = None
) -> str:
    return canonical_json(load_manifest_yaml(source, limits=limits))


def _inspect_events(text: str, limits: YamlLimits) -> None:
    documents = 0
    depth = 0
    nodes = 0
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.events.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise _yaml_error(
                        "yaml_multiple_documents", "Only one YAML document is allowed."
                    )
            if isinstance(event, yaml.events.AliasEvent):
                raise _yaml_error("yaml_alias_forbidden", "YAML aliases are not allowed.")
            if isinstance(
                event,
                (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent),
            ):
                depth += 1
                nodes += 1
                if depth > limits.max_depth:
                    raise _yaml_error(
                        "yaml_depth_limit", "The YAML manifest exceeds the depth limit."
                    )
                _check_tag(event.tag)
            elif isinstance(event, yaml.events.ScalarEvent):
                nodes += 1
                _check_tag(event.tag)
            elif isinstance(
                event,
                (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent),
            ):
                depth -= 1
            if nodes > limits.max_nodes:
                raise _yaml_error("yaml_node_limit", "The YAML manifest exceeds the node limit.")
    except CapabilityHubError:
        raise
    except yaml.YAMLError as exc:
        raise _yaml_error("invalid_yaml", "The YAML manifest is invalid.") from exc


def _check_tag(tag: str | None) -> None:
    if tag is not None and tag not in _JSON_TAGS:
        raise _yaml_error("yaml_tag_forbidden", "Custom YAML tags are not allowed.")


def _text(source: str | bytes) -> str:
    if isinstance(source, str):
        return source
    if not isinstance(source, bytes):
        raise TypeError("YAML source must be str or bytes")
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _yaml_error("yaml_invalid_utf8", "The YAML manifest must be UTF-8.") from exc


def _json_object(value: Mapping[object, object]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise _yaml_error("yaml_non_string_key", "YAML object keys must be strings.")
        result[key] = _json_value(item)
    return result


def _json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        return _json_object(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    raise _yaml_error("yaml_non_json_value", "The YAML manifest contains a non-JSON value.")


def _yaml_error(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INPUT, safe_message=message)
