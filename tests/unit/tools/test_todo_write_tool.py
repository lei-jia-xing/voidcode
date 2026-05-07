from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.tools import TodoWriteTool, ToolCall


def test_todo_write_is_read_only_session_state_tool() -> None:
    assert TodoWriteTool.definition.read_only is True


def test_todo_write_returns_session_metadata_without_workspace_artifact(tmp_path: Path) -> None:
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
    assert not store.exists()
    payload_raw = result.data["todos"]
    assert isinstance(payload_raw, list)
    payload = cast(list[dict[str, str]], payload_raw)
    assert payload[0]["content"] == "task-a"
    assert payload[1]["status"] == "completed"
    assert result.status == "ok"
    assert result.content == "Updated 2 todos\n1. [pending/high] task-a\n2. [completed/low] task-b"
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


def test_todo_write_rejects_more_than_one_in_progress_todo(tmp_path: Path) -> None:
    tool = TodoWriteTool()

    with pytest.raises(ValueError, match="more than one `in_progress`"):
        tool.invoke(
            ToolCall(
                tool_name="todo_write",
                arguments={
                    "todos": [
                        {
                            "content": "first concurrent task",
                            "status": "in_progress",
                            "priority": "high",
                        },
                        {
                            "content": "second concurrent task",
                            "status": "in_progress",
                            "priority": "high",
                        },
                    ]
                },
            ),
            workspace=tmp_path,
        )


def test_todo_write_accepts_single_in_progress_todo(tmp_path: Path) -> None:
    tool = TodoWriteTool()

    result = tool.invoke(
        ToolCall(
            tool_name="todo_write",
            arguments={
                "todos": [
                    {"content": "queued", "status": "pending", "priority": "high"},
                    {"content": "active", "status": "in_progress", "priority": "high"},
                    {"content": "shipped", "status": "completed", "priority": "low"},
                ]
            },
        ),
        workspace=tmp_path,
    )

    summary_raw = result.data["summary"]
    assert isinstance(summary_raw, dict)
    summary = cast(dict[str, object], summary_raw)
    assert summary["in_progress"] == 1


def test_todo_write_description_codifies_harness_discipline() -> None:
    description = TodoWriteTool.definition.description

    # The description must teach the model the contract; if any of these go
    # missing the harness signal degrades and todos become decorative.
    assert "in_progress" in description
    assert "BEFORE starting" in description or "before starting" in description.lower()
    assert "IMMEDIATELY" in description or "immediately" in description.lower()
    assert "Only ONE" in description or "only one" in description.lower()
    assert "2 or more distinct steps" in description
