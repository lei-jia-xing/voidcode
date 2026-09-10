from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from voidcode.graph.contracts import GraphEvent, GraphRunRequest, GraphSession
from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.question import QuestionResponse
from voidcode.runtime.service import RuntimeRequest, VoidCodeRuntime
from voidcode.tools.contracts import ToolCall


@dataclass(slots=True)
class _Step:
    tool_call: ToolCall | None = None
    output: str | None = None
    events: tuple[GraphEvent, ...] = ()
    is_finished: bool = False


class _QuestionThenWriteGraph:
    def step(self, request: GraphRunRequest, tool_results: tuple[object, ...], *, session: GraphSession) -> _Step:
        if not tool_results:
            return _Step(
                tool_call=ToolCall(
                    tool_name="question",
                    arguments={
                        "questions": [
                            {
                                "question": "Choose a path",
                                "header": "Path",
                                "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}],
                                "multiple": False,
                            }
                        ]
                    },
                )
            )
        if len(tool_results) == 1:
            return _Step(tool_call=ToolCall(tool_name="write", arguments={"path": "answered.txt", "content": "answered"}))
        return _Step(output="done", is_finished=True)


@pytest.mark.parametrize("outcome", ["complete", "interrupt", "raise"])
def test_sync_question_answer_tracks_active_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    with VoidCodeRuntime(
        workspace=tmp_path, graph=_QuestionThenWriteGraph(), config=RuntimeConfig(execution_engine="deterministic", approval_mode="allow")
    ) as runtime:
        session_id = "sync-question-lifecycle"
        waiting = runtime.run(RuntimeRequest(prompt="ask", session_id=session_id))
        request_id = next(str(event.payload["request_id"]) for event in waiting.events if event.event_type == "runtime.question_requested")
        original = runtime._resume_coordinator.answer_pending_question_response

        def answer_with_observation(**kwargs: Any) -> Any:
            assert runtime.session_debug_snapshot(session_id=session_id).active is True
            assert kwargs["run_id"]
            assert kwargs["abort_signal"] is not None
            if outcome == "raise":
                raise RuntimeError("continuation failure")
            if outcome == "interrupt":
                assert runtime.cancel_session(session_id, reason="question test").interrupted is True
            return original(**kwargs)

        monkeypatch.setattr(runtime._resume_coordinator, "answer_pending_question_response", answer_with_observation)
        responses = (QuestionResponse(header="Path", answers=("A",)),)
        if outcome == "raise":
            with pytest.raises(RuntimeError, match="continuation failure"):
                runtime.answer_question(session_id, question_request_id=request_id, responses=responses)
        else:
            response = runtime.answer_question(session_id, question_request_id=request_id, responses=responses)
            assert response.session.status == ("interrupted" if outcome == "interrupt" else "completed")
            assert (tmp_path / "answered.txt").exists() is (outcome == "complete")
        assert runtime.session_debug_snapshot(session_id=session_id).active is False


@pytest.mark.parametrize("outcome", ["complete", "interrupt", "raise"])
def test_sync_approval_resume_tracks_active_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(execution_engine="deterministic", approval_mode="ask"),
        permission_policy=PermissionPolicy(mode="ask"),
    )
    session_id = "sync-approval-lifecycle"
    waiting = runtime.run(RuntimeRequest(prompt="write approved.txt approved", session_id=session_id))
    request_id = next(event.payload["request_id"] for event in waiting.events if event.event_type == "runtime.approval_requested")
    original = runtime._resume_coordinator.resume_pending_approval_response

    def resume_with_observation(**kwargs: Any) -> Any:
        assert runtime.session_debug_snapshot(session_id=session_id).active is True
        assert kwargs["run_id"]
        assert kwargs["abort_signal"] is not None
        if outcome == "raise":
            raise RuntimeError("continuation failure")
        if outcome == "interrupt":
            result = runtime.cancel_session(session_id, reason="approval test")
            assert result.interrupted is True
        return original(**kwargs)

    monkeypatch.setattr(runtime._resume_coordinator, "resume_pending_approval_response", resume_with_observation)
    try:
        if outcome == "raise":
            with pytest.raises(RuntimeError, match="continuation failure"):
                runtime.resume(session_id, approval_request_id=request_id, approval_decision="allow")
        else:
            response = runtime.resume(session_id, approval_request_id=request_id, approval_decision="allow")
            assert response.session.status == ("interrupted" if outcome == "interrupt" else "completed")
            assert (tmp_path / "approved.txt").exists() is (outcome == "complete")
        assert runtime.session_debug_snapshot(session_id=session_id).active is False
    finally:
        runtime.__exit__(None, None, None)
