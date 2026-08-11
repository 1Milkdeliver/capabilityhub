from __future__ import annotations

import json
import socket
from copy import deepcopy

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.manifest_export import export_manifest_json
from capabilityhub.openapi_import import (
    OpenApiSelection,
    import_openapi,
    import_openapi_document,
    import_openapi_file,
)


def _selection(*operation_ids: str, host: str = "api.example.test") -> OpenApiSelection:
    return OpenApiSelection(
        namespace="imported",
        name="pet-api",
        version="1.0.0",
        operation_ids=tuple(operation_ids),
        allowed_hosts=(host,),
    )


def _document() -> dict[str, object]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Pet API", "description": "Read and create pets."},
        "servers": [{"url": "https://api.example.test"}],
        "paths": {
            "/pets/{pet_id}": {
                "parameters": [
                    {
                        "name": "pet_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "get": {
                    "operationId": "get-pet",
                    "parameters": [
                        {
                            "name": "verbose",
                            "in": "query",
                            "schema": {"type": "boolean"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"name": {"type": "string"}},
                                    }
                                }
                            }
                        }
                    },
                },
            },
            "/pets": {
                "post": {
                    "operationId": "create-pet",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "age": {"type": "integer"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            },
        },
    }


def test_json_import_projects_only_selected_operation_and_driver_contract() -> None:
    source = json.dumps(_document())

    result = import_openapi(source, format="json", selection=_selection("get-pet"))
    operation = result.manifest.operations[0]
    driver = result.manifest.metadata["driver"]

    assert result.server_origin == "https://api.example.test"
    assert tuple(item.name for item in result.manifest.operations) == ("get-pet",)
    assert operation.input_schema == {
        "additionalProperties": False,
        "properties": {
            "pet_id": {"type": "string"},
            "verbose": {"type": "boolean"},
        },
        "required": ["pet_id"],
        "type": "object",
    }
    assert driver["config"]["operations"]["get-pet"] == {  # type: ignore[index]
        "body": [],
        "method": "GET",
        "path": "/pets/{pet_id}",
        "query": {"verbose": "verbose"},
    }


def test_yaml_import_uses_safe_loader_and_projects_json_body(tmp_path) -> None:
    source = """
openapi: 3.1.0
info: {title: Local API}
servers: [{url: http://127.0.0.1:8123}]
paths:
  /records:
    post:
      operationId: create-record
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [name]
              properties:
                name: {type: string}
                count: {type: integer}
      responses: {'201': {description: Created}}
"""
    path = tmp_path / "openapi.yaml"
    path.write_text(source, encoding="utf-8")

    result = import_openapi_file(path, selection=_selection("create-record", host="127.0.0.1"))
    operation = result.manifest.operations[0]
    config = result.manifest.metadata["driver"]["config"]  # type: ignore[index]

    assert operation.input_schema["required"] == ["name"]
    assert config["operations"]["create-record"]["body"] == [  # type: ignore[index]
        "count",
        "name",
    ]


def test_digest_and_export_are_deterministic() -> None:
    first = import_openapi_document(_document(), selection=_selection("get-pet"))
    second = import_openapi_document(_document(), selection=_selection("get-pet"))

    assert first.source_digest == second.source_digest
    assert first.manifest.identity.digest == first.source_digest
    assert first.export_json() == second.export_json()
    assert first.export_json() == export_manifest_json(first.manifest)


def test_operation_and_host_selection_must_be_explicit() -> None:
    with pytest.raises(ValueError, match="operation_ids"):
        _selection()
    with pytest.raises(ValueError, match="allowed_hosts"):
        OpenApiSelection("imported", "api", "1", ("get-pet",), ())


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        ("https://schemas.example.test/pet.json", "openapi_remote_ref_forbidden"),
        ("other.json#/Pet", "openapi_remote_ref_forbidden"),
    ],
)
def test_remote_refs_are_rejected(reference: str, code: str) -> None:
    document = _document()
    operation = document["paths"]["/pets/{pet_id}"]["get"]  # type: ignore[index]
    operation["responses"]["200"] = {"$ref": reference}  # type: ignore[index]

    with pytest.raises(CapabilityHubError) as caught:
        import_openapi_document(document, selection=_selection("get-pet"))

    assert caught.value.code == code


def test_local_refs_are_resolved_into_portable_schema() -> None:
    document = _document()
    document["components"] = {
        "schemas": {
            "Pet": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        }
    }
    response = document["paths"]["/pets/{pet_id}"]["get"]["responses"]["200"]  # type: ignore[index]
    response["content"]["application/json"]["schema"] = {  # type: ignore[index]
        "$ref": "#/components/schemas/Pet"
    }

    result = import_openapi_document(document, selection=_selection("get-pet"))

    assert result.manifest.operations[0].output_schema == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("webhooks", "openapi_webhooks_forbidden"),
        ("callbacks", "openapi_callbacks_forbidden"),
        ("path_server", "openapi_server_override_forbidden"),
        ("operation_server", "openapi_server_override_forbidden"),
    ],
)
def test_dynamic_or_ambiguous_routing_features_are_rejected(mutation: str, code: str) -> None:
    document = _document()
    path_item = document["paths"]["/pets/{pet_id}"]  # type: ignore[index]
    operation = path_item["get"]  # type: ignore[index]
    if mutation == "webhooks":
        document["webhooks"] = {}
    elif mutation == "callbacks":
        operation["callbacks"] = {}
    elif mutation == "path_server":
        path_item["servers"] = [{"url": "https://other.example.test"}]
    else:
        operation["servers"] = [{"url": "https://other.example.test"}]

    with pytest.raises(CapabilityHubError) as caught:
        import_openapi_document(document, selection=_selection("get-pet"))

    assert caught.value.code == code


