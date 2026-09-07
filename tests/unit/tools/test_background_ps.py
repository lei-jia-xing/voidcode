from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.background.models import BackgroundTaskRef, BackgroundTaskRequestSnapshot, BackgroundTaskState
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools.contracts import ToolCall
from voidcode.tools.delegation.task_ps import TaskPsTool
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


class _RosterRuntime:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.parents: list[str] = []

    def background_task_roster(self, *, parent_session_id: str) -> dict[str, object]:
        self.parents.append(parent_session_id)
        return self.payload


def test_background_ps_requires_parent_runtime_context(tmp_path: Path) -> None:
    tool = TaskPsTool(runtime=_RosterRuntime({"tasks": []}))
    with pytest.raises(RuntimeError, match="active runtime"):
        tool.invoke(ToolCall(tool_name="background_ps"), workspace=tmp_path)


def test_background_ps_uses_active_parent_and_returns_bounded_projection(tmp_path: Path) -> None:
    payload = {
        "parent_session_id": "parent",
        "tasks": [
            {
                "task_id": "task-running",
                "status": "running",
                "child_session_id": "child",
                "result_available": False,
                "next_steps": {"task": 'task(operation="output", task_id="task-running")'},
            },
            {"task_id": "task-done", "status": "completed", "terminal": True, "result_available": True},
        ],
        "task_count": 2,
        "truncated": False,
    }
    runtime = _RosterRuntime(payload)
    tool = TaskPsTool(runtime=runtime)
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="parent")):
        result = tool.invoke(ToolCall(tool_name="background_ps"), workspace=tmp_path)
    assert runtime.parents == ["parent"]
    assert result.data == payload
    assert "prompt" not in str(result.data)
    assert "transcript" not in str(result.data)
    assert result.data["tasks"][0]["status"] == "running"  # type: ignore[index]
    assert result.data["tasks"][1]["terminal"] is True  # type: ignore[index]


def test_runtime_background_ps_scopes_and_omits_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sessions.sqlite3")
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-running"),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="secret prompt " + ("x" * 10000),
                parent_session_id="parent-a",
                session_id="child-a",
                metadata={
                    "delegation": {
                        "mode": "background",
                        "subagent_type": "worker",
                        "parallel_group_id": "group-a",
                        "parallel_group_size": "2",
                    }
                },
            ),
            created_at_unix_ms=1000,
            started_at_unix_ms=1100,
            updated_at=2,
        ),
    )
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="task-done"),
            status="completed",
            request=BackgroundTaskRequestSnapshot(
                prompt="other",
                parent_session_id="parent-b",
                metadata={"delegation": {"mode": "background", "subagent_type": "worker"}},
            ),
            created_at_unix_ms=1000,
            started_at_unix_ms=1100,
            finished_at_unix_ms=2100,
            updated_at=3,
            result_available=True,
        ),
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, session_store=store)
    monkeypatch.setattr(runtime._background_task_supervisor, "reconcile_background_tasks_if_needed", lambda: None)
    monkeypatch.setattr(runtime._background_task_supervisor, "drain_queued_background_tasks", lambda: None)
    try:
        payload = runtime.background_task_roster(parent_session_id="parent-a")
    finally:
        runtime.shutdown_background_tasks()
    assert payload["task_count"] == 1
    assert payload["total_task_count"] == 1
    item = payload["tasks"][0]  # type: ignore[index]
    assert item["task_id"] == "task-running"
    assert item["status"] == "running"
    assert item["parallel_group_id"] == "group-a"
    assert item["parallel_group_size"] == 2
    assert "secret prompt" not in str(payload)
    assert "transcript" not in str(payload)


def test_background_ps_empty_roster(tmp_path: Path) -> None:
    runtime = _RosterRuntime({"parent_session_id": "parent", "tasks": [], "task_count": 0, "truncated": False})
    tool = TaskPsTool(runtime=runtime)
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="parent")):
        result = tool.invoke(ToolCall(tool_name="background_ps"), workspace=tmp_path)
    assert result.content == "Background task roster: 0 task(s)"
    assert result.data["tasks"] == []


def test_background_ps_rejects_arguments(tmp_path: Path) -> None:
    tool = TaskPsTool(runtime=_RosterRuntime({"tasks": []}))
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="parent")):
        with pytest.raises(ValueError, match="accepts no arguments"):
            tool.invoke(ToolCall(tool_name="background_ps", arguments={"task_id": "x"}), workspace=tmp_path)
