"""Privacy-bounded Codex task index and capability observations for Dashboard."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

from capabilityhub.models import JsonValue

_SESSION_ID_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_SESSION_ID = re.compile(rf"^{_SESSION_ID_PATTERN}$")
_SESSION_ID_IN_NAME = re.compile(_SESSION_ID_PATTERN)
_SKILL_PATH = re.compile(
    r"(?:^|[\\/])(?P<name>[A-Za-z0-9_.:@+-]{1,100})[\\/]SKILL\.md(?:\b|['\"`])",
    re.IGNORECASE,
)
_MCP_TOOL = re.compile(r"\b(?:mcp__|tools\.)(?P<name>[A-Za-z][A-Za-z0-9_]{1,120})")
_MAX_INDEX_BYTES = 16_000_000
_MAX_LINE_BYTES = 2_000_000
_DEFAULT_SCAN_BYTES = 128_000_000
_MAX_TRACE_FILES = 5_000


def codex_task_index(
    home: Path | None = None,
    *,
    limit: int = 100,
) -> dict[str, JsonValue]:
    """Read Codex's lightweight task index without opening conversation bodies."""

    root = _codex_root(home)
    path = root / "session_index.jsonl"
    entries: dict[str, dict[str, JsonValue]] = {}
    try:
        if path.is_file() and path.stat().st_size <= _MAX_INDEX_BYTES:
            with path.open("r", encoding="utf-8") as stream:
                for raw_line in stream:
                    item = _json_object(raw_line)
                    if item is None:
                        continue
                    task_id = item.get("id")
                    title = item.get("thread_name")
                    updated_at = item.get("updated_at")
                    if (
                        not isinstance(task_id, str)
                        or _SESSION_ID.fullmatch(task_id) is None
                        or not isinstance(title, str)
                        or not isinstance(updated_at, str)
                    ):
                        continue
                    entries[task_id] = {
                        "archived": False,
                        "id": task_id,
                        "source": "task_index",
                        "title": _compact(title, 160),
                        "updated_at": _compact(updated_at, 64),
                    }
        _merge_trace_index(root, entries)
    except OSError:
        if not entries:
            return {"entries": [], "status": "unavailable", "total": 0}
    ordered = sorted(entries.values(), key=lambda item: str(item["updated_at"]), reverse=True)
    bounded_limit = min(max(limit, 1), 2_000)
    public_entries: list[JsonValue] = [dict(item) for item in ordered[:bounded_limit]]
    return {
        "entries": public_entries,
        "status": "available",
        "total": len(ordered),
        "truncated": len(ordered) > bounded_limit,
    }


def _merge_trace_index(
    root: Path,
    entries: dict[str, dict[str, JsonValue]],
) -> None:
    """Add active and archived traces that the lightweight index no longer lists."""

    for directory, archived in (
        (root / "sessions", False),
        (root / "archived_sessions", True),
    ):
        if not directory.is_dir():
            continue
        for trace in islice(directory.rglob("*.jsonl"), _MAX_TRACE_FILES):
            match = _SESSION_ID_IN_NAME.search(trace.name)
            if match is None or not trace.is_file():
                continue
            task_id = match.group(0)
            try:
                updated_at = datetime.fromtimestamp(trace.stat().st_mtime, UTC).isoformat()
            except OSError:
                continue
            existing = entries.get(task_id)
            if existing is not None:
                existing["archived"] = archived
                existing["trace_available"] = True
                continue
            label = "Archived task" if archived else "Older task"
            entries[task_id] = {
                "archived": archived,
                "id": task_id,
                "source": "trace_discovery",
                "title": f"{label} {task_id[:8]}",
                "trace_available": True,
                "updated_at": updated_at,
            }


def codex_task_capabilities(
    task_id: str,
    home: Path | None = None,
    *,
    max_scan_bytes: int = _DEFAULT_SCAN_BYTES,
) -> dict[str, JsonValue]:
    """Return sanitized capability observations without reading message bodies.

    This parser examines tool-call envelopes only. It never reads message or reasoning
    content, and it refuses oversized histories instead of performing an unbounded scan.
    """

    if _SESSION_ID.fullmatch(task_id) is None:
        return _task_result(task_id, "invalid_task", (), ())
    path = _session_path(_codex_root(home), task_id)
    if path is None:
        return _task_result(task_id, "trace_unavailable", (), ())
    try:
        size = path.stat().st_size
    except OSError:
        return _task_result(task_id, "trace_unavailable", (), ())
    if size > max_scan_bytes:
        return {
            **_task_result(task_id, "trace_too_large", (), ()),
            "scan_limit_bytes": max_scan_bytes,
            "trace_bytes": size,
        }

    skills: set[str] = set()
    tools: set[str] = set()
    tool_envelopes = 0
    scanned_bytes = 0
    try:
        with path.open("rb") as stream:
            for raw_line in stream:
                scanned_bytes += len(raw_line)
                if len(raw_line) > _MAX_LINE_BYTES:
                    continue
                item = _json_object(raw_line.decode("utf-8", errors="replace"))
                if item is None or item.get("type") != "response_item":
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict) or payload.get("type") not in {
                    "custom_tool_call",
                    "function_call",
                }:
                    continue
                tool_envelopes += 1
                name = payload.get("name")
                if isinstance(name, str) and name not in {"exec", "functions.exec"}:
                    tools.add(_compact(name, 120))
                raw_input = payload.get("input", payload.get("arguments"))
                if not isinstance(raw_input, str):
                    continue
                skills.update(match.group("name") for match in _SKILL_PATH.finditer(raw_input))
                tools.update(match.group("name") for match in _MCP_TOOL.finditer(raw_input))
    except OSError:
        return _task_result(task_id, "trace_unavailable", (), ())
    return {
        **_task_result(task_id, "observed", tuple(sorted(skills)), tuple(sorted(tools))),
        "coverage": {
            "bytes_scanned": scanned_bytes,
            "tool_envelopes": tool_envelopes,
            "trace_complete": True,
        },
    }


def _task_result(
    task_id: str,
    status: str,
    skills: tuple[str, ...],
    tools: tuple[str, ...],
) -> dict[str, JsonValue]:
    capabilities: list[JsonValue] = [
        {"kind": "skill", "name": name, "source": "observed_skill_instruction_read"}
        for name in skills[:100]
    ]
    capabilities.extend(
        {"kind": "tool", "name": name, "source": "observed_tool_call"}
        for name in tools[:100]
    )
    return {
        "capabilities": capabilities,
        "disclaimer": "observed_calls_only",
        "status": status,
        "task_id": task_id,
        "total": len(capabilities),
    }


def _codex_root(home: Path | None) -> Path:
    selected = (home or Path.home()).resolve()
    return selected if selected.name == ".codex" else selected / ".codex"


def _session_path(root: Path, task_id: str) -> Path | None:
    for directory in (root / "sessions", root / "archived_sessions"):
        if not directory.is_dir():
            continue
        matches = tuple(directory.rglob(f"*{task_id}.jsonl"))
        if len(matches) == 1 and matches[0].is_file():
            return matches[0]
    return None


def _json_object(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _compact(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"
