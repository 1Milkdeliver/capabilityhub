"""Strict project-manifest wiring for the four opt-in execution adapters."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from capabilityhub.models import CapabilityManifest
from capabilityhub.providers.base import CapabilityProvider
from capabilityhub.providers.cli import CliInvocation, CliProcessFixture, CliProcessProvider
from capabilityhub.providers.http import (
    EnvironmentHeaders,
    HttpApiFixture,
    HttpApiProvider,
    HttpInvocation,
)
from capabilityhub.providers.rag import LocalRagFixture, LocalRagProvider


def project_providers(
    manifests: tuple[CapabilityManifest, ...], project: Path
) -> tuple[tuple[CapabilityProvider, ...], int]:
    """Build only explicitly configured built-in providers; reject malformed entries."""

    groups: dict[str, list[object]] = {
        "cli-process": [],
        "http-api": [],
        "local-rag": [],
        "mcp-stdio": [],
    }
    invalid = 0
    for manifest in manifests:
        driver = manifest.metadata.get("driver")
        if not isinstance(driver, Mapping):
            continue
        try:
            name = _text(driver, "name")
            config = _mapping(driver, "config")
            if name != manifest.provider or name not in groups:
                raise ValueError("unsupported configured provider")
            groups[name].append(_fixture(name, manifest, config, project))
        except (OSError, TypeError, ValueError):
            invalid += 1

    providers: list[CapabilityProvider] = []
    if groups["cli-process"]:
        providers.append(CliProcessProvider(groups["cli-process"], name="cli-process"))  # type: ignore[arg-type]
    if groups["http-api"]:
        providers.append(HttpApiProvider(groups["http-api"], name="http-api"))  # type: ignore[arg-type]
    if groups["local-rag"]:
        providers.append(LocalRagProvider(groups["local-rag"], name="local-rag"))  # type: ignore[arg-type]
    if groups["mcp-stdio"]:
        from capabilityhub.providers.mcp import McpStdioProvider

        providers.append(McpStdioProvider(groups["mcp-stdio"], name="mcp-stdio"))  # type: ignore[arg-type]
    return tuple(providers), invalid


def _fixture(
    name: str, manifest: CapabilityManifest, config: Mapping[str, object], project: Path
) -> object:
    if name == "cli-process":
        return CliProcessFixture(
            manifest,
            _absolute_file(config, "executable"),
            _cli_operations(config),
            cwd=_optional_directory(config, "cwd"),
            environment=_environment(config),
        )
    if name == "http-api":
        return HttpApiFixture(
            manifest,
            _text(config, "baseUrl"),
            _http_operations(config),
            headers=EnvironmentHeaders(
                tuple(_string_mapping(config, "headerEnvironment", required=False).items())
            ),
        )
    if name == "local-rag":
        root = (project / _text(config, "root")).resolve()
        if not root.is_relative_to(project.resolve()):
            raise ValueError("RAG root must remain inside the project")
        return LocalRagFixture(
            manifest,
            root,
            operation=_optional_text(config, "operation", "retrieve"),
            suffixes=tuple(_string_list(config, "suffixes", [".md", ".txt"])),
            chunk_lines=_positive_int(config, "chunkLines", 12),
            max_files=_positive_int(config, "maxFiles", 500),
            max_file_bytes=_positive_int(config, "maxFileBytes", 512_000),
        )
    from capabilityhub.providers.mcp import McpStdioFixture

    return McpStdioFixture(
        manifest,
        _absolute_file(config, "command"),
        tuple(_string_list(config, "args", [])),
        _string_mapping(config, "tools"),
        cwd=_optional_directory(config, "cwd"),
        environment=_environment(config),
    )


def _cli_operations(config: Mapping[str, object]) -> dict[str, CliInvocation]:
    result: dict[str, CliInvocation] = {}
    for name, raw in _mapping(config, "operations").items():
        item = _object(raw)
        result[name] = CliInvocation(
            tuple(_string_list(item, "argv", [])),
            output=_optional_text(item, "output", "json"),  # type: ignore[arg-type]
        )
    return result


def _http_operations(config: Mapping[str, object]) -> dict[str, HttpInvocation]:
    result: dict[str, HttpInvocation] = {}
    for name, raw in _mapping(config, "operations").items():
        item = _object(raw)
        result[name] = HttpInvocation(
            _text(item, "method"),  # type: ignore[arg-type]
            _text(item, "path"),
            query=_string_mapping(item, "query", required=False),
            body=tuple(_string_list(item, "body", [])),
        )
    return result


def _environment(config: Mapping[str, object], *, field: str = "environmentFrom") -> dict[str, str]:
    names = _string_mapping(config, field, required=False)
    result: dict[str, str] = {}
    for target, source in names.items():
        value = os.environ.get(source)
        if value is None:
            raise ValueError(f"required environment variable is unavailable: {source}")
        result[target] = value
    return result


def _absolute_file(config: Mapping[str, object], field: str) -> Path:
    path = Path(_text(config, field))
    if not path.is_absolute():
        raise ValueError(f"{field} must be absolute")
    return path


def _optional_directory(config: Mapping[str, object], field: str) -> Path | None:
    value = config.get(field)
    return None if value is None else Path(_text(config, field))


def _mapping(value: Mapping[str, object], field: str) -> Mapping[str, object]:
    return _object(value.get(field))


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError("expected an object")
    return value


def _text(value: Mapping[str, object], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{field} must be a non-empty string")
    return raw


def _optional_text(value: Mapping[str, object], field: str, default: str) -> str:
    return default if field not in value else _text(value, field)


def _string_list(value: Mapping[str, object], field: str, default: list[str]) -> list[str]:
    raw = value.get(field, default)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field} must be a string array")
    return raw


def _string_mapping(
    value: Mapping[str, object], field: str, *, required: bool = True
) -> dict[str, str]:
    if not required and field not in value:
        return {}
    raw = _mapping(value, field)
    if not all(isinstance(item, str) for item in raw.values()):
        raise ValueError(f"{field} values must be strings")
    return {key: item for key, item in raw.items() if isinstance(item, str)}


def _positive_int(value: Mapping[str, object], field: str, default: int) -> int:
    raw = value.get(field, default)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise ValueError(f"{field} must be a positive integer")
    return raw
