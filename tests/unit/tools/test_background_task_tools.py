from __future__ import annotations

from pathlib import Path
from typing import cast

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
    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult:
        _ = emit_result_read_hook
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


class _RawPreviewBackgroundRuntime(_StubBackgroundRuntime):
    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult:
        assert task_id == "task-1"
        return BackgroundTaskResult(
            task_id="task-1",
            parent_session_id="leader-session",
            child_session_id="child-session",
            status="completed",
            summary_output="Completed: raw child secret sentinel",
            result_available=True,
        )


class _ManyEventBackgroundRuntime(_StubBackgroundRuntime):
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
            transcript=tuple(
                EventEnvelope(
                    session_id="child-session",
                    sequence=sequence,
                    event_type="runtime.event",
                    source="runtime",
                    payload={"sequence": sequence},
                )
                for sequence in range(1, 106)
            ),
            last_event_sequence=105,
        )


class _FailedBackgroundRuntime(_StubBackgroundRuntime):
    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult:
        _ = emit_result_read_hook
        assert task_id == "task-1"
        return BackgroundTaskResult(
            task_id="task-1",
            parent_session_id="leader-session",
            child_session_id="child-session",
            status="failed",
            error="child failed",
            result_available=True,
        )


class _InterruptedBackgroundRuntime(_StubBackgroundRuntime):
    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult:
        _ = emit_result_read_hook
        assert task_id == "task-1"
        return BackgroundTaskResult(
            task_id="task-1",
            parent_session_id="leader-session",
            child_session_id="child-session",
            status="interrupted",
            error="background task interrupted before completion",
            result_available=True,
        )


class _UnavailableBackgroundRuntime(_StubBackgroundRuntime):
    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult:
        _ = emit_result_read_hook
        assert task_id == "task-1"
        return BackgroundTaskResult(
            task_id="task-1",
            parent_session_id="leader-session",
            child_session_id=None,
            status="running",
            result_available=False,
        )


class _ApprovalBlockedBackgroundRuntime(_StubBackgroundRuntime):
    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult:
        assert task_id == "task-1"
        return BackgroundTaskResult(
            task_id="task-1",
            parent_session_id="leader-session",
            child_session_id="child-session",
            status="running",
            approval_blocked=True,
            summary_output="Approval blocked on write_file: write_file alpha.txt",
            result_available=True,
        )

    def session_result(self, *, session_id: str) -> RuntimeSessionResult:
        assert session_id == "child-session"
        return RuntimeSessionResult(
            session=SessionState(
                session=SessionRef(id="child-session", parent_id="leader-session"),
                status="waiting",
                turn=1,
            ),
            prompt="delegated",
            status="waiting",
            summary="Approval blocked on write_file: write_file alpha.txt",
            transcript=(),
            last_event_sequence=1,
        )


class _BlockingUnavailableBackgroundRuntime(_UnavailableBackgroundRuntime):
    def __init__(self) -> None:
        self.load_count = 0

    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult:
        _ = emit_result_read_hook
        self.load_count += 1
        return super().load_background_task_result(
            task_id,
            emit_result_read_hook=emit_result_read_hook,
        )


class _TerminalAfterTimeoutBackgroundRuntime(_StubBackgroundRuntime):
    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult:
        assert task_id == "task-1"
        if not emit_result_read_hook:
            return BackgroundTaskResult(
                task_id="task-1",
                parent_session_id="leader-session",
                child_session_id="child-session",
                status="running",
                result_available=False,
            )
        return BackgroundTaskResult(
            task_id="task-1",
            parent_session_id="leader-session",
            child_session_id="child-session",
            status="completed",
            summary_output="completed after timeout deadline",
            result_available=True,
        )


class _EmptyOutputBackgroundRuntime(_StubBackgroundRuntime):
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
            summary="",
            output="",
            transcript=(),
            last_event_sequence=1,
        )


class _CompletedCancelRuntime(_StubBackgroundRuntime):
    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        assert task_id == "task-1"
        return BackgroundTaskState(
            task=BackgroundTaskRef(id="task-1"),
            status="completed",
            request=BackgroundTaskRequestSnapshot(
                prompt="delegated", parent_session_id="leader-session"
            ),
            session_id="child-session",
            result_available=True,
        )


class _RunningCancelRuntime(_StubBackgroundRuntime):
    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        assert task_id == "task-1"
        return BackgroundTaskState(
            task=BackgroundTaskRef(id="task-1"),
            status="running",
            request=BackgroundTaskRequestSnapshot(
                prompt="delegated", parent_session_id="leader-session"
            ),
            session_id="child-session",
            cancel_requested_at=3,
        )


