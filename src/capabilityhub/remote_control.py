"""Optional mutually-authenticated TLS deployment profile with split planes."""

from __future__ import annotations

import hashlib
import json
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread, current_thread
from typing import Any

from capabilityhub.admin_control import AdminBackend
from capabilityhub.auth import AuthIdentity
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import JsonValue
from capabilityhub.protocol import AdapterContract, AdapterKind, parse_request, success_response

_DATA_OPERATIONS = frozenset(("capability.search", "capability.load", "capability.execute"))
_ROLE_OPERATIONS = {
    "auditor": frozenset(("audit.query",)),
    "approver": frozenset(("approval.list", "approval.decide")),
    "lifecycle-operator": frozenset(
        (
            "lifecycle.list",
            "lifecycle.set",
            "update.list",
            "update.stage",
            "update.health",
            "update.activate",
            "update.rollback",
        )
    ),
    "policy-admin": frozenset(("policy.query", "policy.set")),
}


@dataclass(frozen=True, slots=True)
class TlsFiles:
    certificate: Path
    private_key: Path
    client_ca: Path

    def __repr__(self) -> str:
        return "TlsFiles(certificate=<configured>, private_key=<redacted>, client_ca=<configured>)"


@dataclass(frozen=True, slots=True)
class RemotePrincipal:
    certificate_sha256: str
    tenant_id: str
    principal_id: str
    audience: str
    roles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if len(self.certificate_sha256) != 64 or any(
            item not in "0123456789abcdef" for item in self.certificate_sha256
        ):
            raise ValueError("certificate fingerprint must be lowercase sha256")
        if self.audience not in {"data", "admin"}:
            raise ValueError("remote principal audience must be data or admin")
        if self.audience == "data" and self.roles:
            raise ValueError("data principals cannot hold administration roles")
        if self.audience == "admin" and (
            not self.roles or not self.roles <= _ROLE_OPERATIONS.keys()
        ):
            raise ValueError("admin principals require known roles")


@dataclass(frozen=True, slots=True)
class RemoteTlsAccess:
    data_url: str
    admin_url: str


