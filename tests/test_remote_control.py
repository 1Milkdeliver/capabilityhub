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
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.auth import AuthIdentity
from capabilityhub.budget import BudgetLedger
from capabilityhub.compatibility import FeatureHandshake
from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
    OperationSpec,
    OperationType,
)
from capabilityhub.protocol import (
    AdapterKind,
    ConformanceFixture,
    RequestEnvelope,
    protocol_handshake,
    run_conformance_suite,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.remote_control import (
    RemotePrincipal,
    RemoteTlsControl,
    TlsFiles,
)
from capabilityhub.service import CapabilityHubService, ServiceContext
from capabilityhub.service_adapter import CapabilityHubServiceAdapter


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


class _CountingProvider:
    name = "remote-fixture"

    def __init__(self, manifest: CapabilityManifest) -> None:
        self.manifest = manifest
        self.calls: list[str] = []

    def discover(self) -> tuple[CapabilityManifest, ...]:
        return (self.manifest,)

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        self.calls.append(context.tenant_id)
        return ExecutionResult(
            identity.revision,
            request.operation,
            {"tenant": context.tenant_id},
            self.name,
            1,
            "remote-fixture-audit",
        )


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


def test_mtls_data_certificates_isolate_refs_budgets_and_idempotency(tmp_path: Path) -> None:
    tls, clients = _certificates(tmp_path)
    first_der, first_cert, first_key = clients["data"]
    second_der, second_cert, second_key = clients["data-b"]
    manifest = CapabilityManifest(
        CapabilityIdentity("remote", "fixture", "1", "sha256:" + "a" * 64),
        CapabilityKind.API,
        "Remote tenant fixture.",
        "remote-fixture",
        (OperationSpec("read", OperationType.EXECUTE),),
    )
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    provider = _CountingProvider(manifest)
    service = CapabilityHubService(
        registry=registry,
        providers=(provider,),
        references=ReferenceSigner(b"remote-tenant-reference-key-material"),
        audit=MemoryAuditSink(),
    )
    adapters: dict[str, CapabilityHubServiceAdapter] = {}
    budgets: dict[tuple[str, str], BudgetLedger] = {}

    def adapter_for(principal: RemotePrincipal) -> CapabilityHubServiceAdapter:
        def budget(task_id: str) -> BudgetLedger:
            return budgets.setdefault(
                (principal.certificate_sha256, task_id),
                BudgetLedger(
                    task_id,
                    {"bytes": 50_000, "loads": 1, "executions": 4, "portable_tokens": 5_000},
                ),
            )

        return adapters.setdefault(
            principal.certificate_sha256,
            CapabilityHubServiceAdapter(
                service,
                kind=AdapterKind.HTTP,
                context_provider=lambda: ServiceContext(
                    principal.tenant_id, principal.principal_id, "remote-data"
                ),
                budget_provider=budget,
            ),
        )

    template = adapter_for(
        RemotePrincipal(hashlib.sha256(first_der).hexdigest(), "tenant-a", "caller", "data")
    )
    control = RemoteTlsControl(
        template,
        _Admin(),
        tls=tls,
        principals=(
            RemotePrincipal(
                hashlib.sha256(first_der).hexdigest(), "tenant-a", "caller", "data"
            ),
            RemotePrincipal(
                hashlib.sha256(second_der).hexdigest(), "tenant-b", "caller", "data"
            ),
        ),
        data_adapter_provider=adapter_for,
    )
    access = control.start()
    try:
        first_ref = _search_and_load(access.data_url, tls.client_ca, first_cert, first_key)
        cross_status, cross = _post(
            access.data_url,
            _envelope(
                "capability.load",
                {"capability_ref": first_ref[0], "task_id": "shared-task"},
            ),
            tls.client_ca,
            second_cert,
            second_key,
        )
        second_ref = _search_and_load(access.data_url, tls.client_ca, second_cert, second_key)
        exhausted_status, exhausted = _post(
            access.data_url,
            _envelope(
                "capability.load",
                {"capability_ref": first_ref[0], "task_id": "shared-task"},
            ),
            tls.client_ca,
            first_cert,
            first_key,
        )
        for certificate, key, execution_ref in (
            (first_cert, first_key, first_ref[1]),
            (second_cert, second_key, second_ref[1]),
            (first_cert, first_key, first_ref[1]),
        ):
            status, _ = _post(
                access.data_url,
                _envelope(
                    "capability.execute",
                    {
                        "execution_ref": execution_ref,
                        "operation": "read",
                        "arguments": {},
                        "task_id": "shared-task",
                        "idempotency_key": "same-key",
                    },
                ),
                tls.client_ca,
                certificate,
                key,
            )
            assert status == 200
    finally:
        control.close()

    assert cross_status == 400
    assert cross["error"] == {"code": "reference_scope_mismatch"}
    assert exhausted_status == 400
    assert exhausted["error"] == {"code": "budget_exhausted"}
    assert provider.calls == ["tenant-a", "tenant-b"]


def test_remote_data_rejects_one_adapter_reused_across_certificates(tmp_path: Path) -> None:
    tls, clients = _certificates(tmp_path)
    first_der, _, _ = clients["data"]
    second_der, _, _ = clients["data-b"]
    shared = _Adapter()
    first = RemotePrincipal(
        hashlib.sha256(first_der).hexdigest(), "tenant-a", "caller", "data"
    )
    second = RemotePrincipal(
        hashlib.sha256(second_der).hexdigest(), "tenant-b", "caller", "data"
    )
    control = RemoteTlsControl(
        shared,
        _Admin(),
        tls=tls,
        principals=(first, second),
        data_adapter_provider=lambda _principal: shared,
    )

    control._data(_envelope("capability.search"), first)
    with pytest.raises(CapabilityHubError) as rejected:
        control._data(_envelope("capability.search"), second)
    assert rejected.value.code == "remote_data_adapter_identity_mismatch"


def _envelope(
    operation: str, payload: dict[str, object] | None = None
) -> dict[str, object]:
    handshake = protocol_handshake()
    return {
        "correlation_id": "correlation-1",
        "handshake": {
            "api_versions": list(handshake.api_versions),
            "required_features": list(handshake.required_features),
            "supported_features": list(handshake.supported_features),
        },
        "operation": operation,
        "payload": payload or {},
        "request_id": "request-1",
    }


def _search_and_load(
    url: str, ca: Path, certificate: Path, private_key: Path
) -> tuple[str, str]:
    status, search = _post(
        url,
        _envelope("capability.search", {"query": "remote", "task_id": "shared-task"}),
        ca,
        certificate,
        private_key,
    )
    assert status == 200
    search_result = cast(dict[str, object], search["result"])
    capability_ref = cast(list[dict[str, str]], search_result["cards"])[0]["capability_ref"]
    status, loaded = _post(
        url,
        _envelope(
            "capability.load",
            {"capability_ref": capability_ref, "task_id": "shared-task"},
        ),
        ca,
        certificate,
        private_key,
    )
    assert status == 200
    loaded_result = cast(dict[str, object], loaded["result"])
    return capability_ref, cast(str, loaded_result["execution_ref"])


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
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CapSift test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
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
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        ExtendedKeyUsageOID.SERVER_AUTH
                        if server
                        else ExtendedKeyUsageOID.CLIENT_AUTH
                    ]
                ),
                critical=False,
            )
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
        "data-b": issue("data-b"),
    }
