from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from voidcode.tools import TodoWriteTool, ToolCall


def test_todo_write_persists_todos_json(tmp_path: Path) -> None:
    tool = TodoWriteTool()
    result = tool.invoke(
        ToolCall(
            tool_name="todo_write",
            arguments={
                "todos": [
                    {"content": "  task-a  ", "status": "pending", "priority": "high"},
                    {"content": "task-b", "status": "completed", "priority": "low"},
                ]
            },
        ),
        workspace=tmp_path,
    )

    store = tmp_path / ".voidcode" / "todos.json"
    assert store.exists()
    payload = json.loads(store.read_text(encoding="utf-8"))
    assert payload[0]["content"] == "task-a"
    assert payload[1]["status"] == "completed"
    assert result.status == "ok"
    summary_raw = result.data["summary"]
    assert isinstance(summary_raw, dict)
    summary = cast(dict[str, object], summary_raw)
    assert summary["total"] == 2


def test_todo_write_rejects_invalid_status(tmp_path: Path) -> None:
    tool = TodoWriteTool()

    with pytest.raises(ValueError, match="invalid status"):
        tool.invoke(
            ToolCall(
                tool_name="todo_write",
                arguments={"todos": [{"content": "a", "status": "bad", "priority": "high"}]},
            ),
            workspace=tmp_path,
        )


def test_todo_write_rejects_invalid_priority(tmp_path: Path) -> None:
    tool = TodoWriteTool()

    with pytest.raises(ValueError, match="invalid priority"):
        tool.invoke(
            ToolCall(
                tool_name="todo_write",
                arguments={"todos": [{"content": "a", "status": "pending", "priority": "urgent"}]},
            ),
            workspace=tmp_path,
        )
