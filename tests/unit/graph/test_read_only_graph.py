from __future__ import annotations

from dataclasses import replace

from voidcode.core.deterministic_turns import DeterministicTurnProducer
from voidcode.core.transcript import ContextSegment, ToolResultView
from voidcode.core.turns import FinalTurn, ToolTurn, TurnRequest, TurnSessionSnapshot
from voidcode.runtime.context.window import RuntimeAssembledContext
from voidcode.tools.contracts import TextOutput, ToolDefinition, ToolEffect


def _request(prompt: str) -> TurnRequest:
    assembled = RuntimeAssembledContext(
        prompt=prompt,
        tool_results=(),
        continuity_state=None,
        segments=(ContextSegment(role="user", content=prompt),),
        metadata={},
    )
    return TurnRequest(
        session=TurnSessionSnapshot(session_id="turn-session"),
        prompt=prompt,
        assembled_context=assembled,
        available_tools=(
            ToolDefinition(name="read", description="Read file", effects=frozenset({ToolEffect.READ})),
            ToolDefinition(name="grep", description="Grep files", effects=frozenset({ToolEffect.READ})),
            ToolDefinition(name="write", description="Write file", effects=frozenset({ToolEffect.WRITE})),
            ToolDefinition(name="shell_exec", description="Run shell command", effects=frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN})),
        ),
    )


def test_deterministic_producer_selects_read_tool() -> None:
    producer = DeterministicTurnProducer()
    request = _request("read sample.txt")
    plan = producer.produce(request, (), session=request.session)

    assert isinstance(plan, ToolTurn)
    assert len(plan.calls) == 1
    assert plan.calls[0].tool_name == "read"
    assert plan.calls[0].arguments == {"path": "sample.txt"}


def test_turn_run_step_is_a_watermark_not_a_budget() -> None:
    producer = DeterministicTurnProducer()
    prompt = "\n".join(["read sample.txt"] * 100)
    request = replace(_request(prompt), run_step=100)

    plan = producer.produce(request, (), session=request.session)
    assert isinstance(plan, ToolTurn)
    assert plan.calls[0].tool_name == "read"

    finished = producer.produce(
        replace(request, run_step=101),
        (ToolResultView("read-call", "read", {"path": "sample.txt"}, TextOutput("hello"), "ok"),),
        session=request.session,
    )
    assert isinstance(finished, FinalTurn)
    assert finished.output == "hello"