class _UnknownCancelRuntime(_StubBackgroundRuntime):
    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        assert task_id == "missing-task"
        raise ValueError("unknown background task: missing-task")


def test_background_output_tool_returns_task_summary(tmp_path: Path) -> None:
    tool = BackgroundOutputTool(runtime=_StubBackgroundRuntime())

    result = tool.invoke(
        ToolCall(
            tool_name="background_output", arguments={"task_id": "task-1", "full_session": True}
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content is not None
    assert "Background task result digest:" in result.content
    assert "child_session_id: child-session" in result.content
    assert "raw child output is not injected" in result.content
    assert result.reference == "session:child-session"
    assert result.data["task_id"] == "task-1"
    delegation_payload = result.data["delegation"]
    assert isinstance(delegation_payload, dict)
    assert delegation_payload["delegated_task_id"] == "task-1"
    message_payload = result.data["message"]
    assert isinstance(message_payload, dict)
    assert message_payload["status"] == "completed"
    session_payload = result.data["session"]
    assert isinstance(session_payload, dict)
    assert session_payload["session_id"] == "child-session"
    assert session_payload["child_session_id"] == "child-session"
    assert session_payload["output_available"] is True
    assert session_payload["full_output_preserved"] is True
    assert session_payload["full_session_reference"] == "session:child-session"
    assert "output" not in session_payload


def test_background_output_default_returns_safe_summary_reference(tmp_path: Path) -> None:
    tool = BackgroundOutputTool(runtime=_RawPreviewBackgroundRuntime())

    result = tool.invoke(
        ToolCall(tool_name="background_output", arguments={"task_id": "task-1"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content is not None
    assert "Completed child session child-session" in result.content
    assert "raw child secret sentinel" not in result.content
    assert result.data["summary_output"] == (
        "Completed child session child-session; full output is preserved outside active context."
    )
    assert "raw child secret sentinel" not in str(result.data["message"])
    assert result.reference == "session:child-session"


def test_background_output_full_session_preserves_approval_summary(tmp_path: Path) -> None:
    tool = BackgroundOutputTool(runtime=_ApprovalBlockedBackgroundRuntime())

    result = tool.invoke(
        ToolCall(
            tool_name="background_output",
            arguments={"task_id": "task-1", "full_session": True},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content is not None
    assert "Approval blocked on write_file: write_file alpha.txt" in result.content
    assert "Running child session" not in result.content
    assert result.data["summary_output"] == ("Approval blocked on write_file: write_file alpha.txt")
    message_payload = result.data["message"]
    assert isinstance(message_payload, dict)
    assert message_payload["summary_output"] == (
        "Approval blocked on write_file: write_file alpha.txt"
    )


def test_background_output_tool_bounds_full_session_transcript(tmp_path: Path) -> None:
    tool = BackgroundOutputTool(runtime=_ManyEventBackgroundRuntime())

    result = tool.invoke(
        ToolCall(
            tool_name="background_output",
            arguments={"task_id": "task-1", "full_session": True, "message_limit": 200},
        ),
        workspace=tmp_path,
    )

    session_payload = result.data["session"]
    assert isinstance(session_payload, dict)
    assert session_payload["message_limit"] == 100
    assert session_payload["transcript_count"] == 100
    assert session_payload["transcript_truncated"] is True
    transcript = cast(list[dict[str, object]], session_payload["transcript"])
    assert isinstance(transcript, list)
    assert transcript[-1]["sequence"] == 100
    assert "payload" not in transcript[-1]


def test_background_output_block_timeout_returns_current_state(tmp_path: Path) -> None:
    runtime = _BlockingUnavailableBackgroundRuntime()
    tool = BackgroundOutputTool(runtime=runtime)

    result = tool.invoke(
        ToolCall(
            tool_name="background_output",
            arguments={"task_id": "task-1", "block": True, "timeout": 1},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["status"] == "running"
    assert result.data["block_timed_out"] is True
    assert "Timed out waiting" in str(result.data["guidance"])


def test_background_output_block_omits_timeout_guidance_when_final_read_is_terminal(
    tmp_path: Path,
) -> None:
    tool = BackgroundOutputTool(runtime=_TerminalAfterTimeoutBackgroundRuntime())

    result = tool.invoke(
        ToolCall(
            tool_name="background_output",
            arguments={"task_id": "task-1", "block": True, "timeout": 1},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["status"] == "completed"
    assert result.data["block_timed_out"] is False
    assert "guidance" not in result.data


def test_background_output_tool_guides_failed_child_without_retrying(tmp_path: Path) -> None:
    tool = BackgroundOutputTool(runtime=_FailedBackgroundRuntime())

    result = tool.invoke(
        ToolCall(tool_name="background_output", arguments={"task_id": "task-1"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content is not None
    assert "do not retry automatically" in result.content
    assert "session_id='child-session'" in result.content
    assert "After repeated failures" in result.content
    assert "do not retry automatically" in str(result.data["guidance"])


def test_background_output_tool_handles_interrupted_terminal_state(tmp_path: Path) -> None:
    tool = BackgroundOutputTool(runtime=_InterruptedBackgroundRuntime())

    result = tool.invoke(
        ToolCall(tool_name="background_output", arguments={"task_id": "task-1"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["status"] == "interrupted"
    assert result.data["result_available"] is True
    assert result.data["retrieval_instruction"] == 'background_output(task_id="task-1")'
    handoff = result.data["handoff_summary"]
    assert isinstance(handoff, dict)
    assert handoff["blocked_reason"] == "background task interrupted before completion"
    assert "interrupted before completion" in str(result.data["guidance"])


def test_background_output_tool_guides_unavailable_result_without_looping(tmp_path: Path) -> None:
    tool = BackgroundOutputTool(runtime=_UnavailableBackgroundRuntime())

    result = tool.invoke(
        ToolCall(tool_name="background_output", arguments={"task_id": "task-1"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content is not None
    assert "do not loop indefinitely" in result.content
    assert result.data["result_available"] is False


def test_background_output_tool_guides_empty_child_output(tmp_path: Path) -> None:
    tool = BackgroundOutputTool(runtime=_EmptyOutputBackgroundRuntime())

    result = tool.invoke(
        ToolCall(
            tool_name="background_output",
            arguments={"task_id": "task-1", "full_session": True},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content is not None
    assert "completed with empty output" in result.content
    assert "completed with empty output" in str(result.data["guidance"])


def test_background_cancel_tool_cancels_single_task(tmp_path: Path) -> None:
    tool = BackgroundCancelTool(runtime=_StubBackgroundRuntime())

    result = tool.invoke(
        ToolCall(tool_name="background_cancel", arguments={"taskId": "task-1"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["task_id"] == "task-1"
    assert result.data["status"] == "cancelled"
    assert result.data["cancellation_cause"] == "cancelled before start"
    assert result.data["terminal"] is True


def test_background_cancel_tool_reports_running_cancel_request(tmp_path: Path) -> None:
    tool = BackgroundCancelTool(runtime=_RunningCancelRuntime())

    result = tool.invoke(
        ToolCall(tool_name="background_cancel", arguments={"taskId": "task-1"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content == "Cancellation requested for background task task-1"
    assert result.data["status"] == "running"
    assert result.data["cancel_requested"] is True
    assert result.data["terminal"] is False


def test_background_cancel_tool_reports_completed_task_without_corrupting_result(
    tmp_path: Path,
) -> None:
    tool = BackgroundCancelTool(runtime=_CompletedCancelRuntime())

    result = tool.invoke(
        ToolCall(tool_name="background_cancel", arguments={"taskId": "task-1"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content == "Background task task-1 is already completed"
    assert result.data["status"] == "completed"
    assert result.data["cancel_requested"] is False
    assert result.data["terminal"] is True


def test_background_cancel_tool_reports_unknown_task_deterministically(tmp_path: Path) -> None:
    tool = BackgroundCancelTool(runtime=_UnknownCancelRuntime())

    result = tool.invoke(
        ToolCall(tool_name="background_cancel", arguments={"taskId": "missing-task"}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data == {
        "task_id": "missing-task",
        "status": "unknown",
        "session_id": None,
        "parent_session_id": None,
        "error": "unknown background task: missing-task",
        "cancellation_cause": "unknown background task",
        "cancel_requested": False,
        "terminal": True,
    }


def test_background_cancel_tool_rejects_all_true(tmp_path: Path) -> None:
    tool = BackgroundCancelTool(runtime=_StubBackgroundRuntime())

    with pytest.raises(ValueError, match="not supported"):
        tool.invoke(
            ToolCall(tool_name="background_cancel", arguments={"all": True}),
            workspace=tmp_path,
        )
