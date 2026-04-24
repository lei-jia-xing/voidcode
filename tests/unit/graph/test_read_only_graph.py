from __future__ import annotations

import pytest

from voidcode.graph import GraphRunRequest
from voidcode.graph.deterministic_graph import DeterministicGraph
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.tools.contracts import ToolDefinition, ToolResult


def _request(prompt: str) -> GraphRunRequest:
    return GraphRunRequest(
        session=SessionState(session=SessionRef(id="graph-session"), status="running", turn=1),
        prompt=prompt,
        available_tools=(
            ToolDefinition(name="read_file", description="Read file", read_only=True),
            ToolDefinition(name="grep", description="Grep files", read_only=True),
            ToolDefinition(name="write_file", description="Write file", read_only=False),
            ToolDefinition(name="shell_exec", description="Run shell command", read_only=False),
        ),
    )


def test_graph_direct_import_and_step_work_without_runtime_cycle() -> None:
    graph = DeterministicGraph()
    request = _request("read sample.txt")

    step = graph.step(request, (), session=request.session)

    assert step.tool_call is not None
    assert step.tool_call.tool_name == "read_file"
    assert step.tool_call.arguments == {"filePath": "sample.txt"}
    assert [event.event_type for event in step.events] == [
        "graph.loop_step",
        "graph.model_turn",
    ]


def test_graph_max_step_guard_is_verifiable_via_step() -> None:
    graph = DeterministicGraph(max_steps=1)
    request = _request("read sample.txt")

    with pytest.raises(ValueError, match="graph exceeded max steps: 1"):
        _ = graph.step(
            request,
            (ToolResult(tool_name="read_file", status="ok", content="hello", data={}),),
            session=request.session,
        )
