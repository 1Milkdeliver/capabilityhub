"""Explicit, metadata-only connection probes that never invoke capabilities."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import socket
import ssl
import tomllib
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic
from urllib.parse import urlsplit

from capabilityhub.models import CapabilityKind

_MAX_CONFIG_BYTES = 1_048_576
_MAX_TARGETS = 64


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    connection_id: str
    kind: CapabilityKind
    endpoint: str | None = field(default=None, repr=False)
    configured: bool = True
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    address: str
    port: int


@dataclass(frozen=True, slots=True)
class ProbeResult:
    connection_id: str
    kind: CapabilityKind
    configured: bool
    reachable: bool | None
    authenticated: str
    healthy: bool | None
    transport_security: str
    latency_bucket: str
    reason_code: str
    attempted: bool = False


Resolver = Callable[[str, int], tuple[str, ...]]
Dialer = Callable[[ResolvedEndpoint, float, str | None], None]


class ConnectionProber:
    """Resolve, policy-check, then perform only TCP/TLS setup with strict bounds."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 1,
        max_concurrency: int = 4,
        allow_loopback: bool = False,
        resolver: Resolver | None = None,
        dialer: Dialer | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not math.isfinite(timeout_seconds) or not 0.05 <= timeout_seconds <= 5:
            raise ValueError("timeout_seconds must be between 0.05 and 5")
        if isinstance(max_concurrency, bool) or not 1 <= max_concurrency <= 16:
            raise ValueError("max_concurrency must be between 1 and 16")
        self._timeout_seconds = timeout_seconds
        self._max_concurrency = max_concurrency
        self._allow_loopback = allow_loopback
        self._resolver = resolver or _resolve
        self._dialer = dialer or _dial
        self._clock = clock

    def probe_all(self, targets: Iterable[ProbeTarget]) -> tuple[ProbeResult, ...]:
        selected = tuple(targets)
        if len(selected) > _MAX_TARGETS:
            raise ValueError(f"at most {_MAX_TARGETS} probe targets are allowed")
        with ThreadPoolExecutor(max_workers=self._max_concurrency) as pool:
            return tuple(pool.map(self.probe, selected))

    def probe(self, target: ProbeTarget) -> ProbeResult:
        if not target.configured:
            return _result(target, reason="probe_disabled")
        if target.reason_code is not None or target.endpoint is None:
            return _result(target, reason=target.reason_code or "probe_unsupported")
        parsed = urlsplit(target.endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return _result(target, reason="probe_endpoint_invalid")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return _result(target, reason="probe_endpoint_invalid")
        deadline = monotonic() + self._timeout_seconds
        resolution = _resolve_bounded(
            self._resolver,
            parsed.hostname,
            port,
            timeout=self._timeout_seconds,
        )
        if resolution is None:
            return _result(target, reason="probe_timeout", attempted=True)
        resolved, addresses = resolution
        if not resolved:
            return _result(target, reason="probe_dns_failed")
        if not addresses:
            return _result(target, reason="probe_dns_failed")
        try:
            parsed_addresses = tuple(ipaddress.ip_address(value) for value in addresses)
        except ValueError:
            return _result(target, reason="probe_dns_failed")
        if any(not self._address_allowed(address) for address in parsed_addresses):
            return _result(target, reason="probe_address_denied")
        started = self._clock()
        tls_hostname = parsed.hostname if parsed.scheme == "https" else None
        remaining = deadline - monotonic()
        if remaining <= 0:
            return _result(target, reason="probe_timeout", attempted=True)
        try:
            self._dialer(
                ResolvedEndpoint(str(parsed_addresses[0]), port),
                remaining,
                tls_hostname,
            )
        except TimeoutError:
            return _result(target, reason="probe_timeout", attempted=True)
        except ssl.SSLError:
            return _result(target, reason="probe_tls_failed", attempted=True)
        except (OSError, ConnectionError):
            return _result(target, reason="probe_unreachable", attempted=True)
        except Exception:
            return _result(target, reason="probe_failed", attempted=True)
        latency = max(0.0, (self._clock() - started) * 1_000)
        return ProbeResult(
            connection_id=target.connection_id,
            kind=target.kind,
            configured=True,
            reachable=True,
            authenticated="unknown",
            healthy=None,
            transport_security=("tls_verified" if tls_hostname is not None else "cleartext"),
            latency_bucket=_latency_bucket(latency),
            reason_code=("probe_tls_verified" if tls_hostname is not None else "probe_reachable"),
            attempted=True,
        )

    def _address_allowed(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if address.is_loopback:
            return self._allow_loopback
        return not (
            address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )


def configured_mcp_targets(config_path: str | Path) -> tuple[ProbeTarget, ...]:
    """Read bounded Codex MCP configuration without retaining command paths or credentials."""

    path = Path(config_path)
    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            return (
                ProbeTarget(
                    "mcp-config",
                    CapabilityKind.MCP,
                    reason_code="probe_config_too_large",
                ),
            )
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return ()
    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict):
        return ()
    targets: list[ProbeTarget] = []
    items = sorted(servers.items(), key=lambda item: str(item[0]))
    truncated = len(items) > _MAX_TARGETS
    selected_items = items[: _MAX_TARGETS - 1] if truncated else items
    for name, config in selected_items:
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        endpoint = config.get("url")
        identifier = hashlib.sha256(f"mcp\0{name}".encode()).hexdigest()[:16]
        if config.get("enabled") is False:
            targets.append(ProbeTarget(identifier, CapabilityKind.MCP, configured=False))
        elif isinstance(endpoint, str):
            targets.append(ProbeTarget(identifier, CapabilityKind.MCP, endpoint=endpoint))
        else:
            targets.append(
                ProbeTarget(
                    identifier,
                    CapabilityKind.MCP,
                    reason_code="probe_stdio_unsupported",
                )
            )
    if truncated:
        targets.append(
            ProbeTarget(
                "mcp-target-limit",
                CapabilityKind.MCP,
                reason_code="probe_target_limit",
            )
        )
    return tuple(targets)


def _resolve(hostname: str, port: int) -> tuple[str, ...]:
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        if isinstance(item[4][0], str)
    }
    return tuple(sorted(addresses))


def _resolve_bounded(
    resolver: Resolver, hostname: str, port: int, *, timeout: float
) -> tuple[bool, tuple[str, ...]] | None:
    """Bound a blocking system resolver without retaining its raw failure details."""

    result: Queue[tuple[bool, tuple[str, ...]]] = Queue(maxsize=1)

    def run() -> None:
        try:
            result.put_nowait((True, resolver(hostname, port)))
        except Exception:
            result.put_nowait((False, ()))

    Thread(target=run, name="capabilityhub-dns-probe", daemon=True).start()
    try:
        return result.get(timeout=timeout)
    except Empty:
        return None


def _dial(endpoint: ResolvedEndpoint, timeout: float, tls_hostname: str | None) -> None:
    connection = socket.create_connection((endpoint.address, endpoint.port), timeout=timeout)
    try:
        if tls_hostname is None:
            return
        context = ssl.create_default_context()
        secured = context.wrap_socket(connection, server_hostname=tls_hostname)
        secured.close()
    finally:
        connection.close()


def _result(target: ProbeTarget, *, reason: str, attempted: bool = False) -> ProbeResult:
    return ProbeResult(
        connection_id=target.connection_id,
        kind=target.kind,
        configured=target.configured,
        reachable=False if attempted else None,
        authenticated="unknown",
        healthy=None,
        transport_security="unknown",
        latency_bucket="unknown",
        reason_code=reason,
        attempted=attempted,
    )


def _latency_bucket(latency_ms: float) -> str:
    if latency_ms < 50:
        return "lt_50ms"
    if latency_ms < 200:
        return "50_199ms"
    if latency_ms < 1_000:
        return "200_999ms"
    return "gte_1000ms"
