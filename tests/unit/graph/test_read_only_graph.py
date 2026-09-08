from __future__ import annotations

from dataclasses import replace

from voidcode.graph import GraphRunRequest
from voidcode.graph.contracts import GraphSessionSnapshot
from voidcode.graph.deterministic_graph import DeterministicGraph
from voidcode.runtime.context.window import RuntimeAssembledContext, RuntimeContextSegment
from voidcode.tools.contracts import ToolDefinition, ToolResult


def _request(prompt: str) -> GraphRunRequest:
    assembled = RuntimeAssembledContext(
        prompt=prompt,
        tool_results=(),
        continuity_state=None,
        segments=(RuntimeContextSegment(role="user", content=prompt),),
        metadata={},
    )
    return GraphRunRequest(
        session=GraphSessionSnapshot(session_id="graph-session"),
        prompt=prompt,
        assembled_context=assembled,
        available_tools=(
            ToolDefinition(name="read", description="Read file", read_only=True),
            ToolDefinition(name="grep", description="Grep files", read_only=True),
            ToolDefinition(name="write", description="Write file", read_only=False),
            ToolDefinition(name="shell_exec", description="Run shell command", read_only=False),
        ),
    )


def test_graph_direct_import_and_step_work_without_runtime_cycle() -> None:
    graph = DeterministicGraph()
    request = _request("read sample.txt")

    step = graph.step(request, (), session=request.session)

    assert step.tool_call is not None
    assert step.tool_call.tool_name == "read"
    assert step.tool_call.arguments == {"path": "sample.txt"}
    assert [event.event_type for event in step.events] == [
        "graph.loop_step",
        "graph.model_turn",
    ]


def test_graph_run_step_is_a_watermark_not_a_budget() -> None:
    graph = DeterministicGraph()
    request = replace(_request("read sample.txt"), run_step=100)

    step = graph.step(request, (), session=request.session)

    assert step.tool_call is not None
    assert step.events[0].payload == {"step": 100, "phase": "plan"}

    finished = graph.step(
        replace(request, run_step=101),
        (ToolResult(tool_name="read", status="ok", content="hello", data={}),),
        session=request.session,
    )
    assert finished.is_finished is True
    assert finished.output == "hello"
    assert finished.events[-2].payload == {"step": 102, "phase": "finalize"}
