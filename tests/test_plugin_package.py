from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, TextIO, cast

from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from capabilityhub.mcp_server import create_empty_mcp_server

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "capabilityhub"
NODE = Path(
    shutil.which("node")
    or "C:/Users/Huawei/.cache/codex-runtimes/codex-primary-runtime/"
    "dependencies/node/bin/node.exe"
)


def _clean_path_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PATH"] = ""
    return environment


def test_helpme_is_the_plugin_entry_and_menu_is_progressive() -> None:
    manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    skill = (PLUGIN / "skills" / "helpme" / "SKILL.md").read_text(encoding="utf-8")

    assert manifest["interface"]["defaultPrompt"] == ["/helpme", "/myskills"]
    assert "name: helpme" in skill
    assert "static message catalogs" in skill
    assert "Do not discover or preload" in skill


def test_plugin_declares_portable_local_mcp_runtime() -> None:
    config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))

    assert config == {
        "mcpServers": {
            "capabilityhub-local": {
                "args": ["./runtime/capabilityhub_mcp.cjs"],
                "command": "node",
                "cwd": ".",
                "description": (
                    "Local progressive inventory, search, load, and controlled execution."
                ),
                "title": "CapabilityHub Local",
            }
        }
    }


def test_bundled_mcp_runs_from_clean_path_and_serves_three_tools(tmp_path: Path) -> None:
    installed = tmp_path / "plugin"
    shutil.copytree(PLUGIN, installed)
    process = subprocess.Popen(
        [str(NODE), str(installed / "runtime" / "capabilityhub_mcp.cjs")],
        cwd=installed,
        env=_clean_path_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process_input = cast(TextIO, process.stdin)
    process_output = cast(TextIO, process.stdout)

    def request(
        identifier: int, method: str, params: dict[str, object] | None = None
    ) -> dict[str, Any]:
        message: dict[str, object] = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            message["params"] = params
        process_input.write(json.dumps(message) + "\n")
        process_input.flush()
        return cast(dict[str, Any], json.loads(process_output.readline()))

    initialized = request(
        1,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
    )
    assert initialized["result"]["serverInfo"]["name"] == "capabilityhub-plugin"
    listed = request(2, "tools/list")
    assert [tool["name"] for tool in listed["result"]["tools"]] == [
        "capability.search",
        "capability.load",
        "capability.execute",
    ]
    searched = request(
        3,
        "tools/call",
        {"name": "capability.search", "arguments": {"query": ""}},
    )
    payload = searched["result"]["structuredContent"]
    assert payload["inventory"]["active_by_kind"] == {
        "api": 0,
        "cli": 0,
        "mcp": 1,
        "rag": 0,
        "skill": 2,
    }
    assert {card["coordinate"] for card in payload["cards"]} == {
        "plugin/helpme",
        "plugin/myskills",
    }
    revision = payload["cards"][0]["revision"]
    loaded = request(
        4,
        "tools/call",
        {"name": "capability.load", "arguments": {"capability_ref": revision}},
    )
    assert loaded["result"]["isError"] is False
    denied = request(
        5,
        "tools/call",
        {"name": "capability.execute", "arguments": {"capability_ref": revision}},
    )
    assert denied["result"]["isError"] is True
    assert denied["result"]["structuredContent"]["error"]["code"] == (
        "operation_not_supported"
    )
    process_input.close()
    assert process.wait(timeout=5) == 0
    assert process.stderr is not None
    assert process.stderr.read() == ""


def test_official_mcp_client_lists_and_calls_bundled_runtime_with_clean_path(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "plugin"
    shutil.copytree(PLUGIN, installed)
    parameters = StdioServerParameters(
        command=str(NODE),
        args=[str(installed / "runtime" / "capabilityhub_mcp.cjs")],
        cwd=installed,
        env=_clean_path_environment(),
    )

    async def scenario() -> None:
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "capability.search",
                "capability.load",
                "capability.execute",
            ]
            searched = await session.call_tool("capability.search", {"query": ""})
            assert searched.is_error is False
            payload = cast(dict[str, object], searched.structured_content)
            inventory = cast(dict[str, object], payload["inventory"])
            assert inventory["active_total"] == 3

    asyncio.run(scenario())


def test_every_visible_menu_command_resolves_to_runtime_or_unavailable() -> None:
    mapping = json.loads((PLUGIN / "menu-map.json").read_text(encoding="utf-8"))
    assert mapping["*"]["type"] == "unavailable"
    valid_types = {"mcp", "cli", "menu", "navigation", "unavailable"}
    visible: set[str] = set()
    for skill in ("helpme", "myskills"):
        locale_dir = PLUGIN / "skills" / skill / "references" / "locales"
        for path in locale_dir.glob("*.json"):
            catalog = json.loads(path.read_text(encoding="utf-8"))
            visible.update(_catalog_commands(catalog))
    for command in visible:
        route = mapping.get(command, mapping["*"])
        assert route["type"] in valid_types
        assert route["target"]
    assert mapping["/myskills risks <name>"]["type"] == "unavailable"
    assert mapping["/myskills conflicts"]["type"] == "unavailable"


def test_helpme_locale_catalogs_have_matching_parenthesized_menus() -> None:
    locale_dir = PLUGIN / "skills" / "helpme" / "references" / "locales"
    catalogs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in locale_dir.glob("*.json")
    }
    assert set(catalogs) == {"en", "zh-CN"}

    expected_topics = {
        "overview",
        "capabilities",
        "consumption",
        "runtime",
        "security",
        "evaluation",
        "settings",
        "about",
    }
    reference = catalogs["en"]
    assert set(reference["topics"]) == expected_topics
    reference_root_items = _group_items(reference["root"]["groups"])
    assert "/myskills" in reference_root_items
    assert "/helpme providers" in reference_root_items
    assert "/helpme routing" in reference_root_items
    assert "/helpme mcp" in reference_root_items
    assert "/helpme language" in reference_root_items
    assert set(reference["navigation"]) == {"/helpme back", "/helpme home"}

    for catalog in catalogs.values():
        assert set(catalog["topics"]) == expected_topics
        root_items = _group_items(catalog["root"]["groups"])
        assert set(root_items) == set(reference_root_items)
        assert set(catalog["language"]) == set(reference["language"])
        assert set(catalog["navigation"]) == set(reference["navigation"])
        for topic in expected_topics:
            assert set(catalog["topics"][topic]) == set(reference["topics"][topic])
        descriptions = list(root_items.values())
        descriptions.extend(catalog["language"].values())
        descriptions.extend(catalog["navigation"].values())
        for topic in catalog["topics"].values():
            descriptions.extend(topic.values())
        assert all(
            (text.startswith("(") and text.endswith(")"))
            or (text.startswith("\uff08") and text.endswith("\uff09"))
            for text in descriptions
        )


