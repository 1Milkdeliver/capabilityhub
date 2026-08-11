"""Offline, allowlisted OpenAPI 3 import into inert HTTP capability manifests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.manifest import parse_manifest
from capabilityhub.manifest_export import export_manifest_json
from capabilityhub.manifest_yaml import load_manifest_yaml
from capabilityhub.metering import canonical_json
from capabilityhub.models import CapabilityManifest, JsonValue

MAX_OPENAPI_BYTES = 1_048_576
_METHODS = ("get", "post", "put", "patch", "delete")
_SECRET_FIELDS = {
    "apikeyvalue",
    "clientsecret",
    "default",
    "example",
    "examples",
    "password",
    "secret",
    "token",
    "value",
}


@dataclass(frozen=True, slots=True)
class OpenApiSelection:
    namespace: str
    name: str
    version: str
    operation_ids: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    provider: str = "http-api"

    def __post_init__(self) -> None:
        if not self.operation_ids or len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError("operation_ids must be explicitly selected and unique")
        if not self.allowed_hosts or len(set(self.allowed_hosts)) != len(self.allowed_hosts):
            raise ValueError("allowed_hosts must be explicitly selected and unique")
        for host in self.allowed_hosts:
            if not host or host != host.casefold() or "://" in host or "/" in host or "@" in host:
                raise ValueError("allowed_hosts entries must be lowercase hostnames only")


@dataclass(frozen=True, slots=True)
class OpenApiImportResult:
    manifest: CapabilityManifest
    source_digest: str
    server_origin: str
    selected_operation_ids: tuple[str, ...]

    def export_json(self) -> str:
        return export_manifest_json(self.manifest)


def import_openapi(
    source: str | bytes,
    *,
    format: Literal["json", "yaml"],
    selection: OpenApiSelection,
) -> OpenApiImportResult:
    """Parse local bytes and project only explicitly selected operations.

    No network client is constructed or called by this import path.
    """

    document = _load_document(source, format)
    return import_openapi_document(document, selection=selection)


def import_openapi_file(path: str | Path, *, selection: OpenApiSelection) -> OpenApiImportResult:
    selected = Path(path)
    suffix = selected.suffix.casefold()
    if suffix not in {".json", ".yaml", ".yml"}:
        raise _error("openapi_format_unsupported", "OpenAPI input must be JSON or YAML.")
    try:
        source = selected.read_bytes()
    except OSError as exc:
        raise _error("openapi_unreadable", "The local OpenAPI file could not be read.") from exc
    return import_openapi(
        source,
        format="json" if suffix == ".json" else "yaml",
        selection=selection,
    )


def import_openapi_document(
    document: Mapping[str, Any], *, selection: OpenApiSelection
) -> OpenApiImportResult:
    root = _mapping(document, "document")
    version = root.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        raise _error("openapi_version_unsupported", "Only OpenAPI 3 documents are supported.")
    _reject_dangerous_features(root)
    origin = _server_origin(root, selection.allowed_hosts)
    operations = _selected_operations(root, selection.operation_ids)
    source_digest = _digest(
        {
            "document": _json_value(root),
            "operationIds": list(selection.operation_ids),
            "serverOrigin": origin,
        }
    )
    operation_names = {
        operation_id: _operation_name(operation_id) for operation_id in selection.operation_ids
    }
    if len(set(operation_names.values())) != len(operation_names):
        raise _error(
            "openapi_operation_name_conflict",
            "Selected OpenAPI operation IDs normalize to conflicting capability names.",
        )
    driver_operations: dict[str, JsonValue] = {}
    manifest_operations: list[JsonValue] = []
    for operation_id in selection.operation_ids:
        operation_name = operation_names[operation_id]
        path, method, path_item, operation = operations[operation_id]
        input_schema, query, body = _input_contract(root, path_item, operation)
        output_schema = _output_contract(root, operation)
        driver_operations[operation_name] = {
            "body": list(body),
            "method": method.upper(),
            "path": path,
            "query": query,
        }
        side_effect = (
            "read"
            if method == "get"
            else "irreversible"
            if method == "delete"
            else "reversible_write"
        )
        manifest_operations.append(
            {
                "inputSchema": input_schema,
                "name": operation_name,
                "operationType": "execute",
                "outputSchema": output_schema,
                "requiresApproval": method == "delete",
                "sideEffect": side_effect,
            }
        )
    info = _mapping(root.get("info"), "info")
    summary = info.get("description", info.get("title", "Imported OpenAPI capability."))
    if not isinstance(summary, str) or not summary.strip():
        summary = "Imported OpenAPI capability."
    summary = summary.strip()[:480]
    manifest_document: dict[str, JsonValue] = {
        "apiVersion": "capabilityhub.io/v1alpha1",
        "kind": "Capability",
        "metadata": {
            "digest": source_digest,
            "name": selection.name,
            "namespace": selection.namespace,
            "version": selection.version,
        },
        "spec": {
            "driver": {
                "config": {
                    "baseUrl": origin,
                    "operations": driver_operations,
                },
                "name": selection.provider,
            },
            "operations": manifest_operations,
            "permissions": ["network"],
            "provider": selection.provider,
            "source": f"openapi://{source_digest}",
            "summary": summary,
            "trustTier": "imported",
            "type": "api",
        },
    }
    manifest = parse_manifest(manifest_document)
    return OpenApiImportResult(
        manifest=manifest,
        source_digest=source_digest,
        server_origin=origin,
        selected_operation_ids=selection.operation_ids,
    )


def _operation_name(operation_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", operation_id.casefold()).strip("-._")
    if not normalized:
        raise _error(
            "openapi_operation_name_invalid",
            "A selected OpenAPI operation ID cannot form a capability operation name.",
        )
    if len(normalized) > 63:
        suffix = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:8]
        normalized = f"{normalized[:54].rstrip('-._')}-{suffix}"
    return normalized


def _load_document(source: str | bytes, format: str) -> dict[str, JsonValue]:
    raw = source.encode("utf-8") if isinstance(source, str) else source
    if not isinstance(raw, bytes):
        raise TypeError("OpenAPI source must be str or bytes")
    if len(raw) > MAX_OPENAPI_BYTES:
        raise _error("openapi_size_limit", "The OpenAPI document exceeds the size limit.")
    if format == "yaml":
        return load_manifest_yaml(raw)
    if format != "json":
        raise _error("openapi_format_unsupported", "OpenAPI input must be JSON or YAML.")
    try:
        loaded = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error("openapi_invalid_json", "The OpenAPI JSON document is invalid.") from exc
    if not isinstance(loaded, dict):
        raise _error("openapi_root_invalid", "The OpenAPI document root must be an object.")
    return cast(dict[str, JsonValue], loaded)


def _reject_dangerous_features(root: Mapping[str, Any]) -> None:
    if "webhooks" in root:
        raise _error("openapi_webhooks_forbidden", "OpenAPI webhooks are not importable.")
    stack: list[Any] = [root]
    while stack:
        value = stack.pop()
        if isinstance(value, Mapping):
            reference = value.get("$ref")
            if isinstance(reference, str) and not reference.startswith("#/"):
                raise _error(
                    "openapi_remote_ref_forbidden", "Remote OpenAPI references are forbidden."
                )
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    paths = _mapping(root.get("paths"), "paths")
    for path, raw_path_item in paths.items():
        path_item = _mapping(raw_path_item, f"paths.{path}")
        if "servers" in path_item:
            raise _error("openapi_server_override_forbidden", "Path-level servers are forbidden.")
        for method in _METHODS:
            raw_operation = path_item.get(method)
            if raw_operation is None:
                continue
            operation = _mapping(raw_operation, f"paths.{path}.{method}")
            if "servers" in operation:
                raise _error(
                    "openapi_server_override_forbidden",
                    "Operation-level servers are forbidden.",
                )
            if "callbacks" in operation:
                raise _error("openapi_callbacks_forbidden", "OpenAPI callbacks are not importable.")
            if operation.get("security") not in (None, []):
                raise _error(
                    "openapi_security_binding_forbidden",
                    "OpenAPI security bindings require separate credential configuration.",
                )
    security = root.get("security", [])
    if security not in (None, []):
        raise _error(
            "openapi_security_binding_forbidden",
            "OpenAPI security bindings require separate credential configuration.",
        )
    components = root.get("components")
    if isinstance(components, Mapping):
        schemes = components.get("securitySchemes")
        if isinstance(schemes, Mapping):
            _reject_secret_values(schemes)


def _reject_secret_values(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.casefold().replace("_", "") in _SECRET_FIELDS:
                raise _error(
                    "openapi_security_secret_forbidden",
                    "Security scheme secret values must not be embedded.",
                )
            _reject_secret_values(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_values(item)


def _server_origin(root: Mapping[str, Any], allowed_hosts: tuple[str, ...]) -> str:
    servers = root.get("servers")
    if not isinstance(servers, Sequence) or isinstance(servers, (str, bytes)) or len(servers) != 1:
        raise _error("openapi_server_ambiguous", "Exactly one fixed OpenAPI server is required.")
    server = _mapping(servers[0], "servers[0]")
    if "variables" in server:
        raise _error("openapi_server_variable_forbidden", "Server variables are forbidden.")
    url = server.get("url")
    if not isinstance(url, str) or "{" in url or "}" in url:
        raise _error("openapi_server_invalid", "The OpenAPI server URL must be fixed.")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise _error(
            "openapi_server_invalid", "The OpenAPI server must be a credential-free origin."
        )
    hostname = parsed.hostname.casefold()
    if hostname not in allowed_hosts:
        raise _error("openapi_host_denied", "The OpenAPI server host is not allowlisted.")
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise _error("openapi_cleartext_remote_forbidden", "Remote OpenAPI servers must use HTTPS.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _selected_operations(
    root: Mapping[str, Any], operation_ids: tuple[str, ...]
) -> dict[str, tuple[str, str, Mapping[str, Any], Mapping[str, Any]]]:
    selected = set(operation_ids)
    found: dict[str, tuple[str, str, Mapping[str, Any], Mapping[str, Any]]] = {}
    seen: set[str] = set()
    paths = _mapping(root.get("paths"), "paths")
    for path in sorted(paths):
        if not path.startswith("/") or "://" in path or "?" in path:
            raise _error("openapi_path_invalid", "OpenAPI paths must be fixed absolute paths.")
        path_item = _mapping(paths[path], f"paths.{path}")
        for method in _METHODS:
            raw_operation = path_item.get(method)
            if raw_operation is None:
                continue
            operation = _mapping(raw_operation, f"paths.{path}.{method}")
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                continue
            if operation_id in seen:
                raise _error(
                    "openapi_operation_id_ambiguous", "OpenAPI operationIds must be unique."
                )
            seen.add(operation_id)
            if operation_id in selected:
                found[operation_id] = (path, method, path_item, operation)
    missing = sorted(selected - set(found))
    if missing:
        raise CapabilityHubError(
            code="openapi_operation_not_found",
            category=ErrorCategory.INPUT,
            safe_message="A selected OpenAPI operationId was not found.",
            details={"operation_ids": missing},
        )
    return found


def _input_contract(
    root: Mapping[str, Any],
    path_item: Mapping[str, Any],
    operation: Mapping[str, Any],
) -> tuple[dict[str, JsonValue], dict[str, JsonValue], tuple[str, ...]]:
    properties: dict[str, JsonValue] = {}
    required: list[str] = []
    query: dict[str, JsonValue] = {}
    parameters = [
        *_array(path_item.get("parameters", []), "parameters"),
        *_array(operation.get("parameters", []), "parameters"),
    ]
    for raw_parameter in parameters:
        parameter = _resolved_object(root, raw_parameter, "parameter")
        name = parameter.get("name")
        location = parameter.get("in")
        if not isinstance(name, str) or not name or location not in {"path", "query"}:
            raise _error(
                "openapi_parameter_unsupported", "Only named path/query parameters are supported."
            )
        if name in properties:
            raise _error("openapi_parameter_ambiguous", "OpenAPI parameter names must be unique.")
        parameter_schema = _schema(root, parameter.get("schema", {"type": "string"}))
        if parameter_schema.get("type", "string") not in {
            "string",
            "integer",
            "number",
            "boolean",
        }:
            raise _error("openapi_parameter_unsupported", "Path/query parameters must be scalar.")
        properties[name] = parameter_schema
        if location == "path" or parameter.get("required") is True:
            required.append(name)
        if location == "query":
            query[name] = name
    body_fields: tuple[str, ...] = ()
    raw_body = operation.get("requestBody")
    if raw_body is not None:
        request_body = _resolved_object(root, raw_body, "requestBody")
        content = _mapping(request_body.get("content"), "requestBody.content")
        if set(content) != {"application/json"}:
            raise _error(
                "openapi_content_type_ambiguous",
                "Request bodies must declare only application/json.",
            )
        media = _mapping(content["application/json"], "requestBody.content.application/json")
        body_schema = _schema(root, media.get("schema", {}))
        raw_properties = body_schema.get("properties")
        if body_schema.get("type") == "object" and isinstance(raw_properties, Mapping):
            body_names: list[str] = []
            for name, value in raw_properties.items():
                if name in properties:
                    raise _error(
                        "openapi_parameter_ambiguous", "Body and parameter names must be unique."
                    )
                properties[name] = value
                body_names.append(name)
            body_fields = tuple(sorted(body_names))
            raw_required = body_schema.get("required", [])
            if isinstance(raw_required, list):
                required.extend(
                    item for item in raw_required if isinstance(item, str) and item in body_names
                )
        else:
            if "body" in properties:
                raise _error("openapi_parameter_ambiguous", "Body argument is ambiguous.")
            properties["body"] = body_schema
            body_fields = ("body",)
            if request_body.get("required") is True:
                required.append("body")
    input_schema: dict[str, JsonValue] = {
        "additionalProperties": False,
        "properties": properties,
        "type": "object",
    }
    if required:
        input_schema["required"] = list(dict.fromkeys(required))
    return input_schema, query, body_fields


def _output_contract(root: Mapping[str, Any], operation: Mapping[str, Any]) -> dict[str, JsonValue]:
    responses = _mapping(operation.get("responses", {}), "responses")
    success = sorted(key for key in responses if len(key) == 3 and key.startswith("2"))
    if not success:
        return {}
    response = _resolved_object(root, responses[success[0]], "response")
    content = response.get("content")
    if not isinstance(content, Mapping) or "application/json" not in content:
        return {}
    media = _mapping(content["application/json"], "response.content.application/json")
    return _schema(root, media.get("schema", {}))


def _schema(
    root: Mapping[str, Any], value: Any, seen: tuple[str, ...] = ()
) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise _error("openapi_schema_invalid", "OpenAPI schemas must be objects.")
    reference = value.get("$ref")
    if isinstance(reference, str):
        if reference in seen:
            raise _error("openapi_ref_cycle", "Cyclic OpenAPI references are not importable.")
        resolved = _resolve_pointer(root, reference)
        return _schema(root, resolved, (*seen, reference))
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            result[key] = _schema(root, item, seen)
        elif isinstance(item, list):
            result[key] = [_schema_item(root, nested, seen) for nested in item]
        else:
            result[key] = cast(JsonValue, item)
    return result


def _schema_item(root: Mapping[str, Any], value: Any, seen: tuple[str, ...]) -> JsonValue:
    return _schema(root, value, seen) if isinstance(value, Mapping) else cast(JsonValue, value)


def _resolved_object(root: Mapping[str, Any], value: Any, field: str) -> Mapping[str, Any]:
    item = _mapping(value, field)
    reference = item.get("$ref")
    if isinstance(reference, str):
        return _mapping(_resolve_pointer(root, reference), field)
    return item


def _resolve_pointer(root: Mapping[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        raise _error("openapi_remote_ref_forbidden", "Remote OpenAPI references are forbidden.")
    value: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, Mapping) or part not in value:
            raise _error("openapi_ref_not_found", "A local OpenAPI reference was not found.")
        value = value[part]
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise _error("openapi_shape_invalid", f"{field} must be an object.")
    return value


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise _error("openapi_shape_invalid", f"{field} must be an array.")
    return value


def _json_value(value: Any) -> JsonValue:
    try:
        return cast(JsonValue, json.loads(canonical_json(cast(JsonValue, value))))
    except (TypeError, ValueError) as exc:
        raise _error("openapi_non_json_value", "OpenAPI input must contain JSON values.") from exc


def _digest(value: JsonValue) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _error(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INPUT, safe_message=message)
