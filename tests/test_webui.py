from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from capabilityhub.webui import DashboardServer


def test_dashboard_rejects_non_loopback_binding() -> None:
    with pytest.raises(ValueError, match="loopback"):
        DashboardServer(dict, host="0.0.0.0")


def test_dashboard_is_local_read_only_and_serves_snapshot() -> None:
    def snapshot() -> dict[str, object]:
        return {
            "reasoning_tier": "low",
            "estimated_savings": "42%",
            "budget_remaining": "800",
            "providers": [{"name": "static", "status": "ready"}],
            "active_capabilities": [],
            "loaded_capabilities": [],
        }

    with DashboardServer(snapshot) as dashboard:
        assert dashboard.url.startswith("http://127.0.0.1:")
        with urlopen(f"{dashboard.url}/api/status", timeout=2) as response:
            assert response.status == 200
            assert b'"reasoning_tier":"low"' in response.read()
        with urlopen(f"{dashboard.url}/", timeout=2) as response:
            assert b"CapabilityHub" in response.read()
        request = Request(f"{dashboard.url}/api/status", method="POST")
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=2)
        assert error.value.code == 405


def test_dashboard_redacts_snapshot_failures() -> None:
    def broken_snapshot() -> dict[str, object]:
        raise RuntimeError("SECRET-CANARY")

    with DashboardServer(broken_snapshot) as dashboard:
        with pytest.raises(HTTPError) as error:
            urlopen(f"{dashboard.url}/api/status", timeout=2)
        assert error.value.code == 503
        assert b"SECRET-CANARY" not in error.value.read()


def test_dashboard_start_and_close_are_idempotent_across_threads() -> None:
    dashboard = DashboardServer(dict)
    with ThreadPoolExecutor(max_workers=4) as pool:
        urls = list(pool.map(lambda _: dashboard.start(), range(8)))

    assert len(set(urls)) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: dashboard.close(), range(8)))
