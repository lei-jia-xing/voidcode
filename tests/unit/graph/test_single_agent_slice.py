from __future__ import annotations

import json

import pytest

from voidcode.graph.contracts import GraphRunRequest
from voidcode.graph.provider_graph import ProviderGraph
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_model
from voidcode.runtime.context_window import RuntimeContextWindow, RuntimeContinuityState
from voidcode.runtime.provider_protocol import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
    StubTurnProvider,
)
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult


def _tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(name="read_file", description="read", input_schema={}, read_only=True),
        ToolDefinition(name="write_file", description="write", input_schema={}, read_only=False),
    )


def _session() -> SessionState:
    return SessionState(session=SessionRef(id="s1"), status="running", turn=1, metadata={})


class _CapturingTurnProvider:
    name = "opencode"

    def __init__(self) -> None:
        self.requests: list[ProviderTurnRequest] = []

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        self.requests.append(request)
        return ProviderTurnResult(output="done")


class _MixedNonStreamingTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(
            tool_call=ToolCall(tool_name="read_file", arguments={"filePath": "sample.txt"}),
            output="done",
        )


class _MixedStreamingTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="should-not-be-used")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        return iter(
            (
                ProviderStreamEvent(kind="delta", channel="text", text="I will read it."),
                ProviderStreamEvent(
                    kind="content",
                    channel="tool",
                    text='{"tool_name":"read_file","arguments":{"path":"sample.txt"}}',
                ),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


class _EmptyNonStreamingTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult()


class _StreamOutputTurnProvider:
    name = "opencode"
    stream_calls: int
    propose_calls: int

    def __init__(self) -> None:
        self.stream_calls = 0
        self.propose_calls = 0

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        self.propose_calls += 1
        return ProviderTurnResult(output="stream-final")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        self.stream_calls += 1
        return iter(
            (
                ProviderStreamEvent(kind="delta", channel="text", text="stream-"),
                ProviderStreamEvent(kind="delta", channel="text", text="final"),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


class _StreamErrorTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="fallback")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        return iter(
            (
                ProviderStreamEvent(
                    kind="error",
                    channel="error",
                    error="network interrupted",
                    error_kind="transient_failure",
                ),
                ProviderStreamEvent(kind="done", done_reason="error"),
            )
        )


class _StreamNoTextDoneTurnProvider:
    name = "opencode"

    def __init__(self) -> None:
        self.stream_calls = 0
        self.propose_calls = 0

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        self.propose_calls += 1
        return ProviderTurnResult(output="should-not-be-used")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        self.stream_calls += 1
        return iter((ProviderStreamEvent(kind="done", done_reason="completed"),))


class _StreamToolTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="should-not-be-used")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        return iter(
            (
                ProviderStreamEvent(
                    kind="content",
                    channel="tool",
                    text='{"tool_name":"read_file","arguments":{"path":"sample.txt"}}',
                ),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


class _StreamChunkedToolTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="should-not-be-used")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        return iter(
            (
                ProviderStreamEvent(
                    kind="content",
                    channel="tool",
                    text='{"tool_name":"read_file",',
                ),
                ProviderStreamEvent(
                    kind="content",
                    channel="tool",
                    text='"arguments":{"path":"sample.txt"}}',
                ),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


class _StreamToolSnapshotTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="should-not-be-used")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        return iter(
            (
                ProviderStreamEvent(
                    kind="content",
                    channel="tool",
                    text='{"tool_name":"read_file","arguments":{}}',
                ),
                ProviderStreamEvent(
                    kind="content",
                    channel="tool",
                    text='{"tool_name":"read_file","arguments":{"path":"sample.txt"}}',
                ),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


class _StreamMalformedToolTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="should-not-be-used")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        return iter(
            (
                ProviderStreamEvent(kind="content", channel="tool", text='{"tool_name":'),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


class _StreamMissingDoneTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="should-not-be-used")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        return iter((ProviderStreamEvent(kind="delta", channel="text", text="partial"),))


class _StreamMixedTerminalTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="should-not-be-used")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        return iter(
            (
                ProviderStreamEvent(kind="delta", channel="text", text="hello"),
                ProviderStreamEvent(
                    kind="content",
                    channel="tool",
                    text='{"tool_name":"read_file","arguments":{"path":"sample.txt"}}',
                ),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


def test_provider_provider_graph_requests_tool_on_first_turn() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=StubTurnProvider(name="opencode"),
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
    assert step.events[0].payload == {"step": 1, "phase": "plan", "max_steps": None}
    assert step.events[1].payload == {
        "turn": 1,
        "mode": "provider",
        "provider": "opencode",
        "model": "gpt-5.4",
        "attempt": 0,
        "streaming": False,
        "prompt": "read sample.txt",
    }


def test_provider_provider_graph_finalizes_after_tool_result() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=StubTurnProvider(name="opencode"),
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
    assert step.events[0].payload == {"step": 2, "phase": "plan", "max_steps": None}
    assert step.events[1].payload == {
        "turn": 2,
        "mode": "provider",
        "provider": "opencode",
        "model": "gpt-5.4",
        "attempt": 0,
        "streaming": False,
        "prompt": "read sample.txt",
    }
    assert step.events[2].payload == {"step": 3, "phase": "finalize", "max_steps": None}
    assert step.events[3].payload == {"output_preview": "alpha\n"}


def test_provider_provider_graph_prefers_nonstream_tool_call_over_text() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_MixedNonStreamingTurnProvider(),
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


def test_provider_provider_graph_rejects_nonstream_missing_terminal_outcome() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_EmptyNonStreamingTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(
        ProviderExecutionError,
        match="neither output nor a tool call",
    ) as exc_info:
        _ = graph.step(
            request=GraphRunRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.kind == "transient_failure"


def test_provider_provider_graph_preserves_stream_error_details() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    class _DetailedStreamErrorTurnProvider:
        name = "opencode"

        def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            _ = request
            return ProviderTurnResult(output="fallback")

        def stream_turn(self, request: ProviderTurnRequest):
            _ = request
            error_payload = json.dumps(
                {
                    "message": "network interrupted",
                    "status_code": 429,
                    "code": "rate_limit_exceeded",
                    "prompt": "secret",
                }
            )
            return iter(
                (
                    ProviderStreamEvent(
                        kind="error",
                        channel="error",
                        error=error_payload,
                        error_kind="transient_failure",
                    ),
                )
            )

    graph = ProviderGraph(
        provider=_DetailedStreamErrorTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(ProviderExecutionError, match="network interrupted") as exc_info:
        _ = graph.step(
            request=GraphRunRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.details == {
        "message": "network interrupted",
        "status_code": 429,
        "code": "rate_limit_exceeded",
        "prompt": "secret",
        "source": "stream",
        "error_code": "rate_limit_exceeded",
    }
    assert exc_info.value.kind == "rate_limit"


def test_provider_provider_graph_prefers_parsed_stream_error_kind_over_generic_transient() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    class _ContextLimitStreamErrorTurnProvider:
        name = "opencode"

        def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            _ = request
            return ProviderTurnResult(output="fallback")

        def stream_turn(self, request: ProviderTurnRequest):
            _ = request
            error_payload = json.dumps(
                {
                    "message": "prompt exceeds the context window",
                    "status_code": 413,
                    "code": "context_length_exceeded",
                }
            )
            return iter(
                (
                    ProviderStreamEvent(
                        kind="error",
                        channel="error",
                        error=error_payload,
                        error_kind="transient_failure",
                    ),
                )
            )

    graph = ProviderGraph(
        provider=_ContextLimitStreamErrorTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(
        ProviderExecutionError,
        match="prompt exceeds the context window",
    ) as exc_info:
        _ = graph.step(
            request=GraphRunRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.kind == "context_limit"


def test_provider_provider_graph_passes_applied_skill_context_to_provider() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    provider = _CapturingTurnProvider()
    graph = ProviderGraph(provider=provider, provider_model=provider_model)

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
                    "prompt_context": (
                        "Skill: summarize\n"
                        "Description: Summarize selected files.\n"
                        "Instructions:\n# Summarize\nUse concise bullet points."
                    ),
                },
            ),
            skill_prompt_context="Runtime-managed skills are active.",
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
            "prompt_context": (
                "Skill: summarize\n"
                "Description: Summarize selected files.\n"
                "Instructions:\n# Summarize\nUse concise bullet points."
            ),
        },
    )
    assert provider.requests[0].skill_prompt_context == "Runtime-managed skills are active."
    assert provider.requests[0].context_window.prompt == "read sample.txt"


def test_provider_provider_graph_forwards_agent_preset_to_provider() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    provider = _CapturingTurnProvider()
    graph = ProviderGraph(provider=provider, provider_model=provider_model)

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            metadata={
                "agent_preset": {
                    "preset": "leader",
                    "prompt_profile": "leader",
                    "model": "opencode/gpt-5.4",
                    "execution_engine": "provider",
                }
            },
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.output == "done"
    assert provider.requests[0].agent_preset == {
        "preset": "leader",
        "prompt_profile": "leader",
        "model": "opencode/gpt-5.4",
        "execution_engine": "provider",
    }


def test_provider_provider_graph_forwards_bounded_context_window_to_provider() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    provider = _CapturingTurnProvider()
    graph = ProviderGraph(provider=provider, provider_model=provider_model)

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
        continuity_state=RuntimeContinuityState(
            summary_text=(
                "Compacted 2 earlier tool results:\n"
                '1. read_file ok path=sample.txt content_preview="old\\n"\n'
                '2. read_file ok path=sample.txt content_preview="older\\n"'
            ),
            dropped_tool_result_count=2,
            retained_tool_result_count=1,
            source="tool_result_window",
        ),
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
    assert provider.requests[0].context_window.continuity_state == RuntimeContinuityState(
        summary_text=(
            "Compacted 2 earlier tool results:\n"
            '1. read_file ok path=sample.txt content_preview="old\\n"\n'
            '2. read_file ok path=sample.txt content_preview="older\\n"'
        ),
        dropped_tool_result_count=2,
        retained_tool_result_count=1,
        source="tool_result_window",
    )


def test_provider_graph_forwards_explicit_lsp_tool_feedback_to_provider() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    provider = _CapturingTurnProvider()
    graph = ProviderGraph(provider=provider, provider_model=provider_model)
    lsp_result = ToolResult(
        tool_name="lsp",
        status="ok",
        content="definition found",
        data={"operation": "definition", "response": {"uri": "file:///workspace/main.py"}},
    )

    _ = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="use lsp feedback",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(
                prompt="use lsp feedback",
                tool_results=(lsp_result,),
            ),
        ),
        tool_results=(lsp_result,),
        session=_session(),
    )

    assert provider.requests[0].tool_results == (lsp_result,)
    assert provider.requests[0].context_window.tool_results == (lsp_result,)


def test_provider_graph_forwards_mcp_tool_feedback_to_provider() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    provider = _CapturingTurnProvider()
    graph = ProviderGraph(provider=provider, provider_model=provider_model)
    mcp_result = ToolResult(
        tool_name="mcp/echo/echo",
        status="ok",
        content="echo: hello",
        data={"server": "echo", "tool": "echo", "is_error": False},
    )

    _ = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="use mcp feedback",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(
                prompt="use mcp feedback",
                tool_results=(mcp_result,),
            ),
        ),
        tool_results=(mcp_result,),
        session=_session(),
    )

    assert provider.requests[0].tool_results == (mcp_result,)
    assert provider.requests[0].context_window.tool_results == (mcp_result,)


def test_provider_provider_graph_enforces_configured_max_steps() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=StubTurnProvider(name="opencode"),
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


def test_provider_provider_graph_streams_ordered_events_and_deterministic_output() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_StreamOutputTurnProvider(),
        provider_model=provider_model,
    )

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.is_finished is True
    assert step.output == "stream-final"
    stream_events = [event for event in step.events if event.event_type == "graph.provider_stream"]
    assert [event.payload["kind"] for event in stream_events] == ["delta", "delta", "done"]
    model_turn_events = [event for event in step.events if event.event_type == "graph.model_turn"]
    assert model_turn_events
    assert model_turn_events[0].payload["streaming"] is True


def test_provider_provider_graph_stream_done_without_text_does_not_fallback_propose_turn() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    provider = _StreamNoTextDoneTurnProvider()
    graph = ProviderGraph(provider=provider, provider_model=provider_model)

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.is_finished is True
    assert step.output == ""
    assert provider.stream_calls == 1
    assert provider.propose_calls == 0


def test_provider_provider_graph_returns_streamed_tool_call() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_StreamToolTurnProvider(),
        provider_model=provider_model,
    )

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.is_finished is False
    assert step.output is None
    assert step.tool_call is not None
    assert step.tool_call.tool_name == "read_file"
    assert step.tool_call.arguments == {"path": "sample.txt"}


def test_provider_provider_graph_prefers_streamed_tool_call_over_text() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_MixedStreamingTurnProvider(),
        provider_model=provider_model,
    )

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.tool_call is not None
    assert step.tool_call.tool_name == "read_file"
    assert step.output is None


def test_provider_provider_graph_reconstructs_chunked_streamed_tool_call() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_StreamChunkedToolTurnProvider(),
        provider_model=provider_model,
    )

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.is_finished is False
    assert step.output is None
    assert step.tool_call is not None
    assert step.tool_call.tool_name == "read_file"
    assert step.tool_call.arguments == {"path": "sample.txt"}


