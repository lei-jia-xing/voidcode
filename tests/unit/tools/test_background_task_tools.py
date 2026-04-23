from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.contracts import BackgroundTaskResult, RuntimeSessionResult
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)
from voidcode.tools import BackgroundCancelTool, BackgroundOutputTool, ToolCall


class _StubBackgroundRuntime:
    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult:
        assert task_id == "task-1"
        return BackgroundTaskResult(
            task_id="task-1",
            parent_session_id="leader-session",
            child_session_id="child-session",
            status="completed",
            summary_output="Completed: delegated work",
            result_available=True,
        )

    def session_result(self, *, session_id: str) -> RuntimeSessionResult:
        assert session_id == "child-session"
        return RuntimeSessionResult(
            session=SessionState(
                session=SessionRef(id="child-session", parent_id="leader-session"),
                status="completed",
                turn=1,
            ),
            prompt="delegated",
            status="completed",
            summary="Completed: delegated work",
            output="done",
            transcript=(
                EventEnvelope(
                    session_id="child-session",
                    sequence=1,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"prompt": "delegated"},
                ),
            ),
            last_event_sequence=1,
        )

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        assert task_id == "task-1"
        return BackgroundTaskState(
            task=BackgroundTaskRef(id="task-1"),
            status="cancelled",
            request=BackgroundTaskRequestSnapshot(
                prompt="delegated", parent_session_id="leader-session"
            ),
            error="cancelled before start",
        )


def test_background_output_tool_returns_task_summary(tmp_path: Path) -> None:
    tool = BackgroundOutputTool(runtime=_StubBackgroundRuntime())

    result = tool.invoke(
        ToolCall(
            tool_name="background_output", arguments={"task_id": "task-1", "full_session": True}
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content == "done"
    assert result.data["task_id"] == "task-1"
    session_payload = result.data["session"]
    assert isinstance(session_payload, dict)
    assert session_payload["session_id"] == "child-session"


def test_background_cancel_tool_cancels_single_task(tmp_path: Path) -> None:
    tool = BackgroundCancelTool(runtime=_StubBackgroundRuntime())

    result = tool.invoke(
        ToolCall(tool_name="background_cancel", arguments={"taskId": "task-1"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["task_id"] == "task-1"
    assert result.data["status"] == "cancelled"


def test_background_cancel_tool_rejects_all_true(tmp_path: Path) -> None:
    tool = BackgroundCancelTool(runtime=_StubBackgroundRuntime())

    with pytest.raises(ValueError, match="not supported"):
        tool.invoke(
            ToolCall(tool_name="background_cancel", arguments={"all": True}),
            workspace=tmp_path,
        )
