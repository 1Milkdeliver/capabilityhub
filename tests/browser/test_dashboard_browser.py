from __future__ import annotations

import os
from pathlib import Path
from threading import Event

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
        "conversations": {
            "entries": [
                {
                    "id": "019ff540-d2c0-72d3-8fde-c00c42ae6f58",
                    "title": "Demo task",
                    "updated_at": "2026-08-12T09:15:05Z",
                }
            ]
        },
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
        "lifecycle": {"entries": [{"active": True, "coordinate": "demo/tool", "state": "enabled"}]},
        "loaded_capabilities": [],
        "model_calls": 0,
        "preferences": {"locale": "en"},
        "providers": {"entries": [{"active": 2, "discovered": 2, "provider": "local"}]},
        "reasoning": {
            "budget": {"remaining": 100},
            "current_tier": "**未选择**",
            "escalations_used": 0,
        },
        "secure_audit": {"configured": False, "key_environment": "not exposed"},
        "token_usage": 0,
        "updates": {"states": []},
    }


def test_stale_status_response_cannot_override_language_selection() -> None:
    executable = _browser_executable()
    if executable is None:
        pytest.skip("System Chrome is unavailable")

    locale = {"value": "zh-CN"}
    hold_next = Event()
    stale_started = Event()
    release_stale = Event()

    def snapshot() -> dict[str, object]:
        captured_locale = locale["value"]
        if hold_next.is_set():
            hold_next.clear()
            stale_started.set()
            release_stale.wait(timeout=5)
        return {
            **_snapshot(),
            "context": {"entries": []},
            "preferences": {"locale": captured_locale},
            "reasoning": {"current_tier": "未选择"},
            "updates": {"states": []},
        }

    def save_language(requested: str) -> dict[str, bool]:
        locale["value"] = requested
        return {"saved": True}

    with (
        DashboardServer(snapshot, language_provider=save_language) as dashboard,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--disable-extensions", "--no-first-run"],
        )
        page = browser.new_page()
        page.goto(dashboard.url, wait_until="networkidle")
        page.get_by_role("heading", name="对话", level=2).wait_for()

        hold_next.set()
        page.get_by_role("button", name="刷新对话").click()
        assert stale_started.wait(timeout=5)
        with page.expect_response(lambda response: response.url.endswith("/api/language")):
            page.get_by_label("语言").select_option("en")
        release_stale.set()

        page.get_by_text("Conversation list refreshed.").wait_for()
        assert page.get_by_label("Language").input_value() == "en"
        assert page.locator("html").get_attribute("lang") == "en"
        page.get_by_role("heading", name="Conversations", level=2).wait_for()
        browser.close()


