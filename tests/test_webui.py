from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
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
            assert b"CapSift" in response.read()
            assert response.headers["Cache-Control"] == "no-store"
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


def test_dashboard_assets_have_management_controls_and_no_mojibake() -> None:
    root = Path(__file__).parents[1] / "src" / "capabilityhub" / "web"
    assets = "\n".join(
        (root / name).read_text(encoding="utf-8") for name in ("index.html", "app.js", "style.css")
    )

    assert "search-form" in assets
    assert "/api/lifecycle" in assets
    assert "/api/language" in assets
    assert "provider-list" in assets
    assert "loaded-list" in assets
    assert "/api/approval" in assets
    assert "/api/context" in assets
    assert "/api/capabilities" in assets
    assert "/api/conversation" in assets
    assert 'data-page-view="conversations"' in assets
    assert 'data-page-view="guide"' in assets
    assert 'role", "switch"' in assets
    assert "capability-dialog" in assets
    assert "category-filter" in assets
    assert "capability-sort" in assets
    assert "refresh-conversations" in assets
    assert '"zh-CN"' in assets
    assert "data-i18n" in assets
    assert "reasoning-tier" in assets
    assert "update-list" in assets
    assert "/api/app-update" in assets
    assert "app-update-status" in assets
    assert "secure-audit-status" in assets
    assert "\u0431" not in assets
    assert "\ufffd" not in assets


def test_dashboard_search_and_csrf_protected_management_callbacks() -> None:
    lifecycle_calls: list[tuple[str, str]] = []
    language_calls: list[str] = []
    approval_calls: list[tuple[str, str]] = []
    context_calls: list[tuple[str, str]] = []
    app_update_calls: list[tuple[str, bool]] = []

    with DashboardServer(
        lambda: {"inventory": {"active_total": 1}},
        search_provider=lambda query, kind, limit: {
            "query": query,
            "kind": kind,
            "limit": limit,
            "results": [],
        },
        lifecycle_provider=lambda coordinate, state: (
            lifecycle_calls.append((coordinate, state)) or {"saved": True}
        ),
        language_provider=lambda locale: language_calls.append(locale) or {"saved": True},
        approval_provider=lambda approval_id, decision: (
            approval_calls.append((approval_id, decision)) or {"saved": True}
        ),
        context_provider=lambda action, key: context_calls.append((action, key)) or {"saved": True},
        capability_list_provider=lambda query, kind, offset, limit: {
            "entries": [], "limit": limit, "next_offset": None, "offset": offset, "total": 0,
        },
        conversation_provider=lambda task_id: {
            "capabilities": [], "status": "observed", "task_id": task_id, "total": 0,
        },
        app_update_provider=lambda action, force: (
            app_update_calls.append((action, force)) or {"status": "downloaded"}
        ),
    ) as dashboard:
        with urlopen(f"{dashboard.url}/api/status", timeout=2) as response:
            status = json.loads(response.read())
        token = status["dashboard"]["csrf_token"]
        query = urlencode({"q": "pdf", "kind": "skill", "limit": 3})
        with urlopen(f"{dashboard.url}/api/search?{query}", timeout=2) as response:
            search = json.loads(response.read())
        assert search == {"kind": "skill", "limit": 3, "query": "pdf", "results": []}
        capabilities_query = urlencode({"q": "pdf", "kind": "skill", "offset": 0, "limit": 12})
        capabilities_url = f"{dashboard.url}/api/capabilities?{capabilities_query}"
        with urlopen(capabilities_url, timeout=2) as response:
            capabilities = json.loads(response.read())
        assert capabilities["total"] == 0
        with urlopen(f"{dashboard.url}/api/conversation?id=task-one", timeout=2) as response:
            conversation = json.loads(response.read())
        assert conversation["task_id"] == "task-one"

        denied = Request(
            f"{dashboard.url}/api/language",
            data=b'{"locale":"zh-CN"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(denied, timeout=2)
        assert error.value.code == 403

        lifecycle = Request(
            f"{dashboard.url}/api/lifecycle",
            data=b'{"coordinate":"demo/tool","state":"disabled"}',
            headers={
                "Content-Type": "application/json",
                "X-CapSift-CSRF": token,
            },
            method="POST",
        )
        with urlopen(lifecycle, timeout=2) as response:
            assert json.loads(response.read()) == {"saved": True}
        language = Request(
            f"{dashboard.url}/api/language",
            data=b'{"locale":"zh-CN"}',
            headers={
                "Content-Type": "application/json",
                "X-CapSift-CSRF": token,
            },
            method="POST",
        )
        with urlopen(language, timeout=2) as response:
            assert json.loads(response.read()) == {"saved": True}
        approval = Request(
            f"{dashboard.url}/api/approval",
            data=b'{"approval_id":"apr_one","decision":"approve"}',
            headers={"Content-Type": "application/json", "X-CapSift-CSRF": token},
            method="POST",
        )
        with urlopen(approval, timeout=2) as response:
            assert json.loads(response.read()) == {"saved": True}
        context = Request(
            f"{dashboard.url}/api/context",
            data=b'{"key":"demo::contract","action":"pin"}',
            headers={"Content-Type": "application/json", "X-CapSift-CSRF": token},
            method="POST",
        )
        with urlopen(context, timeout=2) as response:
            assert json.loads(response.read()) == {"saved": True}
        app_update = Request(
            f"{dashboard.url}/api/app-update",
            data=b'{"action":"fetch"}',
            headers={"Content-Type": "application/json", "X-CapSift-CSRF": token},
            method="POST",
        )
        with urlopen(app_update, timeout=2) as response:
            assert json.loads(response.read()) == {"status": "downloaded"}

    assert lifecycle_calls == [("demo/tool", "disabled")]
    assert language_calls == ["zh-CN"]
    assert approval_calls == [("apr_one", "approve")]
    assert context_calls == [("pin", "demo::contract")]
    assert app_update_calls == [("fetch", True)]