def test_provider_provider_graph_uses_latest_complete_tool_snapshot() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_StreamToolSnapshotTurnProvider(),
        provider_model=provider_model,
    )

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.is_finished is False
    assert step.output is None
    assert step.tool_call is not None
    assert step.tool_call.tool_name == "read_file"
    assert step.tool_call.arguments == {"path": "sample.txt"}


def test_provider_provider_graph_rejects_malformed_streamed_tool_payload() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_StreamMalformedToolTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(ProviderExecutionError, match="malformed tool payload") as exc_info:
        _ = graph.step(
            request=GraphRunRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.kind == "transient_failure"


def test_provider_provider_graph_requires_done_event_for_stream_completion() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_StreamMissingDoneTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(ProviderExecutionError, match="without a done event") as exc_info:
        _ = graph.step(
            request=GraphRunRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.kind == "transient_failure"


def test_provider_provider_graph_prefers_mixed_stream_tool_terminal_output() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_StreamMixedTerminalTurnProvider(),
        provider_model=provider_model,
    )

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.tool_call is not None
    assert step.tool_call.tool_name == "read_file"
    assert step.output is None


def test_provider_provider_graph_stream_error_maps_to_provider_execution_error() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_StreamErrorTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(ProviderExecutionError, match="network interrupted"):
        _ = graph.step(
            request=GraphRunRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )
