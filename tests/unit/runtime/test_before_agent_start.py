from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.questions import QuestionResponse
from voidcode.core.turns import FinalTurn, ModelTurnFact, ToolTurn, TurnRequest, TurnSession
from voidcode.hook.before_agent_start import (
    BeforeAgentStartBinding,
    BeforeAgentStartBlock,
    BeforeAgentStartDeclaration,
    BeforeAgentStartRegistry,
    BeforeAgentStartRequest,
    BeforeAgentStartTransform,
)
from voidcode.runtime.config.loading import RuntimeConfig
from voidcode.runtime.permissions.policy import PermissionPolicy
from voidcode.runtime.service import RuntimeRequest, VoidCodeRuntime
from voidcode.runtime.tools.registry import ToolRegistry
from voidcode.tools.contracts import ToolCall
from voidcode.tools.question import QuestionTool


class _ApprovalThenFinish:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        self.prompts.append(request.prompt)
        fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
        if request.prompt == "fresh input prepared":
            return FinalTurn(output="fresh turn", facts=(fact,))
        if not tool_results:
            return ToolTurn(
                calls=(ToolCall(tool_name="write", arguments={"path": "approved.txt", "content": "ok"}),),
                facts=(fact,),
            )
        return FinalTurn(output="approved", facts=(fact,))


def _registry(calls: list[tuple[str, str]]) -> BeforeAgentStartRegistry:
    declaration = BeforeAgentStartDeclaration(name="test-rewrite", version="1", priority=0)

    def rewrite(request: BeforeAgentStartRequest) -> BeforeAgentStartTransform:
        calls.append((request.original_prompt, request.effective_prompt))
        return BeforeAgentStartTransform(prompt=f"{request.effective_prompt} prepared")

    return BeforeAgentStartRegistry(
        bindings=(BeforeAgentStartBinding(declaration=declaration, handler=rewrite),),
    )


class _Finish:
    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> FinalTurn:
        return FinalTurn(output="done", facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),))


def test_no_handlers_leave_event_and_session_metadata_unchanged(tmp_path: Path) -> None:
    with VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=_Finish(),
        config=RuntimeConfig(),
    ) as runtime:
        response = runtime.run(RuntimeRequest(prompt="ordinary request", session_id="no-hook"))

    assert response.session.status == "completed"
    assert "before_agent_start" not in response.session.metadata
    assert all(event.event_type != "runtime.before_agent_start" for event in response.events)


class _NeverCalled:
    def __init__(self) -> None:
        self.calls = 0

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> FinalTurn:
        self.calls += 1
        return FinalTurn(output="must not run", facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),))


@pytest.mark.parametrize("failure_kind", ["blocked", "error"])
def test_closing_failed_hook_stream_leaves_durable_terminal_result(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    handler_calls = 0
    producer = _NeverCalled()
    declaration = BeforeAgentStartDeclaration(name="test-failure", version="1")

    def fail(request: BeforeAgentStartRequest) -> BeforeAgentStartBlock | BeforeAgentStartTransform:
        nonlocal handler_calls
        handler_calls += 1
        if failure_kind == "blocked":
            return BeforeAgentStartBlock(reason="policy rejected request")
        raise RuntimeError("hook failed")

    def registry() -> BeforeAgentStartRegistry:
        return BeforeAgentStartRegistry(
            bindings=(BeforeAgentStartBinding(declaration=declaration, handler=fail),),
        )

    session_id = f"before-agent-start-{failure_kind}"
    config = RuntimeConfig()
    with VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=producer,
        config=config,
        before_agent_start_registry=registry(),
    ) as runtime:
        stream = runtime.run_stream(RuntimeRequest(prompt="blocked request", session_id=session_id))
        hook_chunk = next(chunk for chunk in stream if chunk.event is not None and chunk.event.event_type == "runtime.before_agent_start")
        assert hook_chunk.event is not None
        assert hook_chunk.event.payload["status"] == ("blocked" if failure_kind == "blocked" else "failed")
        stream.close()
        stored = runtime.replay_session(session_id=session_id)

    with VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=producer,
        config=config,
        before_agent_start_registry=registry(),
    ) as resumed_runtime:
        resumed = resumed_runtime.resume(session_id)
        events = resumed_runtime.replay_session(session_id=session_id).events

    assert stored.session.status == "failed"
    assert resumed.session.status == "failed"
    assert [event.event_type for event in stored.events][-2:] == ["runtime.before_agent_start", "runtime.failed"]
    assert sum(event.event_type == "runtime.before_agent_start" for event in events) == 1
    assert sum(event.event_type == "runtime.failed" for event in events) == 1
    assert handler_calls == 1
    assert producer.calls == 0


class _WriteThenFinish:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        self.prompts.append(request.prompt)
        fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
        if tool_results:
            return FinalTurn(output="interrupted run resumed", facts=(fact,))
        return ToolTurn(
            calls=(ToolCall(tool_name="write", arguments={"path": "completed.txt", "content": "write happened once"}),),
            facts=(fact,),
        )


def test_hook_is_not_repeated_during_cold_interrupted_resume(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    producer = _WriteThenFinish()
    session_id = "before-agent-start-interrupted-resume"
    with VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=producer,
        config=RuntimeConfig(approval_mode="yolo"),
        before_agent_start_registry=_registry(calls),
    ) as runtime:
        stream = runtime.run_stream(RuntimeRequest(prompt="interrupted input", session_id=session_id))
        completed_tool = next(
            chunk
            for chunk in stream
            if chunk.event is not None and chunk.event.event_type == "runtime.tool_completed" and chunk.event.payload.get("tool") == "write"
        )
        assert completed_tool.event is not None
        stream.close()

    assert (tmp_path / "completed.txt").read_text(encoding="utf-8") == "write happened once"
    with VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=producer,
        config=RuntimeConfig(approval_mode="yolo"),
        before_agent_start_registry=_registry(calls),
    ) as resumed_runtime:
        resumed = resumed_runtime.resume(session_id)
        events = resumed_runtime.replay_session(session_id=session_id).events

    assert resumed.session.status == "completed"
    assert resumed.output == "interrupted run resumed"
    assert calls == [("interrupted input", "interrupted input")]
    assert producer.prompts == ["interrupted input prepared", "interrupted input prepared"]
    assert sum(event.event_type == "runtime.before_agent_start" for event in events) == 1
    assert sum(event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "write" for event in events) == 1


class _QuestionThenFinish:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        self.prompts.append(request.prompt)
        fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
        if tool_results:
            return FinalTurn(output="question answered", facts=(fact,))
        return ToolTurn(
            calls=(
                ToolCall(
                    tool_name="question",
                    arguments={
                        "questions": [
                            {
                                "question": "Continue?",
                                "header": "Continue",
                                "options": [],
                                "multiple": False,
                                "allow_freeform": True,
                            }
                        ]
                    },
                ),
            ),
            facts=(fact,),
        )


def test_hook_is_not_repeated_during_cold_question_resume(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    producer = _QuestionThenFinish()
    config = RuntimeConfig(approval_mode="ask")
    session_id = "before-agent-start-question-resume"
    tool_registry = ToolRegistry.from_tools((QuestionTool(),))
    with VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=tool_registry,
        turn_producer=producer,
        config=config,
        before_agent_start_registry=_registry(calls),
    ) as runtime:
        waiting = runtime.run(RuntimeRequest(prompt="question input", session_id=session_id))
        question_id = next(str(event.payload["request_id"]) for event in waiting.events if event.event_type == "runtime.question_requested")
        assert waiting.session.status == "waiting"

    with VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=tool_registry,
        turn_producer=producer,
        config=config,
        before_agent_start_registry=_registry(calls),
    ) as resumed_runtime:
        resumed = resumed_runtime.answer_question(
            session_id,
            question_request_id=question_id,
            responses=(QuestionResponse(header="Continue", answers=("yes",)),),
        )
        events = resumed_runtime.replay_session(session_id=session_id).events

    assert resumed.session.status == "completed"
    assert resumed.output == "question answered"
    assert calls == [("question input", "question input")]
    assert producer.prompts == ["question input prepared", "question input prepared"]
    assert sum(event.event_type == "runtime.before_agent_start" for event in events) == 1


def test_hook_runs_per_new_turn_but_not_during_cold_approval_resume(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    producer = _ApprovalThenFinish()
    config = RuntimeConfig(approval_mode="ask")
    session_id = "before-agent-start-resume"
    with VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=producer,
        config=config,
        permission_policy=PermissionPolicy(mode="ask"),
        before_agent_start_registry=_registry(calls),
    ) as runtime:
        waiting = runtime.run(RuntimeRequest(prompt="approval input", session_id=session_id))
        approval_id = next(str(event.payload["request_id"]) for event in waiting.events if event.event_type == "runtime.approval_requested")
        assert calls == [("approval input", "approval input")]
        assert producer.prompts == ["approval input prepared"]

    # A newly constructed runtime exercises the persisted approval continuation.
    with VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=producer,
        config=config,
        permission_policy=PermissionPolicy(mode="ask"),
        before_agent_start_registry=_registry(calls),
    ) as resumed_runtime:
        resumed = resumed_runtime.resume(
            session_id,
            approval_request_id=approval_id,
            approval_decision="allow",
        )
        assert resumed.session.status == "completed"
        assert calls == [("approval input", "approval input")]
        assert producer.prompts[-1] == "approval input prepared"
        events = resumed_runtime.replay_session(session_id=session_id).events
        assert sum(event.event_type == "runtime.before_agent_start" for event in events) == 1

    # A fresh request in the same session is a new logical turn and gets one callback.
    with VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=producer,
        config=config,
        permission_policy=PermissionPolicy(mode="ask"),
        before_agent_start_registry=_registry(calls),
    ) as next_turn_runtime:
        fresh = next_turn_runtime.run(RuntimeRequest(prompt="fresh input", session_id=session_id))
        assert fresh.session.status == "completed"
        assert calls == [
            ("approval input", "approval input"),
            ("fresh input", "fresh input"),
        ]
        assert producer.prompts[-1] == "fresh input prepared"
        events = next_turn_runtime.replay_session(session_id=session_id).events
        assert sum(event.event_type == "runtime.before_agent_start" for event in events) == 2


def test_trace_bounds_long_handler_metadata_without_prompt_text() -> None:
    def unchanged(request: BeforeAgentStartRequest) -> BeforeAgentStartTransform:
        return BeforeAgentStartTransform(prompt=request.effective_prompt)

    declarations = tuple(
        BeforeAgentStartDeclaration(
            name=f"handler-{index}-" + ("x" * 300),
            version="v" * 300,
            priority=index,
        )
        for index in range(33)
    )
    registry = BeforeAgentStartRegistry(
        bindings=tuple(BeforeAgentStartBinding(declaration=item, handler=unchanged) for item in declarations),
    )
    prompt = "never-in-event: sensitive prompt"
    trace = registry.apply(BeforeAgentStartRequest(original_prompt=prompt, effective_prompt=prompt)).trace_payload()

    handlers = trace["handlers"]
    assert isinstance(handlers, list)
    assert len(handlers) == 32
    assert trace["omitted_handler_count"] == 1
    assert all(len(item["name"]) <= 240 and len(item["version"]) <= 240 for item in handlers)
    assert prompt not in str(trace)
    assert declarations[0].name in registry.declarations
