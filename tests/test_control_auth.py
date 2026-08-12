from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from capabilityhub.auth import AuthIdentity, LoopbackAuthenticator
from capabilityhub.compatibility import FeatureHandshake
from capabilityhub.errors import CapabilityHubError
from capabilityhub.http_control import LoopbackHttpControl
from capabilityhub.protocol import AdapterKind, JsonValue, RequestEnvelope, protocol_handshake
from capabilityhub.runtime import local_http_control


@dataclass
class _Adapter:
    kind: AdapterKind = AdapterKind.HTTP
    handshake: FeatureHandshake = field(default_factory=protocol_handshake)

    def dispatch(self, request: RequestEnvelope) -> JsonValue:
        return {"operation": request.operation}

    def cancel(self, correlation_id: str) -> bool:
        return False


def _body() -> bytes:
    handshake = protocol_handshake()
    return json.dumps(
        {
            "request_id": "request-1",
            "correlation_id": "correlation-1",
            "operation": "capability.search",
            "payload": {"query": "safe"},
            "handshake": {
                "api_versions": list(handshake.api_versions),
                "supported_features": list(handshake.supported_features),
                "required_features": list(handshake.required_features),
            },
        }
    ).encode()


def _post(
    url: str,
    token: str | None,
    *,
    body: bytes | None = None,
) -> tuple[int, dict[str, object]]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=_body() if body is None else body, method="POST", headers=headers)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def test_session_token_is_server_and_tenant_bound_and_missing_fails_closed() -> None:
    first = LoopbackHttpControl(
        _Adapter(),
        identity=AuthIdentity("tenant-a", "alice", "http-loopback", "session-a"),
    )
    second = LoopbackHttpControl(
        _Adapter(),
        identity=AuthIdentity("tenant-b", "bob", "http-loopback", "session-b"),
    )
    first_access = first.start()
    second_access = second.start()
    try:
        missing, missing_body = _post(first_access.url, None)
        cross_tenant, cross_body = _post(second_access.url, first_access.bearer_token)
        valid, _ = _post(second_access.url, second_access.bearer_token)
    finally:
        first.close()
        second.close()

    assert (missing, cross_tenant, valid) == (401, 401, 200)
    assert missing_body["error"]["code"] == "invalid_bearer_token"  # type: ignore[index]
    assert cross_body["error"]["code"] == "invalid_bearer_token"  # type: ignore[index]
    serialized = json.dumps((missing_body, cross_body))
    assert first_access.bearer_token not in serialized
    assert second_access.bearer_token not in serialized


def test_signed_request_token_expires_and_detects_replay() -> None:
    now = [1_000.0]
    authenticator = LoopbackAuthenticator(
        AuthIdentity("tenant", "operator", "http-loopback", "signed"),
        clock=lambda: now[0],
    )
    control = LoopbackHttpControl(_Adapter(), authenticator=authenticator)
    access = control.start()
    try:
        replay_token = control.issue_request_token(ttl_seconds=10)
        first, _ = _post(access.url, replay_token)
        replay, replay_body = _post(access.url, replay_token)
        expired_token = control.issue_request_token(ttl_seconds=1)
        now[0] += 2
        expired, expired_body = _post(access.url, expired_token)
    finally:
        control.close()

    assert (first, replay, expired) == (200, 401, 401)
    assert replay_body["error"]["code"] == "authentication_replayed"  # type: ignore[index]
    assert expired_body["error"]["code"] == "authentication_expired"  # type: ignore[index]
    assert replay_token not in json.dumps(replay_body)
    assert expired_token not in json.dumps(expired_body)


def test_one_time_token_is_consumed_atomically_under_concurrency() -> None:
    control = LoopbackHttpControl(
        _Adapter(),
        identity=AuthIdentity("tenant", "operator", "http-loopback", "concurrent"),
    )
    access = control.start()
    token = control.issue_request_token()
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            statuses = list(pool.map(lambda _index: _post(access.url, token)[0], range(24)))
    finally:
        control.close()

    assert statuses.count(200) == 1
    assert statuses.count(401) == 23


def test_signed_identity_payload_cannot_be_forged() -> None:
    identity = AuthIdentity("tenant-a", "alice", "http-loopback", "session")
    authenticator = LoopbackAuthenticator(identity)
    credential = authenticator.start_session()
    token = authenticator.issue_one_time()
    prefix, payload, signature = token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["tenant"] = "tenant-b"
    forged_payload = base64.urlsafe_b64encode(
        json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()

    try:
        with pytest.raises(CapabilityHubError) as denied:
            authenticator.authenticate(f"Bearer {prefix}.{forged_payload}.{signature}")
        assert denied.value.code == "invalid_bearer_token"
        assert authenticator.authenticate(f"Bearer {credential.token}") == identity
    finally:
        authenticator.close()


def test_runtime_rejects_client_supplied_tenant_and_principal_scope(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    control, access = local_http_control(
        project_root=project,
        tenant_id="trusted-tenant",
        principal_id="trusted-principal",
    )
    handshake = protocol_handshake()
    body = json.dumps(
        {
            "request_id": "forged-request",
            "correlation_id": "forged-correlation",
            "operation": "capability.search",
            "payload": {
                "query": "anything",
                "task_id": "task",
                "tenant_id": "attacker-tenant",
                "principal_id": "attacker-principal",
            },
            "handshake": {
                "api_versions": list(handshake.api_versions),
                "supported_features": list(handshake.supported_features),
                "required_features": list(handshake.required_features),
            },
        }
    ).encode()
    try:
        status, response = _post(access.url, access.bearer_token, body=body)
    finally:
        control.close()

    assert status == 400
    assert response["error"]["code"] == "invalid_adapter_payload"  # type: ignore[index]
    assert "attacker-tenant" not in json.dumps(response)
    assert "attacker-principal" not in json.dumps(response)


def test_rejected_credentials_are_not_logged(capsys) -> None:
    control = LoopbackHttpControl(_Adapter())
    access = control.start()
    canary = "SECRET-BEARER-CANARY"
    try:
        status, response = _post(access.url, canary)
    finally:
        control.close()

    captured = capsys.readouterr()
    assert status == 401
    assert canary not in captured.out
    assert canary not in captured.err
    assert canary not in json.dumps(response)
