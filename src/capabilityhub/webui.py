"""Read-only local dashboard for an injected CapabilityHub status snapshot."""

from __future__ import annotations

import json
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

StatusSnapshot = Mapping[str, object]


class DashboardServer:
    """Serve static dashboard assets and an injected, read-only status endpoint."""

    def __init__(
        self,
        snapshot_provider: Callable[[], StatusSnapshot],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if host != "localhost" and not ip_address(host).is_loopback:
            raise ValueError("dashboard host must be a loopback address")
        self._snapshot_provider = snapshot_provider
        self._host = host
        self._port = port
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
        directory: str | None = None,
    ) -> None:
        self._snapshot_provider = snapshot_provider
        super().__init__(request, client_address, server, directory=directory)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/api/status":
            self._write_status()
            return
        super().do_GET()

    def do_POST(self) -> None:
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Dashboard is read-only")

    def _write_status(self) -> None:
        try:
            payload = json.dumps(
                self._snapshot_provider(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError):
            self.send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Status snapshot is not JSON serializable"
            )
            return
        except Exception:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Status snapshot unavailable")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        """Avoid recording local dashboard requests by default."""


def _assets_directory() -> Path:
    return Path(__file__).with_name("web")


WebUI = DashboardServer
