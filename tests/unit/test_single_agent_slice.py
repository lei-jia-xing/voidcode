from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.graph.contracts import GraphRunRequest
from voidcode.graph.single_agent_slice import ProviderSingleAgentGraph
from voidcode.runtime.model_provider import ModelProviderRegistry, resolve_provider_model
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.single_agent_provider import StubSingleAgentProvider
from voidcode.tools.contracts import ToolDefinition, ToolResult


def _tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(name="read_file", description="read", input_schema={}, read_only=True),
        ToolDefinition(name="write_file", description="write", input_schema={}, read_only=False),
    )


def _session() -> SessionState:
    return SessionState(session=SessionRef(id="s1"), status="running", turn=1, metadata={})


def test_provider_single_agent_graph_requests_tool_on_first_turn() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderSingleAgentGraph(
        provider=StubSingleAgentProvider(name="opencode"),
        provider_model=provider_model,
    )

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.tool_call is not None
    assert step.tool_call.tool_name == "read_file"
    assert step.output is None
    assert step.is_finished is False
    assert [event.event_type for event in step.events] == ["graph.loop_step", "graph.model_turn"]


def test_provider_single_agent_graph_finalizes_after_tool_result() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderSingleAgentGraph(
        provider=StubSingleAgentProvider(name="opencode"),
        provider_model=provider_model,
    )

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
        ),
        tool_results=(
            ToolResult(
                tool_name="read_file",
                content="alpha\n",
                status="ok",
                data={"path": "sample.txt", "content": "alpha\n"},
            ),
        ),
        session=_session(),
    )

    assert step.tool_call is None
    assert step.output == "alpha\n"
    assert step.is_finished is True
    assert [event.event_type for event in step.events] == [
        "graph.loop_step",
        "graph.model_turn",
        "graph.loop_step",
        "graph.response_ready",
    ]
