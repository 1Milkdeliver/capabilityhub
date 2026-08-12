#!/usr/bin/env python3
"""Dependency-free MCP stdio runtime for the packaged CapabilityHub plugin."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
PROTOCOL = "2025-06-18"


def _skills() -> list[dict[str, str]]:
    result = []
    for path in sorted(SKILLS.glob("*/SKILL.md")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result.append(
            {
                "coordinate": f"plugin/{path.parent.name}",
                "kind": "skill",
                "revision": f"plugin/{path.parent.name}@sha256:{digest}",
                "summary": _description(path),
                "path": str(path),
            }
        )
    return result


def _description(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("description:"):
            return line.partition(":")[2].strip()
    return "Packaged Skill instructions"


def _tools() -> list[dict[str, Any]]:
    common = {"type": "object", "additionalProperties": True}
    return [
        {
            "name": "capability.search",
            "description": "Search compact packaged capability metadata.",
            "inputSchema": common,
        },
        {
            "name": "capability.load",
            "description": "Load one exact packaged Skill revision.",
            "inputSchema": common,
        },
        {
            "name": "capability.execute",
            "description": "Execute a capability when supported; packaged Skills are load-only.",
            "inputSchema": common,
        },
    ]


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    skills = _skills()
    if name == "capability.search":
        query = str(arguments.get("query", "")).casefold()
        cards = [
            {key: item[key] for key in ("coordinate", "kind", "revision", "summary")}
            for item in skills
            if not query
            or query in item["coordinate"].casefold()
            or query in item["summary"].casefold()
        ]
        payload = {
            "cards": cards[:8],
            "inventory": {
                "active_by_kind": {
                    "skill": len(skills),
                    "mcp": 1,
                    "cli": 0,
                    "api": 0,
                    "rag": 0,
                },
                "active_total": len(skills) + 1,
                "generation": "plugin-bundled",
                "status": "complete",
            },
            "total_matches": len(cards),
        }
        return _success(payload)
    if name == "capability.load":
        requested = str(arguments.get("capability_ref", arguments.get("revision", "")))
        match = next(
            (item for item in skills if requested in {item["revision"], item["coordinate"]}),
            None,
        )
        if match is None:
            return _error("revision_not_found", "The packaged capability was not found.")
        body = Path(match["path"]).read_text(encoding="utf-8")
        return _success(
            {
                "kind": "skill",
                "revision": match["revision"],
                "sections": {"instructions": body},
            }
        )
    if name == "capability.execute":
        return _error("operation_not_supported", "Packaged Skills are load-only.")
    return _error("tool_not_found", "The requested tool is unavailable.")


def _success(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
        "structuredContent": payload,
        "isError": False,
    }


def _error(code: str, message: str) -> dict[str, Any]:
    payload = {"error": {"code": code, "message": message, "retryable": False}}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
        "structuredContent": payload,
        "isError": True,
    }


def _respond(identifier: object, result: object = None, error: object = None) -> None:
    response = {"jsonrpc": "2.0", "id": identifier}
    if error is None:
        response["result"] = result
    else:
        response["error"] = error
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            method = request.get("method")
            identifier = request.get("id")
            if method == "notifications/initialized":
                continue
            if method == "initialize":
                _respond(
                    identifier,
                    {
                        "protocolVersion": PROTOCOL,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "capabilityhub-plugin", "version": "0.1.1"},
                    },
                )
            elif method == "tools/list":
                _respond(identifier, {"tools": _tools()})
            elif method == "tools/call":
                params = request.get("params", {})
                _respond(
                    identifier,
                    _call(str(params.get("name", "")), params.get("arguments", {})),
                )
            elif identifier is not None:
                _respond(identifier, error={"code": -32601, "message": "Method not found"})
        except Exception:
            _respond(None, error={"code": -32603, "message": "Internal error"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
