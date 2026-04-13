from __future__ import annotations

import pytest

from voidcode.graph.contracts import GraphRunRequest
from voidcode.graph.single_agent_slice import ProviderSingleAgentGraph
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_model
from voidcode.runtime.context_window import RuntimeContextWindow
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.single_agent_provider import (
    SingleAgentTurnRequest,
    SingleAgentTurnResult,
    StubSingleAgentProvider,
)
from voidcode.tools.contracts import ToolDefinition, ToolResult


def _tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(name="read_file", description="read", input_schema={}, read_only=True),
        ToolDefinition(name="write_file", description="write", input_schema={}, read_only=False),
    )


def _session() -> SessionState:
    return SessionState(session=SessionRef(id="s1"), status="running", turn=1, metadata={})


class _CapturingSingleAgentProvider:
    name = "opencode"

    def __init__(self) -> None:
        self.requests: list[SingleAgentTurnRequest] = []

    def propose_turn(self, request: SingleAgentTurnRequest) -> SingleAgentTurnResult:
        self.requests.append(request)
        return SingleAgentTurnResult(output="done")


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
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.tool_call is not None
    assert step.tool_call.tool_name == "read_file"
    assert step.output is None
    assert step.is_finished is False
    assert [event.event_type for event in step.events] == ["graph.loop_step", "graph.model_turn"]
    assert step.events[0].payload == {"step": 1, "phase": "plan", "max_steps": 4}
    assert step.events[1].payload == {
        "turn": 1,
        "mode": "single_agent",
        "provider": "opencode",
        "model": "gpt-5.4",
        "attempt": 0,
        "prompt": "read sample.txt",
    }


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
            context_window=RuntimeContextWindow(
                prompt="read sample.txt",
                tool_results=(
                    ToolResult(
                        tool_name="read_file",
                        content="alpha\n",
                        status="ok",
                        data={"path": "sample.txt", "content": "alpha\n"},
                    ),
                ),
            ),
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
    assert step.events[0].payload == {"step": 2, "phase": "plan", "max_steps": 4}
    assert step.events[1].payload == {
        "turn": 2,
        "mode": "single_agent",
        "provider": "opencode",
        "model": "gpt-5.4",
        "attempt": 0,
        "prompt": "read sample.txt",
    }
    assert step.events[2].payload == {"step": 3, "phase": "finalize", "max_steps": 4}
    assert step.events[3].payload == {"output_preview": "alpha\n"}


def test_provider_single_agent_graph_passes_applied_skill_context_to_provider() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    provider = _CapturingSingleAgentProvider()
    graph = ProviderSingleAgentGraph(provider=provider, provider_model=provider_model)

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            applied_skills=(
                {
                    "name": "summarize",
                    "description": "Summarize selected files.",
                    "content": "# Summarize\nUse concise bullet points.",
                },
            ),
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.output == "done"
    assert provider.requests[0].applied_skills == (
        {
            "name": "summarize",
            "description": "Summarize selected files.",
            "content": "# Summarize\nUse concise bullet points.",
        },
    )
    assert provider.requests[0].context_window.prompt == "read sample.txt"


def test_provider_single_agent_graph_forwards_bounded_context_window_to_provider() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    provider = _CapturingSingleAgentProvider()
    graph = ProviderSingleAgentGraph(provider=provider, provider_model=provider_model)

    bounded_context = RuntimeContextWindow(
        prompt="read sample.txt",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                content="new\n",
                status="ok",
                data={"path": "sample.txt", "content": "new\n"},
            ),
        ),
        compacted=True,
        compaction_reason="tool_result_window",
        original_tool_result_count=3,
        retained_tool_result_count=1,
    )

    _ = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=bounded_context,
        ),
        tool_results=(
            ToolResult(
                tool_name="read_file",
                content="old\n",
                status="ok",
                data={"path": "sample.txt", "content": "old\n"},
            ),
        ),
        session=_session(),
    )

    assert provider.requests[0].context_window is bounded_context
    assert provider.requests[0].context_window.compacted is True
    assert provider.requests[0].context_window.retained_tool_result_count == 1


def test_provider_single_agent_graph_enforces_configured_max_steps() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderSingleAgentGraph(
        provider=StubSingleAgentProvider(name="opencode"),
        provider_model=provider_model,
        max_steps=1,
    )

    with pytest.raises(ValueError, match="graph exceeded max steps: 1"):
        _ = graph.step(
            request=GraphRunRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
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
