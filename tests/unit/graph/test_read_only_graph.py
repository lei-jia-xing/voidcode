from __future__ import annotations

from dataclasses import replace

from voidcode.core.deterministic_turns import DeterministicTurnProducer
from voidcode.core.transcript import ContextSegment
from voidcode.core.turns import TurnRequest, TurnSessionSnapshot
from voidcode.runtime.context.window import RuntimeAssembledContext
from voidcode.tools.contracts import ToolDefinition, ToolEffect, ToolResult


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

    assert len(plan.tool_calls) == 1
    assert plan.tool_calls[0].tool_name == "read"
    assert plan.tool_calls[0].arguments == {"path": "sample.txt"}
    assert [fact.kind for fact in plan.facts] == ["loop_step", "model_turn"]


def test_turn_run_step_is_a_watermark_not_a_budget() -> None:
    producer = DeterministicTurnProducer()
    request = replace(_request("read sample.txt"), run_step=100)

    plan = producer.produce(request, (), session=request.session)

    assert plan.tool_calls[0].tool_name == "read"
    assert plan.facts[0].payload == {"step": 100, "phase": "plan"}

    finished = producer.produce(
        replace(request, run_step=101),
        (ToolResult(tool_name="read", status="ok", content="hello", data={}),),
        session=request.session,
    )
    assert finished.is_finished is True
    assert finished.output == "hello"
    assert finished.facts[-2].payload == {"step": 102, "phase": "finalize"}
