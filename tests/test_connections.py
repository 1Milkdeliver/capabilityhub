from __future__ import annotations

import json
from threading import Barrier, Event, Lock
from time import monotonic

import pytest

from capabilityhub import runtime
from capabilityhub.cli import main
from capabilityhub.connections import (
    ConnectionProber,
    ProbeTarget,
    configured_mcp_targets,
)
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.models import CapabilityKind


def _target(endpoint: str) -> ProbeTarget:
    return ProbeTarget("opaque-connection", CapabilityKind.MCP, endpoint=endpoint)


def test_https_probe_performs_only_resolved_tcp_tls_setup_and_redacts_endpoint() -> None:
    calls: list[tuple[str, int, float, str | None]] = []

    def dial(endpoint, timeout, tls_hostname):
        calls.append((endpoint.address, endpoint.port, timeout, tls_hostname))

    prober = ConnectionProber(
        timeout_seconds=0.5,
        resolver=lambda _host, _port: ("8.8.8.8",),
        dialer=dial,
        clock=lambda: 1,
    )
    result = prober.probe(_target("https://SECRET.example/private/path"))

    assert calls == [("8.8.8.8", 443, 0.5, "secret.example")]
    assert result.reachable is True
    assert result.authenticated == "unknown"
    assert result.healthy is None
    assert result.transport_security == "tls_verified"
    assert result.reason_code == "probe_tls_verified"
    assert "SECRET" not in repr(result)
    assert "private/path" not in repr(result)


def test_ssrf_policy_denies_private_or_mixed_dns_without_dialing() -> None:
    calls: list[object] = []
    private = ConnectionProber(
        resolver=lambda _host, _port: ("10.0.0.1",),
        dialer=lambda *args: calls.append(args),
    ).probe(_target("https://private.example"))
    rebinding = ConnectionProber(
        resolver=lambda _host, _port: ("8.8.8.8", "169.254.169.254"),
        dialer=lambda *args: calls.append(args),
    ).probe(_target("https://mixed.example"))

    assert private.reason_code == "probe_address_denied"
    assert rebinding.reason_code == "probe_address_denied"
    assert private.attempted is False
    assert rebinding.attempted is False
    assert calls == []


def test_loopback_requires_explicit_policy_and_cleartext_auth_is_unknown() -> None:
    calls: list[object] = []
    denied = ConnectionProber(
        resolver=lambda _host, _port: ("127.0.0.1",),
        dialer=lambda *args: calls.append(args),
    ).probe(_target("http://localhost:8123/mcp"))
    allowed = ConnectionProber(
        allow_loopback=True,
        resolver=lambda _host, _port: ("127.0.0.1",),
        dialer=lambda *args: calls.append(args),
    ).probe(_target("http://localhost:8123/mcp"))

    assert denied.reason_code == "probe_address_denied"
    assert allowed.reachable is True
    assert allowed.authenticated == "unknown"
    assert allowed.healthy is None
    assert allowed.transport_security == "cleartext"
    assert len(calls) == 1


def test_invalid_credentials_timeout_and_stdio_are_stable_and_redacted(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[mcp_servers.stdio_secret]
command = "C:/SECRET/private/tool.exe"

[mcp_servers.url_secret]
url = "https://user:SECRET@example.test/mcp"
""",
        encoding="utf-8",
    )
    targets = configured_mcp_targets(config)
    assert len(targets) == 2
    assert "SECRET" not in repr(targets)

    prober = ConnectionProber(
        resolver=lambda _host, _port: ("8.8.8.8",),
        dialer=lambda *_args: (_ for _ in ()).throw(TimeoutError("SECRET timeout")),
    )
    results = prober.probe_all(targets)
    assert {item.reason_code for item in results} == {
        "probe_stdio_unsupported",
        "probe_endpoint_invalid",
    }

    timed_out = prober.probe(_target("https://example.test"))
    assert timed_out.reason_code == "probe_timeout"
    assert timed_out.attempted is True
    assert "SECRET" not in repr(timed_out)


def test_dns_resolution_is_bounded_by_the_probe_deadline() -> None:
    release = Event()
    calls: list[object] = []

    def blocked_resolver(_hostname, _port):
        release.wait(timeout=2)
        return ("8.8.8.8",)

    prober = ConnectionProber(
        timeout_seconds=0.05,
        resolver=blocked_resolver,
        dialer=lambda *args: calls.append(args),
    )
    started = monotonic()
    result = prober.probe(_target("https://slow.example"))
    elapsed = monotonic() - started
    release.set()

    assert result.reason_code == "probe_timeout"
    assert result.attempted is True
    assert elapsed < 0.5
    assert calls == []


def test_probe_concurrency_is_strictly_bounded() -> None:
    barrier = Barrier(2)
    lock = Lock()
    active = 0
    maximum = 0

    def dial(_endpoint, _timeout, _tls_hostname):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        barrier.wait(timeout=2)
        with lock:
            active -= 1

    prober = ConnectionProber(
        max_concurrency=2,
        resolver=lambda _host, _port: ("8.8.8.8",),
        dialer=dial,
    )
    results = prober.probe_all((_target("https://one.example"), _target("https://two.example")))

    assert maximum == 2
    assert all(item.reachable is True for item in results)
    assert all(item.healthy is None for item in results)
    with pytest.raises(ValueError, match="between 1 and 16"):
        ConnectionProber(max_concurrency=17)


def test_runtime_default_is_zero_probe_and_explicit_probe_is_sanitized(tmp_path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    project.mkdir()
    config.write_text(
        '[mcp_servers.private]\nurl = "https://SECRET.example/private/path"\n',
        encoding="utf-8",
    )
    monitor = LocalCatalogMonitor(home=home, project=project, refresh_interval_seconds=0)

    default = runtime.local_connections(monitor=monitor)
    assert default["network_probes_performed"] == 0
    assert default["scope"] == "configuration_only"
    assert "probe_results" not in default

    prober = ConnectionProber(
        resolver=lambda _host, _port: ("8.8.8.8",),
        dialer=lambda _endpoint, _timeout, _tls_hostname: None,
    )
    probed = runtime.local_connections(
        monitor=monitor,
        probe=True,
        connection_prober=prober,
    )
    assert probed["network_probes_performed"] == 1
    assert probed["scope"] == "configuration_and_safe_probe"
    rows = {item["kind"]: item for item in probed["connections"]}
    assert rows["mcp"]["reachable"] is True
    assert rows["mcp"]["authenticated"] == "unknown"
    assert rows["mcp"]["healthy"] is None
    assert rows["mcp"]["transport_security"] == "tls_verified"
    serialized = json.dumps(probed)
    assert "SECRET.example" not in serialized
    assert "private/path" not in serialized


def test_cli_probe_flag_is_explicit_and_passes_only_bounded_options(monkeypatch, capsys) -> None:
    seen: list[dict[str, object]] = []

    def connections(_project_root, **options):
        seen.append(options)
        return {"network_probes_performed": int(options["probe"])}

    monkeypatch.setattr(runtime, "local_connections", connections)

    assert main(["connections"]) == 0
    assert json.loads(capsys.readouterr().out)["network_probes_performed"] == 0
    assert seen[-1]["probe"] is False

    assert (
        main(
            [
                "connections",
                "--probe",
                "--probe-timeout-ms",
                "250",
                "--probe-concurrency",
                "2",
                "--allow-loopback-probe",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["network_probes_performed"] == 1
    assert seen[-1] == {
        "allow_loopback": True,
        "probe": True,
        "probe_concurrency": 2,
        "probe_timeout_ms": 250,
    }