def test_real_dashboard_actions_responsive_accessible_and_no_model_spend(tmp_path: Path) -> None:
    executable = _browser_executable()
    if executable is None:
        if os.environ.get("CAPABILITYHUB_BROWSER_REQUIRED") == "1":
            pytest.fail("A system Chrome executable is required for the browser gate")
        pytest.skip("System Chrome is unavailable")

    calls: list[tuple[str, ...]] = []
    usage = {"model_calls": 0, "token_usage": 0}
    preference = {"locale": "en"}
    capability_state = {"active": True}
    context_entries: list[dict[str, object]] = [
        {
            "key": "demo::contract",
            "pinned": False,
            "portable_tokens": 4,
            "section": "contract",
        }
    ]

    def snapshot() -> dict[str, object]:
        return {
            **_snapshot(),
            **usage,
            "context": {"entries": list(context_entries)},
            "preferences": dict(preference),
        }

    def save_language(locale: str) -> dict[str, bool]:
        preference["locale"] = locale
        calls.append(("language", locale))
        return {"saved": True}

    def save_lifecycle(coordinate: str, state: str) -> dict[str, bool]:
        capability_state["active"] = state == "enabled"
        calls.append(("lifecycle", coordinate, state))
        return {"saved": True}

    def save_context(action: str, key: str) -> dict[str, bool]:
        calls.append(("context", action, key))
        context_entries.clear()
        return {"saved": True}

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
        lifecycle_provider=save_lifecycle,
        language_provider=save_language,
        approval_provider=lambda approval_id, decision: (
            calls.append(("approval", approval_id, decision)) or {"saved": True}
        ),
        context_provider=save_context,
        capability_list_provider=lambda query, kind, offset, limit: {
            "entries": [
                {
                    "active": capability_state["active"],
                    "category": "development",
                    "coordinate": "demo/tool",
                    "estimated_load_tokens": 42,
                    "kind": kind or "skill",
                    "operations": ["load"],
                    "provider": "local",
                    "revision": "demo/tool@1",
                    "state": "enabled",
                    "summary": f"Result for {query or 'catalog'}",
                }
            ],
            "limit": limit,
            "next_offset": None,
            "offset": offset,
            "total": 1,
        },
        conversation_provider=lambda task_id: {
            "capabilities": [
                {
                    "kind": "skill",
                    "name": "demo",
                    "source": "observed_skill_instruction_read",
                }
            ],
            "status": "observed",
            "task_id": task_id,
            "total": 1,
        },
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
                lambda message: (
                    console_findings.append(f"{message.type}: {message.text}")
                    if message.type in {"warning", "error"}
                    else None
                ),
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "response",
                lambda response: (
                    network.append((response.url, response.status))
                    if response.url.startswith(dashboard_url)
                    else None
                ),
            )
            artifact_dir = Path(
                os.environ.get("CAPABILITYHUB_BROWSER_ARTIFACTS", ".artifacts/browser")
            )
            artifact_dir.mkdir(parents=True, exist_ok=True)

            page.goto(dashboard_url, wait_until="networkidle")
            page.get_by_text("Live snapshot updated.").wait_for()
            assert page.get_by_role("heading", name="CapSift", level=1).is_visible()
            assert page.get_by_role("heading", name="Conversations", level=2).is_visible()
            page.get_by_role("link", name="Capabilities", exact=True).click()
            page.get_by_role("heading", name="Capabilities", level=2).wait_for()
            assert page.get_by_label("Search a specific name").is_visible()
            assert page.get_by_label("Capability kind").is_visible()

            before = dict(usage)
            page.get_by_label("Search a specific name").fill("dmo")
            page.get_by_label("Capability kind").select_option("skill")
            page.get_by_role("button", name="Search", exact=True).click()
            page.locator("#search-list strong").get_by_text("demo/tool", exact=True).wait_for()
            page.locator("#search-list").get_by_role("button", name="View details").click()
            assert page.get_by_role("dialog").is_visible()
            page.get_by_role("dialog").get_by_role("button", name="Close").click()
            with page.expect_response(lambda response: response.url.endswith("/api/lifecycle")):
                page.locator("#search-list").get_by_role(
                    "switch", name="Block future loading"
                ).click()
            page.locator("#search-list").get_by_role(
                "switch", name="Allow future loading"
            ).wait_for()
            page.screenshot(path=artifact_dir / "dashboard-capabilities.png", full_page=True)
            page.get_by_role("link", name="Manage", exact=True).click()
            page.get_by_text("No staged update state.", exact=True).wait_for()
            page.get_by_role("link", name="System details", exact=True).click()
            assert page.locator("#reasoning-tier").inner_text() == "not selected"
            assert page.locator("#secure-audit-status").inner_text() == "not configured"
            page.get_by_role("link", name="Capabilities", exact=True).click()
            with page.expect_response(lambda response: response.url.endswith("/api/language")):
                page.get_by_label("Language").select_option("zh-CN")
            page.get_by_role("heading", name="能力库", level=2).wait_for()
            page.get_by_role("link", name="对话", exact=True).click()
            with page.expect_response(lambda response: response.url.endswith("/api/status")):
                page.get_by_role("button", name="刷新对话").click()
            with page.expect_response(lambda response: "/api/conversation" in response.url):
                page.get_by_label("选择对话").select_option("019ff540-d2c0-72d3-8fde-c00c42ae6f58")
            page.locator("#conversation-capabilities").get_by_text("SKILL · demo").wait_for()
            page.get_by_role("link", name="管理", exact=True).click()
            with page.expect_response(lambda response: response.url.endswith("/api/status")):
                page.locator("#approval-list").get_by_role("button", name="批准").click()
            with page.expect_response(lambda response: response.url.endswith("/api/status")):
                page.locator("#context-list").get_by_role("button", name="固定").click()

            page.get_by_role("link", name="本地使用说明书", exact=True).click()
            assert page.url.endswith("#guide")
            page.get_by_role("heading", name="本地使用说明书", level=2).wait_for()

            with page.expect_response(lambda response: response.url.endswith("/api/language")):
                page.get_by_label("语言").select_option("en")
            page.get_by_role("heading", name="Local user guide", level=2).wait_for()
            page.get_by_role("link", name="Manage", exact=True).click()
            page.get_by_text("No staged update state.", exact=True).wait_for()
            page.get_by_text("No disclosed sections are resident.", exact=True).wait_for()
            page.get_by_role("link", name="System details", exact=True).click()
            assert page.locator("#reasoning-tier").inner_text() == "not selected"
            assert page.locator("#secure-audit-status").inner_text() == "not configured"
            for page_name, link_name in (
                ("conversations", "Conversations"),
                ("capabilities", "Capabilities"),
                ("manage", "Manage"),
                ("details", "System details"),
                ("guide", "Local user guide"),
            ):
                page.get_by_role("link", name=link_name, exact=True).click()
                visible_text = page.locator(f'[data-page-view="{page_name}"]').inner_text()
                assert not any("\u3400" <= character <= "\u9fff" for character in visible_text), (
                    page_name,
                    visible_text,
                )

            page.get_by_label("Language").focus()
            assert page.evaluate(
                "() => document.activeElement?.matches('a,button,input,select,summary')"
            )
            page.evaluate("scrollTo(0, 0)")
            page.screenshot(path=artifact_dir / "dashboard-desktop.png", full_page=True)
            page.get_by_role("link", name="Capabilities", exact=True).click()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator("main").evaluate("node => node.scrollWidth <= innerWidth")
            page.screenshot(path=artifact_dir / "dashboard-narrow.png", full_page=True)
            after = dict(usage)
            context.close()
            browser.close()

    assert before == after == {"model_calls": 0, "token_usage": 0}
    assert ("lifecycle", "demo/tool", "disabled") in calls
    assert ("language", "zh-CN") in calls
    assert ("language", "en") in calls
    assert ("approval", "apr_browser", "approve") in calls
    assert ("context", "pin", "demo::contract") in calls
    assert console_findings == []
    assert page_errors == []
    assert network
    assert all(status < 400 for _, status in network)
    assert all(url.startswith(dashboard_url) for url, _ in network)
