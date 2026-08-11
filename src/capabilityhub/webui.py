"""Loopback dashboard with bounded local search and explicit control callbacks."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from socket import socket
from socketserver import BaseServer
from threading import RLock, Thread
from typing import Any
from urllib.parse import parse_qs, urlsplit

StatusSnapshot = Mapping[str, object]
SearchProvider = Callable[[str, str | None, int], StatusSnapshot]
LifecycleProvider = Callable[[str, str], StatusSnapshot]
LanguageProvider = Callable[[str], StatusSnapshot]


class DashboardServer:
    """Serve local assets, status/search, and optional explicit management callbacks."""

    def __init__(
        self,
        snapshot_provider: Callable[[], StatusSnapshot],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        search_provider: SearchProvider | None = None,
        lifecycle_provider: LifecycleProvider | None = None,
        language_provider: LanguageProvider | None = None,
    ) -> None:
        if host != "localhost" and not ip_address(host).is_loopback:
            raise ValueError("dashboard host must be a loopback address")
        self._snapshot_provider = snapshot_provider
        self._host = host
        self._port = port
        self._search_provider = search_provider
        self._lifecycle_provider = lifecycle_provider
        self._language_provider = language_provider
        self._csrf_token = secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        self._lifecycle_lock = RLock()

    @property
    def url(self) -> str:
        with self._lifecycle_lock:
            server = self._server
        if server is None:
            raise RuntimeError("dashboard server has not been started")
        host, port = server.server_address[:2]
        if isinstance(host, bytes):
            host = host.decode("ascii")
        return f"http://{host}:{port}"

    def start(self) -> str:
        with self._lifecycle_lock:
            if self._server is not None:
                return self.url
            handler = partial(
                _DashboardHandler,
                directory=str(_assets_directory()),
                snapshot_provider=self._snapshot_provider,
                search_provider=self._search_provider,
                lifecycle_provider=self._lifecycle_provider,
                language_provider=self._language_provider,
                csrf_token=self._csrf_token,
            )
            server = ThreadingHTTPServer((self._host, self._port), handler)
            thread = Thread(
                target=server.serve_forever,
                name="capabilityhub-dashboard",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()
            return self.url

    def close(self) -> None:
        with self._lifecycle_lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def __enter__(self) -> DashboardServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(
        self,
        request: socket | tuple[bytes, socket],
        client_address: Any,
        server: BaseServer,
        *,
        snapshot_provider: Callable[[], StatusSnapshot],
        search_provider: SearchProvider | None,
        lifecycle_provider: LifecycleProvider | None,
        language_provider: LanguageProvider | None,
        csrf_token: str,
        directory: str | None = None,
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._search_provider = search_provider
        self._lifecycle_provider = lifecycle_provider
        self._language_provider = language_provider
        self._csrf_token = csrf_token
        super().__init__(request, client_address, server, directory=directory)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/api/status":
            self._write_status()
            return
        if parsed.path == "/api/search":
            self._write_search(parse_qs(parsed.query))
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/lifecycle", "/api/language"}:
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Dashboard action is not available")
            return
        if not self._authorized():
            self._drain_request_body()
            self.send_error(HTTPStatus.FORBIDDEN, "Dashboard request was not authorized")
            return
        body = self._json_body()
        if body is None:
            return
        if path == "/api/lifecycle":
            lifecycle_provider = self._lifecycle_provider
            coordinate = body.get("coordinate")
            state = body.get("state")
            if (
                lifecycle_provider is None
                or not isinstance(coordinate, str)
                or not isinstance(state, str)
                or state not in {"enabled", "disabled", "quarantined"}
            ):
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid lifecycle request")
                return
            self._write_callback(lambda: lifecycle_provider(coordinate, state))
            return
        language_provider = self._language_provider
        locale = body.get("locale")
        if (
            language_provider is None
            or not isinstance(locale, str)
            or locale not in {"auto", "en", "zh-CN"}
        ):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid language request")
            return
        self._write_callback(lambda: language_provider(locale))

    def _write_status(self) -> None:
        try:
            snapshot = dict(self._snapshot_provider())
            snapshot["dashboard"] = {
                "csrf_token": self._csrf_token,
                "language_mutation": self._language_provider is not None,
                "lifecycle_mutation": self._lifecycle_provider is not None,
                "search": self._search_provider is not None,
            }
            payload = _json_bytes(snapshot)
        except (TypeError, ValueError):
            self.send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Status snapshot is not JSON serializable"
            )
            return
        except Exception:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Status snapshot unavailable")
            return
        self._write_json(payload)

    def _write_search(self, query: Mapping[str, list[str]]) -> None:
        search_provider = self._search_provider
        if search_provider is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Dashboard search is not configured")
            return
        text = query.get("q", [""])[0]
        kind = query.get("kind", [None])[0]
        raw_limit = query.get("limit", ["5"])[0]
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 0
        if len(text) > 500 or kind not in {None, "skill", "mcp", "cli", "api", "rag"}:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid search request")
            return
        if not 1 <= limit <= 10:
            self.send_error(HTTPStatus.BAD_REQUEST, "Search limit must be from 1 to 10")
            return
        self._write_callback(lambda: search_provider(text, kind, limit))

    def _write_callback(self, callback: Callable[[], StatusSnapshot]) -> None:
        try:
            payload = _json_bytes(callback())
        except (TypeError, ValueError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Dashboard action was rejected")
            return
        except Exception:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Dashboard action failed")
            return
        self._write_json(payload)

    def _authorized(self) -> bool:
        token = self.headers.get("X-CapabilityHub-CSRF", "")
        if not secrets.compare_digest(token, self._csrf_token):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin == f"http://{self.headers.get('Host')}"

    def _json_body(self) -> dict[str, object] | None:
        if self.headers.get_content_type() != "application/json":
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON body required")
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 1 <= length <= 16_384:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Invalid request size")
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
            return None
        if not isinstance(payload, dict):
            self.send_error(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
            return None
        return payload

    def _drain_request_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        if 0 < length <= 16_384:
            self.rfile.read(length)

    def _write_json(self, payload: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        """Avoid recording local dashboard requests by default."""


def _assets_directory() -> Path:
    return Path(__file__).with_name("web")


WebUI = DashboardServer


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