@pytest.mark.parametrize(
    ("servers", "code"),
    [
        ([], "openapi_server_ambiguous"),
        (
            [
                {"url": "https://api.example.test"},
                {"url": "https://backup.example.test"},
            ],
            "openapi_server_ambiguous",
        ),
        ([{"url": "https://user:secret@api.example.test"}], "openapi_server_invalid"),
        ([{"url": "https://{region}.example.test"}], "openapi_server_invalid"),
    ],
)
def test_server_must_be_one_fixed_credential_free_origin(
    servers: list[dict[str, str]], code: str
) -> None:
    document = _document()
    document["servers"] = servers

    with pytest.raises(CapabilityHubError) as caught:
        import_openapi_document(document, selection=_selection("get-pet"))

    assert caught.value.code == code


def test_host_allowlist_and_cleartext_remote_are_enforced() -> None:
    document = _document()
    with pytest.raises(CapabilityHubError) as denied:
        import_openapi_document(document, selection=_selection("get-pet", host="other.test"))
    document["servers"] = [{"url": "http://api.example.test"}]
    with pytest.raises(CapabilityHubError) as cleartext:
        import_openapi_document(document, selection=_selection("get-pet"))

    assert denied.value.code == "openapi_host_denied"
    assert cleartext.value.code == "openapi_cleartext_remote_forbidden"


def test_security_bindings_and_embedded_secret_values_are_rejected() -> None:
    bound = _document()
    bound["security"] = [{"ApiKey": []}]
    secret = _document()
    secret["components"] = {
        "securitySchemes": {
            "ApiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Key",
                "value": "do-not-import",
            }
        }
    }

    with pytest.raises(CapabilityHubError) as binding_error:
        import_openapi_document(bound, selection=_selection("get-pet"))
    with pytest.raises(CapabilityHubError) as secret_error:
        import_openapi_document(secret, selection=_selection("get-pet"))

    assert binding_error.value.code == "openapi_security_binding_forbidden"
    assert secret_error.value.code == "openapi_security_secret_forbidden"


def test_missing_or_duplicate_operation_ids_fail_closed() -> None:
    document = _document()
    duplicate = deepcopy(document)
    duplicate["paths"]["/other"] = {  # type: ignore[index]
        "get": {"operationId": "get-pet", "responses": {}}
    }

    with pytest.raises(CapabilityHubError) as missing:
        import_openapi_document(document, selection=_selection("missing"))
    with pytest.raises(CapabilityHubError) as ambiguous:
        import_openapi_document(duplicate, selection=_selection("get-pet"))

    assert missing.value.code == "openapi_operation_not_found"
    assert ambiguous.value.code == "openapi_operation_id_ambiguous"


def test_common_mixed_case_operation_ids_normalize_and_collisions_fail() -> None:
    document = _document()
    document["paths"]["/pets/{pet_id}"]["get"]["operationId"] = "getPet"  # type: ignore[index]
    result = import_openapi_document(document, selection=_selection("getPet"))
    assert result.manifest.operations[0].name == "getpet"

    document["paths"]["/pets/{pet_id}"]["get"]["operationId"] = "get_pet"  # type: ignore[index]
    document["paths"]["/pets"]["post"]["operationId"] = "GET_PET"  # type: ignore[index]
    with pytest.raises(CapabilityHubError) as collision:
        import_openapi_document(document, selection=_selection("get_pet", "GET_PET"))
    assert collision.value.code == "openapi_operation_name_conflict"


def test_safe_yaml_rejects_aliases_and_json_rejects_duplicate_keys() -> None:
    with pytest.raises(CapabilityHubError) as alias:
        import_openapi(
            "openapi: 3.1.0\ninfo: &info {title: A}\ncopy: *info",
            format="yaml",
            selection=_selection("get-pet"),
        )
    with pytest.raises(CapabilityHubError) as duplicate:
        import_openapi(
            '{"openapi":"3.1.0","openapi":"3.0.0"}',
            format="json",
            selection=_selection("get-pet"),
        )

    assert alias.value.code == "yaml_alias_forbidden"
    assert duplicate.value.code == "openapi_invalid_json"


def test_import_performs_no_network_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = import_openapi_document(_document(), selection=_selection("get-pet"))

    assert result.server_origin == "https://api.example.test"
