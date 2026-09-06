from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.tools import TodoTool, ToolCall
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def _invoke(
    tool: TodoTool,
    arguments: dict[str, object],
    *,
    phases: tuple[dict[str, object], ...] = (),
    workspace: Path,
) -> tuple[object, tuple[dict[str, object], ...]]:
    context = RuntimeToolInvocationContext(session_id="todo-test", todo_phases=phases)
    with bind_runtime_tool_context(context):
        result = tool.invoke(ToolCall(tool_name="todo", arguments=arguments), workspace=workspace)
    return result, cast(tuple[dict[str, object], ...], tuple(cast(list[dict[str, object]], result.data["phases"])))


def test_todo_is_runtime_owned_and_read_only() -> None:
    assert TodoTool.definition.read_only is True
    assert TodoTool.definition.effective_replay_policy == "safe"


def test_init_normalizes_active_task_and_returns_phases(tmp_path: Path) -> None:
    result, phases = _invoke(TodoTool(), {"op": "init", "list": [{"phase": "Build", "items": ["compile", "test"]}]}, workspace=tmp_path)
    assert result.status == "ok"
    assert phases[0] == {"name": "Build", "tasks": [{"content": "compile", "status": "in_progress"}, {"content": "test", "status": "pending"}]}
    assert result.data["summary"] == {"total": 2, "pending": 1, "in_progress": 1, "completed": 0, "abandoned": 0, "blocked": 0, "active": 2}
    assert not (tmp_path / ".voidcode" / "todos.json").exists()


def test_single_operations_update_one_runtime_snapshot(tmp_path: Path) -> None:
    tool = TodoTool()
    _, phases = _invoke(tool, {"op": "init", "items": ["first", "second"]}, workspace=tmp_path)
    _, phases = _invoke(tool, {"op": "start", "task": "second"}, phases=phases, workspace=tmp_path)
    _, phases = _invoke(tool, {"op": "block", "task": "second", "reason": " waiting\tfor dependency "}, phases=phases, workspace=tmp_path)
    assert cast(list[dict[str, object]], phases[0]["tasks"])[1] == {"content": "second", "status": "blocked", "blocker": "waiting for dependency"}
    _, phases = _invoke(tool, {"op": "unblock", "task": "second"}, phases=phases, workspace=tmp_path)
    _, phases = _invoke(tool, {"op": "done", "task": "second"}, phases=phases, workspace=tmp_path)
    assert cast(list[dict[str, object]], phases[0]["tasks"])[1] == {"content": "second", "status": "completed"}


def test_block_preserves_closed_tasks_and_drop_marks_abandoned(tmp_path: Path) -> None:
    tool = TodoTool()
    _, phases = _invoke(tool, {"op": "init", "items": ["done", "open"]}, workspace=tmp_path)
    _, phases = _invoke(tool, {"op": "done", "task": "done"}, phases=phases, workspace=tmp_path)
    _, phases = _invoke(tool, {"op": "block", "phase": "Tasks", "reason": "dependency"}, phases=phases, workspace=tmp_path)
    tasks = cast(list[dict[str, object]], phases[0]["tasks"])
    assert tasks[0] == {"content": "done", "status": "completed"}
    assert tasks[1] == {"content": "open", "status": "blocked", "blocker": "dependency"}
    _, phases = _invoke(tool, {"op": "drop", "task": "open"}, phases=phases, workspace=tmp_path)
    assert cast(list[dict[str, object]], phases[0]["tasks"])[1] == {"content": "open", "status": "abandoned"}


def test_append_and_rm_are_atomic_on_failure(tmp_path: Path) -> None:
    tool = TodoTool()
    _, phases = _invoke(tool, {"op": "init", "items": ["existing"]}, workspace=tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        _invoke(tool, {"op": "append", "phase": "Tasks", "items": ["new", "existing"]}, phases=phases, workspace=tmp_path)
    assert phases == ({"name": "Tasks", "tasks": [{"content": "existing", "status": "in_progress"}]},)
    _, phases = _invoke(tool, {"op": "rm", "task": "existing"}, phases=phases, workspace=tmp_path)
    assert phases == ({"name": "Tasks", "tasks": []},)


def test_init_rejects_blank_and_trim_colliding_items_atomically(tmp_path: Path) -> None:
    tool = TodoTool()
    _, phases = _invoke(tool, {"op": "init", "items": ["keep"]}, workspace=tmp_path)
    for items in (["new", "   "], ["new", " new "]):
        with pytest.raises(ValueError, match="non-empty|Duplicate task|already exists"):
            _invoke(tool, {"op": "append", "phase": "Tasks", "items": list(items)}, phases=phases, workspace=tmp_path)


def test_rm_failure_is_atomic(tmp_path: Path) -> None:
    tool = TodoTool()
    _, phases = _invoke(tool, {"op": "init", "items": ["keep"]}, workspace=tmp_path)
    with pytest.raises(ValueError, match="not found"):
        _invoke(tool, {"op": "rm", "task": "missing"}, phases=phases, workspace=tmp_path)
    assert phases == ({"name": "Tasks", "tasks": [{"content": "keep", "status": "in_progress"}]},)


def test_view_is_read_only_and_old_payload_is_rejected(tmp_path: Path) -> None:
    tool = TodoTool()
    phases = ({"name": "Tasks", "tasks": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]},)
    result, viewed = _invoke(tool, {"op": "view"}, phases=phases, workspace=tmp_path)
    assert result.data["mutated"] is False
    assert viewed == phases
    with pytest.raises(ValueError, match="invalid op"):
        tool.invoke(ToolCall(tool_name="todo", arguments={"todos": []}), workspace=tmp_path)
