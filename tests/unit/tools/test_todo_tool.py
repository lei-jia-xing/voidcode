from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools.contracts import ToolCall
from voidcode.tools.todo import TodoResultBody, TodoTool


def _invoke(
    tool: TodoTool,
    arguments: dict[str, object],
    *,
    phases: tuple[dict[str, object], ...] = (),
    workspace: Path,
) -> tuple[object, tuple[dict[str, object], ...]]:
    context = ToolContext(workspace=workspace, session_id="todo-test", todo_phases=phases)
    result = tool.invoke(ToolCall(tool_name="todo", arguments=arguments), context=context)
    body = result.body
    assert isinstance(body, TodoResultBody)
    return result, body.phases


def test_init_normalizes_active_task_and_returns_phases(tmp_path: Path) -> None:
    result, phases = _invoke(TodoTool(), {"op": "init", "list": [{"phase": "Build", "items": ["compile", "test"]}]}, workspace=tmp_path)
    assert result.status == "ok"
    assert phases[0] == {"name": "Build", "tasks": [{"content": "compile", "status": "in_progress"}, {"content": "test", "status": "pending"}]}
    assert isinstance(result.body, TodoResultBody)
    assert result.body.summary == {"total": 2, "pending": 1, "in_progress": 1, "completed": 0, "abandoned": 0, "blocked": 0, "active": 2}
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


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("blocked", "unblock it before starting"),
        ("completed", "already completed"),
        ("abandoned", "already abandoned"),
    ],
)
def test_start_rejects_blocked_and_closed_tasks_without_mutation(tmp_path: Path, status: str, error: str) -> None:
    phases = ({"name": "Tasks", "tasks": [{"content": "task", "status": status}]},)
    with pytest.raises(ValueError, match=error):
        _invoke(TodoTool(), {"op": "start", "task": "task"}, phases=phases, workspace=tmp_path)
    assert phases == ({"name": "Tasks", "tasks": [{"content": "task", "status": status}]},)


@pytest.mark.parametrize(("closed_op", "opposite_op", "closed_status"), [("done", "drop", "completed"), ("drop", "done", "abandoned")])
def test_closed_task_operations_are_idempotent_but_do_not_switch_state(tmp_path: Path, closed_op: str, opposite_op: str, closed_status: str) -> None:
    phases = ({"name": "Tasks", "tasks": [{"content": "task", "status": closed_status}]},)
    same, unchanged = _invoke(TodoTool(), {"op": closed_op, "task": "task"}, phases=phases, workspace=tmp_path)
    assert isinstance(same.body, TodoResultBody)
    assert same.body.mutated is False
    assert unchanged == phases
    with pytest.raises(ValueError, match="cannot become"):
        _invoke(TodoTool(), {"op": opposite_op, "task": "task"}, phases=phases, workspace=tmp_path)


def test_todo_output_compacts_changes_but_body_keeps_full_snapshot(tmp_path: Path) -> None:
    tool = TodoTool()
    _, phases = _invoke(
        tool,
        {"op": "init", "list": [{"phase": "Build", "items": ["compile", "test"]}, {"phase": "Docs", "items": ["guide"]}]},
        workspace=tmp_path,
    )
    result, updated = _invoke(tool, {"op": "start", "task": "test"}, phases=phases, workspace=tmp_path)
    assert result.output.text.count("compile") == 1
    assert "test" in result.output.text
    assert "guide" not in result.output.text
    assert "Next active: test" in result.output.text
    assert "Summary:" in result.output.text
    assert updated[1]["tasks"] == [{"content": "guide", "status": "pending"}]
    assert result.body.phases == updated


def test_append_and_rm_are_atomic_on_failure(tmp_path: Path) -> None:
    tool = TodoTool()
    _, phases = _invoke(tool, {"op": "init", "items": ["existing"]}, workspace=tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        _invoke(tool, {"op": "append", "phase": "Tasks", "items": ["new", "existing"]}, phases=phases, workspace=tmp_path)
    assert phases == ({"name": "Tasks", "tasks": [{"content": "existing", "status": "in_progress"}]},)
    _, phases = _invoke(tool, {"op": "rm", "task": "existing"}, phases=phases, workspace=tmp_path)
    assert phases == ({"name": "Tasks", "tasks": []},)


def test_view_is_read_only_and_old_payload_is_rejected(tmp_path: Path) -> None:
    tool = TodoTool()
    phases = ({"name": "Tasks", "tasks": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]},)
    result, viewed = _invoke(tool, {"op": "view"}, phases=phases, workspace=tmp_path)
    assert isinstance(result.body, TodoResultBody)
    assert result.body.mutated is False
    assert viewed == phases
    with pytest.raises(ValueError):
        tool.invoke(ToolCall(tool_name="todo", arguments={"todos": []}), context=ToolContext(workspace=tmp_path, session_id="session"))
