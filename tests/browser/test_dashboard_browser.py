from __future__ import annotations

import os
from pathlib import Path

import pytest

from capabilityhub.webui import DashboardServer

playwright = pytest.importorskip("playwright.sync_api")


def _browser_executable() -> str | None:
    configured = os.environ.get("CAPABILITYHUB_CHROME_PATH")
    candidates = (
        configured,
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    )
    return next((item for item in candidates if item and Path(item).is_file()), None)


def _snapshot() -> dict[str, object]:
    return {
        "approvals": {
            "approvals": [
                {
                    "approval_id": "apr_browser",
                    "expires_at": "soon",
                    "operation": "write",
                    "revision": "demo/tool@1",
                    "status": "pending",
                }
            ]
        },
        "audit": {"events": []},
        "connections": {"connections": []},
        "context": {
            "entries": [
                {
                    "key": "demo::contract",
                    "pinned": False,
                    "portable_tokens": 4,
                    "section": "contract",
                }
            ]
        },
        "health": {"checks": [{"check": "dashboard", "status": "ready"}]},
        "inventory": {
            "active_by_kind": {"api": 0, "cli": 0, "mcp": 1, "rag": 0, "skill": 1},
            "active_total": 2,
            "excluded_by_reason": {},
            "generation": 7,
            "inactive_count": 0,
            "status": "fresh",
        },
        "lifecycle": {
            "entries": [{"active": True, "coordinate": "demo/tool", "state": "enabled"}]
        },
        "loaded_capabilities": [],
        "model_calls": 0,
        "preferences": {"locale": "en"},
        "providers": {"entries": [{"active": 2, "discovered": 2, "provider": "local"}]},
        "reasoning": {
            "budget": {"remaining": 100},
            "current_tier": "low",
            "escalations_used": 0,
        },
        "secure_audit": {"configured": False, "key_environment": "not exposed"},
        "token_usage": 0,
        "updates": {"states": []},
    }


def test_real_dashboard_actions_responsive_accessible_and_no_model_spend(tmp_path: Path) -> None:
    executable = _browser_executable()
    if executable is None:
        if os.environ.get("CAPABILITYHUB_BROWSER_REQUIRED") == "1":
            pytest.fail("A system Chrome executable is required for the browser gate")
        pytest.skip("System Chrome is unavailable")

    calls: list[tuple[str, ...]] = []
    usage = {"model_calls": 0, "token_usage": 0}

    def snapshot() -> dict[str, object]:
        return {**_snapshot(), **usage}

    with DashboardServer(
        snapshot,
        search_provider=lambda query, kind, limit: {
            "results": [
                {
                    "kind": kind or "skill",
                    "match_reason": ["name"],
                    "revision": "demo/tool@1",
                    "summary": f"Result for {query} ({limit})",
                }
            ]
        },
        lifecycle_provider=lambda coordinate, state: (
            calls.append(("lifecycle", coordinate, state)) or {"saved": True}
        ),
        language_provider=lambda locale: calls.append(("language", locale)) or {"saved": True},
        approval_provider=lambda approval_id, decision: (
            calls.append(("approval", approval_id, decision)) or {"saved": True}
        ),
        context_provider=lambda action, key: (
            calls.append(("context", action, key)) or {"saved": True}
        ),
    ) as dashboard:
        dashboard_url = dashboard.url
        console_findings: list[str] = []
        page_errors: list[str] = []
        network: list[tuple[str, int]] = []
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                executable_path=executable,
                headless=True,
                args=["--disable-extensions", "--no-first-run"],
            )
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.on(
                "console",
                lambda message: console_findings.append(f"{message.type}: {message.text}")
                if message.type in {"warning", "error"}
                else None,
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "response",
                lambda response: network.append((response.url, response.status))
                if response.url.startswith(dashboard_url)
                else None,
            )

            page.goto(dashboard_url, wait_until="networkidle")
            page.get_by_text("Live snapshot updated.").wait_for()
            assert page.get_by_role("heading", name="CapabilityHub", level=1).is_visible()
            assert page.get_by_role("heading", name="Search", level=2).is_visible()
            assert page.get_by_label("Task or name").is_visible()
            assert page.get_by_label("Capability kind").is_visible()

            before = dict(usage)
            page.get_by_label("Task or name").fill("demo")
            page.get_by_label("Capability kind").select_option("skill")
            page.get_by_role("button", name="Search", exact=True).click()
            page.get_by_text("demo/tool (skill)").wait_for()
            with page.expect_response(lambda response: response.url.endswith("/api/status")):
                page.locator("#search-list").get_by_role("button", name="Disable").click()
            page.get_by_label("Language").select_option("zh-CN")
            with page.expect_response(lambda response: response.url.endswith("/api/language")):
                page.get_by_role("button", name="Save language").click()
            with page.expect_response(lambda response: response.url.endswith("/api/status")):
                page.locator("#approval-list").get_by_role("button", name="Approve").click()
            with page.expect_response(lambda response: response.url.endswith("/api/status")):
                page.locator("#context-list").get_by_role("button", name="Pin").click()

            page.get_by_role("link", name="Search", exact=True).click()
            assert page.url.endswith("#search-title")
            page.get_by_role("button", name="Back").click()
            page.get_by_role("link", name="Home").click()
            assert page.url.endswith("#top")

            page.keyboard.press("Tab")
            assert page.evaluate("() => document.activeElement?.matches('a,button,input,select')")
            artifact_dir = Path(
                os.environ.get("CAPABILITYHUB_BROWSER_ARTIFACTS", ".artifacts/browser")
            )
            artifact_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=artifact_dir / "dashboard-desktop.png", full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator("main").evaluate("node => node.scrollWidth <= innerWidth")
            page.screenshot(path=artifact_dir / "dashboard-narrow.png", full_page=True)
            after = dict(usage)
            context.close()
            browser.close()

    assert before == after == {"model_calls": 0, "token_usage": 0}
    assert ("lifecycle", "demo/tool", "disabled") in calls
    assert ("language", "zh-CN") in calls
    assert ("approval", "apr_browser", "approve") in calls
    assert ("context", "pin", "demo::contract") in calls
    assert console_findings == []
    assert page_errors == []
    assert network
    assert all(status < 400 for _, status in network)
    assert all(url.startswith(dashboard_url) for url, _ in network)
