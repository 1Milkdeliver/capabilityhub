from __future__ import annotations

import datetime
import hashlib
import ipaddress
import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, Request, build_opener

import pytest

pytest.importorskip("cryptography")
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from capabilityhub.auth import AuthIdentity
from capabilityhub.compatibility import FeatureHandshake
from capabilityhub.models import JsonValue
from capabilityhub.protocol import (
    AdapterKind,
    ConformanceFixture,
    RequestEnvelope,
    protocol_handshake,
    run_conformance_suite,
)
from capabilityhub.remote_control import (
    RemotePrincipal,
    RemoteTlsControl,
    TlsFiles,
)


@dataclass
class _Adapter:
    kind: AdapterKind = AdapterKind.HTTP
    handshake: FeatureHandshake = field(default_factory=protocol_handshake)

    def dispatch(self, request: RequestEnvelope) -> JsonValue:
        return {"operation": request.operation}

    def cancel(self, correlation_id: str) -> bool:
        return False


class _Admin:
    def __init__(self) -> None:
        self.identity: AuthIdentity | None = None

    def dispatch(
        self, operation: str, payload: Mapping[str, JsonValue], identity: AuthIdentity
    ) -> JsonValue:
        self.identity = identity
        return {"operation": operation, "principal": identity.principal_id}


def test_real_mtls_split_planes_roles_and_adapter_conformance(tmp_path: Path) -> None:
    tls, clients = _certificates(tmp_path)
    data_der, data_cert, data_key = clients["data"]
    admin_der, admin_cert, admin_key = clients["admin"]
    adapter = _Adapter()
    backend = _Admin()
    data_identities: list[RemotePrincipal] = []

    def data_adapter(principal: RemotePrincipal) -> _Adapter:
        data_identities.append(principal)
        return adapter

    control = RemoteTlsControl(
        adapter,
        backend,
        tls=tls,
        principals=(
            RemotePrincipal(hashlib.sha256(data_der).hexdigest(), "tenant", "caller", "data"),
            RemotePrincipal(
                hashlib.sha256(admin_der).hexdigest(),
                "tenant",
                "approver-7",
                "admin",
                frozenset(("approver",)),
            ),
        ),
        data_adapter_provider=data_adapter,
    )
    access = control.start()
    try:
        status, data = _post(
            access.data_url,
            _envelope("capability.search"),
            tls.client_ca,
            data_cert,
            data_key,
        )
        admin_status, admin = _post(
            access.admin_url,
            {"request_id": "admin-1", "operation": "approval.list", "payload": {}},
            tls.client_ca,
            admin_cert,
            admin_key,
        )
        crossed_status, crossed = _post(
            access.admin_url,
            {"request_id": "bad", "operation": "approval.list", "payload": {}},
            tls.client_ca,
            data_cert,
            data_key,
        )
    finally:
        control.close()

    assert status == 200
    assert data["result"] == {"operation": "capability.search"}
    assert data_identities == [
        RemotePrincipal(hashlib.sha256(data_der).hexdigest(), "tenant", "caller", "data")
    ]
    assert admin_status == 200
    assert admin["result"] == {"operation": "approval.list", "principal": "approver-7"}
    assert backend.identity is not None and backend.identity.source == "remote-mtls-admin"
    assert crossed_status == 403
    assert crossed["error"] == {"code": "remote_identity_audience_denied"}
    assert "PRIVATE" not in repr(tls)

    fixture = ConformanceFixture(
        "search", "capability.search", {}, {"operation": "capability.search"}
    )
    for kind in AdapterKind:
        compatible = _Adapter(kind=kind)
        assert run_conformance_suite(compatible, (fixture,), server_handshake=compatible.handshake)[
            0
        ].passed


def _envelope(operation: str) -> dict[str, object]:
    handshake = protocol_handshake()
    return {
        "correlation_id": "correlation-1",
        "handshake": {
            "api_versions": list(handshake.api_versions),
            "required_features": list(handshake.required_features),
            "supported_features": list(handshake.supported_features),
        },
        "operation": operation,
        "payload": {},
        "request_id": "request-1",
    }


def _post(
    url: str,
    payload: dict[str, object],
    ca: Path,
    certificate: Path,
    private_key: Path,
) -> tuple[int, dict[str, object]]:
    context = ssl.create_default_context(cafile=ca)
    context.load_cert_chain(certificate, private_key)
    opener = build_opener(HTTPSHandler(context=context))
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = opener.open(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, cast(dict[str, object], json.loads(response.read()))


def _certificates(
    root: Path,
) -> tuple[TlsFiles, dict[str, tuple[bytes, Path, Path]]]:
    now = datetime.datetime.now(datetime.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CapabilityHub test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = root / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))

    def issue(name: str, *, server: bool = False) -> tuple[bytes, Path, Path]:
        key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
        )
        if server:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
        certificate = builder.sign(ca_key, hashes.SHA256())
        cert_path, key_path = root / f"{name}.pem", root / f"{name}.key"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        return certificate.public_bytes(serialization.Encoding.DER), cert_path, key_path

    _server_der, server_cert, server_key = issue("server", server=True)
    return TlsFiles(server_cert, server_key, ca_path), {
        "admin": issue("admin"),
        "data": issue("data"),
    }
