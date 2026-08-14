from __future__ import annotations

import json
from pathlib import Path

from capabilityhub.codex_sessions import codex_task_capabilities, codex_task_index


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_task_index_is_bounded_and_deduplicates_without_opening_histories(tmp_path: Path) -> None:
    task_id = "019ff540-d2c0-72d3-8fde-c00c42ae6f58"
    _write_jsonl(
        tmp_path / ".codex" / "session_index.jsonl",
        [
            {"id": task_id, "thread_name": "Old title", "updated_at": "2026-08-11T00:00:00Z"},
            {"id": task_id, "thread_name": "Current title", "updated_at": "2026-08-12T00:00:00Z"},
            {"id": "invalid", "thread_name": "ignored", "updated_at": "now"},
        ],
    )

    result = codex_task_index(tmp_path)

    assert result["total"] == 1
    assert result["entries"] == [
        {
            "archived": False,
            "id": task_id,
            "source": "task_index",
            "title": "Current title",
            "updated_at": "2026-08-12T00:00:00Z",
        }
    ]


def test_task_index_discovers_old_active_and_archived_traces(tmp_path: Path) -> None:
    active_id = "019ff540-d2c0-72d3-8fde-c00c42ae6f58"
    archived_id = "019ff541-d2c0-72d3-8fde-c00c42ae6f59"
    _write_jsonl(
        tmp_path / ".codex" / "sessions" / "2026" / f"rollout-{active_id}.jsonl",
        [],
    )
    _write_jsonl(
        tmp_path / ".codex" / "archived_sessions" / f"rollout-{archived_id}.jsonl",
        [],
    )

    result = codex_task_index(tmp_path)
    by_id = {entry["id"]: entry for entry in result["entries"]}  # type: ignore[index]

    assert result["total"] == 2
    assert by_id[active_id]["archived"] is False  # type: ignore[index]
    assert by_id[archived_id]["archived"] is True  # type: ignore[index]
    assert all(entry["trace_available"] is True for entry in by_id.values())


def test_task_capabilities_only_inspects_tool_envelopes(tmp_path: Path) -> None:
    task_id = "019ff540-d2c0-72d3-8fde-c00c42ae6f58"
    trace = tmp_path / ".codex" / "sessions" / "2026" / "08" / f"rollout-{task_id}.jsonl"
    _write_jsonl(
        trace,
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "content": "C:/secret/fake-skill/SKILL.md tools.fake_tool",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "mcp__files__read",
                    "input": '{"path":"C:/safe/pdf-reader/SKILL.md"}',
                },
            },
        ],
    )

    result = codex_task_capabilities(task_id, tmp_path)

    assert result["status"] == "observed"
    assert result["capabilities"] == [
        {"kind": "skill", "name": "pdf-reader", "source": "observed_skill_instruction_read"},
        {"kind": "tool", "name": "mcp__files__read", "source": "observed_tool_call"},
    ]
    assert result["coverage"] == {
        "bytes_scanned": trace.stat().st_size,
        "tool_envelopes": 1,
        "trace_complete": True,
    }
    assert "fake-skill" not in json.dumps(result)


def test_task_capabilities_refuses_oversized_trace(tmp_path: Path) -> None:
    task_id = "019ff540-d2c0-72d3-8fde-c00c42ae6f58"
    trace = tmp_path / ".codex" / "sessions" / f"rollout-{task_id}.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_bytes(b"x" * 100)

    result = codex_task_capabilities(task_id, tmp_path, max_scan_bytes=50)

    assert result["status"] == "trace_too_large"
    assert result["capabilities"] == []