def test_myskills_catalogs_match_and_keep_professional_terms_visible() -> None:
    skill_root = PLUGIN / "skills" / "myskills"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    catalogs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in (skill_root / "references" / "locales").glob("*.json")
    }
    assert set(catalogs) == {"en", "zh-CN"}
    assert "Never intercept native" in skill
    reference_items = _group_items(catalogs["en"]["groups"])
    for command in (
        "/myskills list",
        "/myskills loaded",
        "/myskills providers",
        "/myskills routing <name>",
        "/myskills lifecycle <name>",
        "/myskills risks <name>",
        "/myskills conflicts",
    ):
        assert command in reference_items
    for catalog in catalogs.values():
        items = _group_items(catalog["groups"])
        assert set(items) == set(reference_items)
        assert set(catalog["navigation"]) == {
            "/myskills back",
            "/helpme language",
            "/helpme home",
        }
        assert all(
            (text.startswith("(") and text.endswith(")"))
            or (text.startswith("\uff08") and text.endswith("\uff09"))
            for text in items.values()
        )
        assert all(
            (text.startswith("(") and text.endswith(")"))
            or (text.startswith("\uff08") and text.endswith("\uff09"))
            for text in catalog["navigation"].values()
        )


def test_fresh_install_and_upgrade_keep_plugin_actions_connected_to_mcp(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    cache = home / ".codex" / "plugins" / "cache" / "local" / "capabilityhub"
    config = home / ".codex" / "config.toml"
    project.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    config.write_text(
        '[plugins."capabilityhub@local"]\nenabled = true\n'
        "[mcp_servers.capabilityhub-local]\ncommand = 'capabilityhub'\nargs = ['mcp-serve']\n",
        encoding="utf-8",
    )

    shutil.copytree(PLUGIN, cache / "0.1.0")
    shutil.copytree(PLUGIN, cache / "0.1.1")
    server = create_empty_mcp_server(home=home, project=project, refresh_interval_seconds=0)

    async def scenario() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "capability.search",
                "capability.load",
                "capability.execute",
            ]
            result = await client.call_tool(
                "capability.search",
                {
                    "query": "",
                    "task_id": "plugin-upgrade",
                    "include_inventory": True,
                    "include_cards": True,
                    "max_output_tokens": 2_000,
                },
            )
            assert not result.is_error
            payload = cast(dict[str, object], result.structured_content)
            inventory = cast(dict[str, object], payload["inventory"])
            by_kind = cast(dict[str, int], inventory["active_by_kind"])
            assert by_kind["skill"] == 2
            assert by_kind["mcp"] == 1
            cards = cast(list[dict[str, object]], payload["cards"])
            revisions = {cast(str, card["revision"]) for card in cards}
            assert any("helpme" in revision for revision in revisions)
            assert any("myskills" in revision for revision in revisions)

    asyncio.run(scenario())

    installed_skills = {
        path.parent.name for path in (cache / "0.1.1" / "skills").glob("*/SKILL.md")
    }
    assert installed_skills == {"helpme", "myskills"}
    assert installed_skills.isdisjoint({"help", "skills", "status", "model", "mcp"})


def _group_items(groups: list[dict[str, object]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for group in groups:
        items = group["items"]
        assert isinstance(items, dict)
        assert all(isinstance(key, str) and isinstance(value, str) for key, value in items.items())
        merged.update(items)
    return merged


def _catalog_commands(value: object) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        result.update(key for key in value if isinstance(key, str) and key.startswith("/"))
        for child in value.values():
            result.update(_catalog_commands(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_catalog_commands(child))
    return result
