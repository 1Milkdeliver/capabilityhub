"""Authenticated loopback-only HTTP adapter for the shared protocol envelope."""

from __future__ import annotations

import json
import secrets
import socket
from dataclasses import dataclass, field
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock, Thread, current_thread
from typing import Any
from urllib.parse import urlsplit

from .auth import AuthIdentity, LoopbackAuthenticator
from .errors import CapabilityHubError, ErrorCategory
from .protocol import (
    AdapterContract,
    AdapterKind,
    ResponseEnvelope,
    error_response,
    parse_request,
    success_response,
)

_OPERATIONS = frozenset(("capability.search", "capability.load", "capability.execute"))
_LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1"))


@dataclass(frozen=True, slots=True)
class HttpControlAccess:
    url: str
    bearer_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class HttpControlStatus:
    host: str
    port: int
    running: bool


class LoopbackHttpControl:
    """Serve the transport-neutral contract over authenticated loopback HTTP."""

    def __init__(
        self,
        adapter: AdapterContract,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        max_body_bytes: int = 65_536,
        allowed_origins: tuple[str, ...] = (),
        identity: AuthIdentity | None = None,
        authenticator: LoopbackAuthenticator | None = None,
    ) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError("HTTP control host must be 127.0.0.1 or ::1")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
            raise ValueError("HTTP control port must be from 0 to 65535")
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or not 1 <= max_body_bytes <= 1_048_576
        ):
            raise ValueError("max_body_bytes must be from 1 to 1048576")
        if adapter.kind is not AdapterKind.HTTP:
            raise ValueError("HTTP control requires an HTTP adapter contract")
        origins = frozenset(_validate_origin(item) for item in allowed_origins)
        self.adapter = adapter
        self._host = host
        self._requested_port = port
        self._max_body_bytes = max_body_bytes
        self._allowed_origins = origins
        selected_identity = identity or AuthIdentity("local", "operator", "http-loopback", "http")
        if authenticator is not None and identity is not None:
            raise ValueError("supply identity or authenticator, not both")
        self._authenticator = authenticator or LoopbackAuthenticator(selected_identity)
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        self._lifecycle_lock = RLock()

    def start(self) -> HttpControlAccess:
        """Start once and return the bearer token only to the host caller."""

        with self._lifecycle_lock:
            if self._server is not None:
                raise RuntimeError("HTTP control is already running")
            credential = self._authenticator.start_session()
            handler = partial(_ControlHandler, control=self)
            server_type = _IPv6ThreadingHTTPServer if self._host == "::1" else ThreadingHTTPServer
            try:
                server = server_type((self._host, self._requested_port), handler)
            except Exception:
                self._authenticator.close()
                raise
            server.daemon_threads = True
            thread = Thread(
                target=server.serve_forever,
                name="capabilityhub-http-control",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()
            port = int(server.server_address[1])
            host = f"[{self._host}]" if ":" in self._host else self._host
            return HttpControlAccess(f"http://{host}:{port}/protocol", credential.token)

    def status(self) -> HttpControlStatus:
        with self._lifecycle_lock:
            server = self._server
            return HttpControlStatus(
                host=self._host,
                port=int(server.server_address[1]) if server is not None else 0,
                running=server is not None,
            )

    def close(self) -> None:
        """Stop safely; repeated and concurrent closes are no-ops."""

        with self._lifecycle_lock:
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

    def _authorized(self, value: str | None) -> bool:
        try:
            self._authenticator.authenticate(value)
        except CapabilityHubError:
            return False
        return True

    def issue_request_token(self, *, ttl_seconds: int = 60) -> str:
        """Issue a short-lived, single-use token bound to this control identity."""

        return self._authenticator.issue_one_time(ttl_seconds=ttl_seconds)

    def _dispatch(self, raw: dict[str, Any]) -> ResponseEnvelope:
        request = parse_request(
            AdapterKind.HTTP,
            raw,
            server_handshake=self.adapter.handshake,
        )
        if request.operation not in _OPERATIONS:
            raise CapabilityHubError(
                code="unsupported_operation",
                category=ErrorCategory.INPUT,
                safe_message="The protocol operation is unsupported.",
            )
        if request.stream or request.cancel_target is not None:
            raise CapabilityHubError(
                code="unsupported_http_mode",
                category=ErrorCategory.INPUT,
                safe_message="HTTP control supports terminal requests only.",
            )
        output = self.adapter.dispatch(request)
        return success_response(request, output)


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class _ControlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: object, control: LoopbackHttpControl, **kwargs: object) -> None:
        self._control = control
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def do_POST(self) -> None:
        correlation = f"http-{secrets.token_hex(16)}"
        if self.path != "/protocol":
            self._transport_error(HTTPStatus.NOT_FOUND, "http_route_not_found", correlation)
            return
        if not self._valid_peer() or not self._valid_host():
            self._transport_error(HTTPStatus.FORBIDDEN, "loopback_required", correlation)
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self._control._allowed_origins:
            self._transport_error(HTTPStatus.FORBIDDEN, "origin_not_allowed", correlation)
            return
        authorization = self.headers.get_all("Authorization", ())
        try:
            if len(authorization) != 1:
                raise CapabilityHubError(
                    code="invalid_bearer_token",
                    category=ErrorCategory.POLICY,
                    safe_message="The HTTP control credential was rejected.",
                )
            self._control._authenticator.authenticate(authorization[0])
        except CapabilityHubError as error:
            self._transport_error(HTTPStatus.UNAUTHORIZED, error.code, correlation)
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._transport_error(HTTPStatus.BAD_REQUEST, "chunked_body_not_allowed", correlation)
            return
        content_types = self.headers.get_all("Content-Type", ())
        if len(content_types) != 1 or not _is_json_content_type(content_types[0]):
            self._transport_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "json_content_type_required",
                correlation,
            )
            return
        lengths = self.headers.get_all("Content-Length", ())
        try:
            length = int(lengths[0]) if len(lengths) == 1 else -1
        except ValueError:
            length = -1
        if length < 0:
            self._transport_error(
                HTTPStatus.LENGTH_REQUIRED,
                "content_length_required",
                correlation,
            )
            return
        if length > self._control._max_body_bytes:
            self._transport_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_body_too_large",
                correlation,
            )
            return
        try:
            body = self.rfile.read(length)
            raw = json.loads(body, parse_constant=_reject_constant)
            if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
                raise ValueError
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._transport_error(HTTPStatus.BAD_REQUEST, "invalid_json_body", correlation)
            return
        request_id = _safe_identifier(raw.get("request_id"), "unavailable")
        supplied_correlation = raw.get("correlation_id")
        correlation = _safe_identifier(supplied_correlation, correlation)
        try:
            response = self._control._dispatch(raw)
            self._send_envelope(HTTPStatus.OK, response)
        except Exception as error:
            response = error_response(
                request_id=request_id,
                correlation_id=correlation,
                error=error,
            )
            status = (
                HTTPStatus.BAD_REQUEST
                if isinstance(error, CapabilityHubError)
                else HTTPStatus.INTERNAL_SERVER_ERROR
            )
            self._send_envelope(status, response)

    def do_GET(self) -> None:
        self._method_not_allowed()

    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET
    do_HEAD = do_GET

    def _method_not_allowed(self) -> None:
        correlation = f"http-{secrets.token_hex(16)}"
        self._transport_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", correlation)

    def _valid_peer(self) -> bool:
        return bool(self.client_address) and str(self.client_address[0]) in _LOOPBACK_HOSTS

    def _valid_host(self) -> bool:
        values = self.headers.get_all("Host", ())
        if len(values) != 1:
            return False
        try:
            parsed = urlsplit("//" + values[0])
            address = self.server.server_address
            if not isinstance(address, tuple) or len(address) < 2:
                return False
            server_port = int(address[1])
            return parsed.hostname in _LOOPBACK_HOSTS and parsed.port == server_port
        except ValueError:
            return False

    def _transport_error(self, status: HTTPStatus, code: str, correlation: str) -> None:
        error = CapabilityHubError(
            code=code,
            category=ErrorCategory.POLICY if status in {401, 403} else ErrorCategory.INPUT,
            safe_message="The HTTP control request was rejected.",
        )
        response = error_response(
            request_id="unavailable",
            correlation_id=correlation,
            error=error,
        )
        self._send_envelope(status, response)

    def _send_envelope(self, status: HTTPStatus, response: ResponseEnvelope) -> None:
        encoded = json.dumps(
            response.as_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Correlation-ID", response.correlation_id)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def log_message(self, _format: str, *args: object) -> None:
        return


def _validate_origin(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("allowed origins must be loopback HTTP origins")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("allowed origins must be loopback HTTP origins") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or port is None
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("allowed origins must be loopback HTTP origins")
    return value.rstrip("/")


def _is_json_content_type(value: str) -> bool:
    parts = [item.strip().casefold() for item in value.split(";")]
    if not parts or parts[0] != "application/json":
        return False
    return all(item in ("", "charset=utf-8") for item in parts[1:])


def _reject_constant(_value: str) -> None:
    raise ValueError


def _safe_identifier(value: object, fallback: str) -> str:
    if (
        isinstance(value, str)
        and value
        and len(value) <= 256
        and not any(character.isspace() for character in value)
    ):
        return value
    return fallback