class RemoteTlsControl:
    """Serve data and management on distinct mTLS listeners; local defaults are untouched."""

    def __init__(
        self,
        data_adapter: AdapterContract,
        admin_backend: AdminBackend,
        *,
        tls: TlsFiles,
        principals: tuple[RemotePrincipal, ...],
        host: str = "127.0.0.1",
        data_port: int = 0,
        admin_port: int = 0,
        max_body_bytes: int = 65_536,
        data_adapter_provider: Callable[[RemotePrincipal], AdapterContract] | None = None,
    ) -> None:
        if data_adapter.kind is not AdapterKind.HTTP:
            raise ValueError("remote data control requires an HTTP adapter")
        if not principals or len({item.certificate_sha256 for item in principals}) != len(
            principals
        ):
            raise ValueError("remote certificate identities must be non-empty and unique")
        if not 1 <= max_body_bytes <= 1_048_576:
            raise ValueError("max_body_bytes must be from 1 to 1048576")
        self._adapter = data_adapter
        self._data_adapter_provider = data_adapter_provider
        self._admin = admin_backend
        self._tls = tls
        self._principals = {item.certificate_sha256: item for item in principals}
        self._host = host
        self._data_port = data_port
        self._admin_port = admin_port
        self._max_body_bytes = max_body_bytes
        self._servers: list[ThreadingHTTPServer] = []
        self._threads: list[Thread] = []
        self._lock = RLock()

    def start(self) -> RemoteTlsAccess:
        with self._lock:
            if self._servers:
                raise RuntimeError("remote TLS control is already running")
            context = _server_context(self._tls)
            data = _server(
                self._host,
                self._data_port,
                partial(_RemoteHandler, control=self, plane="data"),
                context,
            )
            try:
                admin = _server(
                    self._host,
                    self._admin_port,
                    partial(_RemoteHandler, control=self, plane="admin"),
                    context,
                )
            except Exception:
                data.server_close()
                raise
            self._servers = [data, admin]
            for plane, server in (("data", data), ("admin", admin)):
                thread = Thread(
                    target=server.serve_forever, name=f"capabilityhub-remote-{plane}", daemon=True
                )
                self._threads.append(thread)
                thread.start()
            return RemoteTlsAccess(
                f"https://{self._host}:{data.server_address[1]}/protocol",
                f"https://{self._host}:{admin.server_address[1]}/admin",
            )

    def close(self) -> None:
        with self._lock:
            servers, self._servers = self._servers, []
            threads, self._threads = self._threads, []
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            if thread is not current_thread():
                thread.join(timeout=5)

    def _principal(self, certificate: bytes, plane: str) -> RemotePrincipal:
        fingerprint = hashlib.sha256(certificate).hexdigest()
        principal = self._principals.get(fingerprint)
        if principal is None or principal.audience != plane:
            raise _remote_error("remote_identity_audience_denied", ErrorCategory.POLICY)
        return principal

    def _data(
        self, raw: Mapping[str, Any], principal: RemotePrincipal
    ) -> dict[str, JsonValue]:
        adapter = (
            self._adapter
            if self._data_adapter_provider is None
            else self._data_adapter_provider(principal)
        )
        if adapter.kind is not AdapterKind.HTTP or adapter.handshake != self._adapter.handshake:
            raise _remote_error("remote_data_adapter_invalid", ErrorCategory.INTERNAL)
        request = parse_request(AdapterKind.HTTP, raw, server_handshake=adapter.handshake)
        if request.operation not in _DATA_OPERATIONS:
            raise _remote_error("remote_data_operation_denied", ErrorCategory.POLICY)
        if request.stream or request.cancel_target is not None:
            raise _remote_error("remote_terminal_mode_required", ErrorCategory.INPUT)
        return success_response(request, adapter.dispatch(request)).as_dict()

    def _management(
        self, raw: Mapping[str, Any], principal: RemotePrincipal
    ) -> dict[str, JsonValue]:
        if set(raw) != {"request_id", "operation", "payload"}:
            raise _remote_error("management_envelope_invalid", ErrorCategory.INPUT)
        request_id, operation, payload = (
            raw.get("request_id"),
            raw.get("operation"),
            raw.get("payload"),
        )
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise _remote_error("management_envelope_invalid", ErrorCategory.INPUT)
        allowed = frozenset().union(*(_ROLE_OPERATIONS[role] for role in principal.roles))
        if not isinstance(operation, str) or operation not in allowed:
            raise _remote_error("remote_admin_role_denied", ErrorCategory.POLICY)
        if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
            raise _remote_error("management_envelope_invalid", ErrorCategory.INPUT)
        identity = AuthIdentity(
            principal.tenant_id,
            principal.principal_id,
            "remote-mtls-admin",
            "remote-admin",
        )
        result = self._admin.dispatch(operation, payload, identity)
        return {"ok": True, "request_id": request_id, "result": result}


class _RemoteHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(
        self, *args: object, control: RemoteTlsControl, plane: str, **kwargs: object
    ) -> None:
        self._control = control
        self._plane = plane
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def do_POST(self) -> None:
        expected = "/protocol" if self._plane == "data" else "/admin"
        try:
            if self.path != expected:
                raise _remote_error("remote_plane_route_denied", ErrorCategory.POLICY)
            certificate = self.connection.getpeercert(binary_form=True)
            principal = self._control._principal(certificate, self._plane)
            if self.headers.get_content_type() != "application/json":
                raise _remote_error("json_content_type_required", ErrorCategory.INPUT)
            length = int(self.headers.get("Content-Length", "-1"))
            if not 1 <= length <= self._control._max_body_bytes:
                raise _remote_error("request_body_invalid", ErrorCategory.INPUT)
            raw = json.loads(self.rfile.read(length))
            if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
                raise ValueError
            result = (
                self._control._data(raw, principal)
                if self._plane == "data"
                else self._control._management(raw, principal)
            )
        except CapabilityHubError as error:
            self._send(
                HTTPStatus.FORBIDDEN
                if error.category is ErrorCategory.POLICY
                else HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": {"code": error.code}},
            )
            return
        except Exception:
            self._send(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": {"code": "remote_request_invalid"}}
            )
            return
        self._send(HTTPStatus.OK, result)

    def do_GET(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": {"code": "method_denied"}})

    def _send(self, status: HTTPStatus, payload: Mapping[str, JsonValue]) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _server(host: str, port: int, handler: Any, context: ssl.SSLContext) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def _server_context(files: TlsFiles) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_cert_chain(files.certificate, files.private_key)
        context.load_verify_locations(cafile=files.client_ca)
    except (OSError, ssl.SSLError) as error:
        raise RuntimeError("remote TLS material could not be loaded") from error
    return context


def _remote_error(code: str, category: ErrorCategory) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=category,
        safe_message="The remote control request was rejected.",
    )
