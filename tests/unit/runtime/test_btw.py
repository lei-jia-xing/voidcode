"""Native /btw side question: fork semantics, history untouched, usage."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.turns import FinalTurn, ModelTurnFact, ToolTurn, TurnPlan, TurnRequest
from voidcode.runtime.contracts import RuntimeRequest, UnknownSessionError
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.tools.contracts import ToolCall


class _AnswerProducer:
    """Mock provider: records requests, answers once, never executes tools."""

    def __init__(self, answer: str = "side answer") -> None:
        self.requests: list[TurnRequest] = []
        self.answer = answer

    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object) -> TurnPlan:
        _ = tool_results, session
        self.requests.append(request)
        return FinalTurn(facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),), output=self.answer)


class _ToolAttemptProducer:
    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []

    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object) -> TurnPlan:
        _ = tool_results, session
        self.requests.append(request)
        return ToolTurn(
            facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),),
            calls=(ToolCall(tool_name="read", arguments={"path": "x"}),),
        )


def _seeded_session(tmp_path: Path) -> str:
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer("seeded")) as runtime:
        return runtime.run(RuntimeRequest(prompt="seed prompt")).session.session.id


def test_side_question_reuses_context_prefix_zero_tools_one_turn(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    producer = _AnswerProducer()
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=producer) as runtime:
        result = runtime.ask_side_question(session_id=session_id, question="  what did I ask?  ")

    assert result["session_id"] == session_id
    assert result["question"] == "what did I ask?"
    assert result["answer"] == "side answer"
    # Exactly 1 provider turn.
    assert len(producer.requests) == 1
    request = producer.requests[0]
    # Zero tools offered on the forked call.
    assert request.available_tools == ()
    assert request.run_step == 1
    # Same session + shared context prefix: replayed conversation present.
    assert request.session.session_id == session_id
    assert "what did I ask?" in request.prompt
    assert any("seed prompt" in (segment.content or "") for segment in request.assembled_context.segments)


def test_side_question_tool_attempt_maps_to_fallback_and_executes_nothing(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    producer = _ToolAttemptProducer()
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=producer) as runtime:
        result = runtime.ask_side_question(session_id=session_id, question="q")

    assert len(producer.requests) == 1
    assert producer.requests[0].available_tools == ()
    assert "tried to call read" in str(result["answer"])


def test_side_question_leaves_transcript_and_events_untouched(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        before = runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=session_id)
        before_types = [event.event_type for event in before.events]
        before_metadata = dict(before.session.metadata)

        runtime.ask_side_question(session_id=session_id, question="side q")

        after = runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=session_id)
        assert [event.event_type for event in after.events] == before_types
        assert dict(after.session.metadata) == before_metadata
        assert after.session.status == before.session.status
        assert after.session.turn == before.session.turn


def test_side_question_validation(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        with pytest.raises(ValueError, match="must not be empty"):
            runtime.ask_side_question(session_id=session_id, question="   ")
        with pytest.raises(UnknownSessionError):
            runtime.ask_side_question(session_id="does-not-exist", question="q")


def test_side_question_works_while_session_running(tmp_path: Path) -> None:
    """No busy gate: mid-turn availability at the runtime level."""
    from voidcode.runtime.active_session import ACTIVE_SESSION_REGISTRY

    session_id = _seeded_session(tmp_path)
    ACTIVE_SESSION_REGISTRY.register(workspace=tmp_path.resolve(), session_id=session_id, run_id="live-run", metadata={})
    try:
        with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
            result = runtime.ask_side_question(session_id=session_id, question="q")
    finally:
        ACTIVE_SESSION_REGISTRY.unregister(workspace=tmp_path.resolve(), session_id=session_id, run_id="live-run")
    assert result["answer"] == "side answer"
