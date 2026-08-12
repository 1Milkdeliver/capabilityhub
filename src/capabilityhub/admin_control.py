"""Independent authenticated loopback administration plane."""

from __future__ import annotations

import json
import secrets
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock, Thread, current_thread
from typing import Protocol, cast
from urllib.parse import urlsplit

from capabilityhub.audit import AuditEvent, AuditSink
from capabilityhub.auth import AuthIdentity, LoopbackAuthenticator
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import JsonValue

_LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1"))
_ROLE_OPERATIONS: Mapping[str, frozenset[str]] = {
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
_ALL_OPERATIONS = frozenset().union(*_ROLE_OPERATIONS.values())


class AdminBackend(Protocol):
    def dispatch(
        self, operation: str, payload: Mapping[str, JsonValue], identity: AuthIdentity
    ) -> JsonValue: ...


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    identity: AuthIdentity
    roles: frozenset[str]

    def __post_init__(self) -> None:
        if self.identity.source not in {
            "admin-loopback",
            "admin-cli",
            "admin-dashboard",
        }:
            raise ValueError("admin identity source must be a trusted admin entry")
        if not self.roles or not self.roles <= _ROLE_OPERATIONS.keys():
            raise ValueError("admin roles must be known and non-empty")

    @property
    def allowed_operations(self) -> frozenset[str]:
        return frozenset().union(*(_ROLE_OPERATIONS[role] for role in self.roles))


@dataclass(frozen=True, slots=True)
class AdminControlAccess:
    url: str
    bearer_token: str = field(repr=False)
    expires_in_seconds: int = 60


@dataclass(frozen=True, slots=True)
class AdminRequestEnvelope:
    request_id: str
    operation: str
    payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.request_id or len(self.request_id) > 128:
            raise ValueError("admin request_id is invalid")


class AuthenticatedAdminDispatcher:
    """One role check, identity injection, and audit path for every local admin entry."""

    def __init__(
        self,
        backend: AdminBackend,
        principal: AdminPrincipal,
        *,
        audit: AuditSink,
    ) -> None:
        self._backend = backend
        self._principal = principal
        self._audit = audit
        self._lock = RLock()
        self._sequence = 0

    def dispatch(self, request: AdminRequestEnvelope) -> JsonValue:
        if request.operation not in _ALL_OPERATIONS:
            raise _admin_error("admin_operation_unsupported", ErrorCategory.INPUT)
        if request.operation not in self._principal.allowed_operations:
            raise _admin_error("admin_role_denied", ErrorCategory.POLICY)
        try:
            result = self._backend.dispatch(
                request.operation,
                request.payload,
                self._principal.identity,
            )
        except Exception:
            self._emit(request, "failure")
            raise
        self._emit(request, "success")
        return result

    def _emit(self, request: AdminRequestEnvelope, outcome: str) -> None:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        self._audit.emit(
            AuditEvent(
                event_id=f"admin-{request.request_id}-{sequence:08d}",
                sequence=sequence,
                task_id="admin-control",
                event_type=request.operation,
                capability_revision=None,
                outcome=outcome,
                reason_codes=(f"admin_authenticated:{self._principal.identity.source}",),
            )
        )


class LoopbackAdminControl:
    """Serve management-only operations with short-lived, single-use credentials."""

    def __init__(
        self,
        backend: AdminBackend,
        principal: AdminPrincipal,
        *,
        audit: AuditSink,
        host: str = "127.0.0.1",
        port: int = 0,
        max_body_bytes: int = 65_536,
        token_ttl_seconds: int = 60,
        authenticator: LoopbackAuthenticator | None = None,
    ) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError("admin control host must be loopback")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
            raise ValueError("admin control port must be from 0 to 65535")
        if not 1 <= max_body_bytes <= 1_048_576:
            raise ValueError("max_body_bytes must be from 1 to 1048576")
        if not 1 <= token_ttl_seconds <= 300:
            raise ValueError("token_ttl_seconds must be from 1 to 300")
        self._backend = backend
        self._principal = principal
        self._audit = audit
        self._dispatcher = AuthenticatedAdminDispatcher(backend, principal, audit=audit)
        self._host = host
        self._requested_port = port
        self._max_body_bytes = max_body_bytes
        self._token_ttl = token_ttl_seconds
        self._authenticator = authenticator or LoopbackAuthenticator(principal.identity)
        if self._authenticator.identity != principal.identity:
            raise ValueError("admin authenticator identity must match principal")
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        self._lock = RLock()
        self._sequence = 0

    def start(self) -> AdminControlAccess:
        with self._lock:
            if self._server is not None:
                raise RuntimeError("admin control is already running")
            self._authenticator.start_session()
            handler = partial(_AdminHandler, control=self)
            server_type = _IPv6ThreadingHTTPServer if self._host == "::1" else ThreadingHTTPServer
            try:
                server = server_type((self._host, self._requested_port), handler)
            except Exception:
                self._authenticator.close()
                raise
            server.daemon_threads = True
            thread = Thread(target=server.serve_forever, name="capabilityhub-admin", daemon=True)
            self._server = server
            self._thread = thread
            thread.start()
            host = f"[{self._host}]" if ":" in self._host else self._host
            port = int(server.server_address[1])
            return AdminControlAccess(
                f"http://{host}:{port}/admin",
                self.issue_request_token(),
                self._token_ttl,
            )

    def issue_request_token(self, *, ttl_seconds: int | None = None) -> str:
        return self._authenticator.issue_one_time(
            ttl_seconds=self._token_ttl if ttl_seconds is None else ttl_seconds
        )

    def close(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._authenticator.close()
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None and thread is not current_thread():
            thread.join(timeout=5)

    def _dispatch(self, operation: object, payload: object) -> JsonValue:
        if not isinstance(operation, str) or operation not in _ALL_OPERATIONS:
            raise _admin_error("admin_operation_unsupported", ErrorCategory.INPUT)
        if operation not in self._principal.allowed_operations:
            raise _admin_error("admin_role_denied", ErrorCategory.POLICY)
        if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
            raise _admin_error("admin_payload_invalid", ErrorCategory.INPUT)
        return self._dispatcher.dispatch(
            AdminRequestEnvelope(
                secrets.token_hex(16),
                operation,
                cast(Mapping[str, JsonValue], payload),
            )
        )

    def _emit(self, operation: str, outcome: str) -> None:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        self._audit.emit(
            AuditEvent(
                event_id=f"admin-{sequence:08d}",
                sequence=sequence,
                task_id="admin-control",
                event_type=operation,
                capability_revision=None,
                outcome=outcome,
                reason_codes=("admin_authenticated",),
            )
        )


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class _AdminHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: object, control: LoopbackAdminControl, **kwargs: object) -> None:
        self._control = control
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def do_POST(self) -> None:
        correlation = f"admin-{secrets.token_hex(16)}"
        if self.path != "/admin" or not self._valid_peer_and_host():
            self._send(HTTPStatus.NOT_FOUND, False, correlation, error="admin_route_denied")
            return
        length = self._content_length()
        if length is None or length > self._control._max_body_bytes:
            self._send(HTTPStatus.BAD_REQUEST, False, correlation, error="admin_request_invalid")
            return
        authorization = self.headers.get_all("Authorization", ())
        try:
            if len(authorization) != 1:
                raise _admin_error("invalid_bearer_token", ErrorCategory.POLICY)
            self._control._authenticator.authenticate(authorization[0])
        except CapabilityHubError as error:
            self.rfile.read(length)
            self._send(HTTPStatus.UNAUTHORIZED, False, correlation, error=error.code)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            self.rfile.read(length)
            self._send(HTTPStatus.BAD_REQUEST, False, correlation, error="admin_request_invalid")
            return
        try:
            raw = json.loads(self.rfile.read(length))
            if not isinstance(raw, dict) or set(raw) != {"request_id", "operation", "payload"}:
                raise ValueError
            request_id = raw["request_id"]
            if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
                raise ValueError
            result = self._control._dispatch(raw["operation"], raw["payload"])
        except CapabilityHubError as error:
            status = (
                HTTPStatus.FORBIDDEN
                if error.category is ErrorCategory.POLICY
                else HTTPStatus.BAD_REQUEST
            )
            self._send(status, False, correlation, error=error.code)
            return
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send(HTTPStatus.BAD_REQUEST, False, correlation, error="admin_request_invalid")
            return
        except Exception:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, False, correlation, error="admin_failed")
            return
        self._send(HTTPStatus.OK, True, correlation, result=result)

    def do_GET(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, False, "admin-unavailable", error="method_denied")

    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET

    def _content_length(self) -> int | None:
        if self.headers.get("Transfer-Encoding") is not None:
            return None
        values = self.headers.get_all("Content-Length", ())
        try:
            value = int(values[0]) if len(values) == 1 else -1
        except ValueError:
            return None
        return value if value >= 0 else None

    def _valid_peer_and_host(self) -> bool:
        if not self.client_address or str(self.client_address[0]) not in _LOOPBACK_HOSTS:
            return False
        values = self.headers.get_all("Host", ())
        if len(values) != 1:
            return False
        try:
            parsed = urlsplit("//" + values[0])
            address = self.server.server_address
            if not isinstance(address, tuple) or len(address) < 2:
                return False
            return (
                parsed.hostname in _LOOPBACK_HOSTS
                and parsed.port == int(address[1])
                and not parsed.username
                and not parsed.password
            )
        except (TypeError, ValueError):
            return False

    def _send(
        self,
        status: HTTPStatus,
        ok: bool,
        correlation: str,
        *,
        result: JsonValue | None = None,
        error: str | None = None,
    ) -> None:
        body: dict[str, JsonValue] = {"correlation_id": correlation, "ok": ok}
        if ok:
            body["result"] = result
        else:
            body["error"] = {"code": error or "admin_failed"}
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *args: object) -> None:
        return


def _admin_error(code: str, category: ErrorCategory) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=category,
        safe_message="The administration request was rejected.",
    )
