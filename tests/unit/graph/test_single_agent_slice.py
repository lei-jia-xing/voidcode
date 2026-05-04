from __future__ import annotations

import json

import pytest

from voidcode.graph.contracts import GraphRunRequest
from voidcode.graph.provider_graph import ProviderGraph
from voidcode.provider.protocol import ProviderErrorKind
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_model
from voidcode.runtime.context_window import (
    RuntimeAssembledContext,
    RuntimeContextSegment,
    RuntimeContextWindow,
    RuntimeContinuityState,
)
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


def _session(session_id: str = "s1") -> SessionState:
    return SessionState(session=SessionRef(id=session_id), status="running", turn=1, metadata={})


def _session_with_run(session_id: str = "s1", run_id: str = "run-one") -> SessionState:
    return SessionState(
        session=SessionRef(id=session_id),
        status="running",
        turn=1,
        metadata={"runtime_state": {"run_id": run_id}},
    )


def _assembled_from_context_window(context_window: RuntimeContextWindow) -> RuntimeAssembledContext:
    segments: list[RuntimeContextSegment] = [
        RuntimeContextSegment(role="user", content=context_window.prompt)
    ]
    for index, result in enumerate(context_window.tool_results, start=1):
        tool_call_id = f"test_tool_{index}"
        segments.append(
            RuntimeContextSegment(
                role="assistant",
                content=None,
                tool_call_id=tool_call_id,
                tool_name=result.tool_name,
                tool_arguments={},
            )
        )
        segments.append(
            RuntimeContextSegment(
                role="tool",
                content=result.content or "",
                tool_call_id=tool_call_id,
                tool_name=result.tool_name,
                metadata={
                    "status": result.status,
                    "error": result.error,
                    "data": result.data,
                    "truncated": result.truncated,
                    "partial": result.partial,
                    "reference": result.reference,
                },
            )
        )
    return RuntimeAssembledContext(
        prompt=context_window.prompt,
        tool_results=context_window.tool_results,
        continuity_state=context_window.continuity_state,
        segments=tuple(segments),
        metadata=context_window.metadata_payload(),
    )


class _CapturingTurnProvider:
    name = "opencode"

    def __init__(self) -> None:
        self.requests: list[ProviderTurnRequest] = []
        self.abort_cancelled_values: list[bool | None] = []

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        self.requests.append(request)
        self.abort_cancelled_values.append(
            request.abort_signal.cancelled if request.abort_signal is not None else None
        )
        return ProviderTurnResult(output="done")


class _AbortSignal:
    def __init__(self, *, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class _MixedNonStreamingTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(
            tool_call=ToolCall(tool_name="read_file", arguments={"filePath": "sample.txt"}),
            output="done",
        )


class _BatchNonStreamingTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(
            tool_calls=(
                ToolCall(
                    tool_name="read_file",
                    arguments={"filePath": "alpha.txt"},
                    tool_call_id="call-alpha",
                ),
                ToolCall(
                    tool_name="read_file",
                    arguments={"filePath": "beta.txt"},
                    tool_call_id="call-beta",
                ),
            )
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


class _StreamReasoningMetadataTurnProvider:
    name = "opencode"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="should-not-be-used")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        return iter(
            (
                ProviderStreamEvent(
                    kind="delta",
                    channel="reasoning",
                    text="private chain",
                    metadata={"source": "fixture"},
                ),
                ProviderStreamEvent(kind="delta", channel="text", text="answer"),
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


class _StreamToolBatchTurnProvider:
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
                    text=(
                        '{"tool_calls":['
                        '{"tool_name":"read_file","tool_call_id":"call-alpha",'
                        '"arguments":{"filePath":"alpha.txt"}},'
                        '{"tool_name":"read_file","tool_call_id":"call-beta",'
                        '"arguments":{"filePath":"beta.txt"}}]}'
                    ),
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

    request_context = RuntimeContextWindow(prompt="read sample.txt")
    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=request_context,
            assembled_context=_assembled_from_context_window(request_context),
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


def test_provider_graph_queues_non_streaming_tool_call_batch() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(provider=_BatchNonStreamingTurnProvider(), provider_model=provider_model)
    request_context = RuntimeContextWindow(prompt="read two files")
    request = GraphRunRequest(
        session=_session(),
        prompt="read two files",
        available_tools=_tool_definitions(),
        context_window=request_context,
        assembled_context=_assembled_from_context_window(request_context),
    )

    first_step = graph.step(request=request, tool_results=(), session=_session())
    second_step = graph.step(
        request=request,
        tool_results=(ToolResult(tool_name="read_file", status="ok", content="alpha"),),
        session=_session(),
    )

    assert first_step.tool_call is not None
    assert first_step.tool_call.tool_call_id == "call-alpha"
    assert len(first_step.tool_calls) == 2
    assert second_step.tool_call is not None
    assert second_step.tool_call.tool_call_id == "call-beta"
    assert second_step.events == ()


def test_provider_graph_discards_queued_tool_call_batch_for_different_session() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(provider=_BatchNonStreamingTurnProvider(), provider_model=provider_model)
    first_context = RuntimeContextWindow(prompt="read two files")
    first_request = GraphRunRequest(
        session=_session("session-one"),
        prompt="read two files",
        available_tools=_tool_definitions(),
        context_window=first_context,
        assembled_context=_assembled_from_context_window(first_context),
    )

    first_step = graph.step(request=first_request, tool_results=(), session=_session("session-one"))

    second_context = RuntimeContextWindow(prompt="unrelated session")
    second_request = GraphRunRequest(
        session=_session("session-two"),
        prompt="unrelated session",
        available_tools=_tool_definitions(),
        context_window=second_context,
        assembled_context=_assembled_from_context_window(second_context),
    )
    second_step = graph.step(
        request=second_request,
        tool_results=(),
        session=_session("session-two"),
    )

    assert first_step.tool_call is not None
    assert first_step.tool_call.tool_call_id == "call-alpha"
    assert second_step.tool_call is not None
    assert second_step.tool_call.tool_call_id == "call-alpha"
    assert second_step.events != ()


def test_provider_graph_discards_queued_tool_call_batch_for_new_run() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(provider=_BatchNonStreamingTurnProvider(), provider_model=provider_model)
    first_context = RuntimeContextWindow(prompt="read two files")
    first_session = _session_with_run("shared-session", "run-one")
    first_request = GraphRunRequest(
        session=first_session,
        prompt="read two files",
        available_tools=_tool_definitions(),
        context_window=first_context,
        assembled_context=_assembled_from_context_window(first_context),
    )

    first_step = graph.step(request=first_request, tool_results=(), session=first_session)

    second_context = RuntimeContextWindow(prompt="unrelated follow-up")
    second_session = _session_with_run("shared-session", "run-two")
    second_request = GraphRunRequest(
        session=second_session,
        prompt="unrelated follow-up",
        available_tools=_tool_definitions(),
        context_window=second_context,
        assembled_context=_assembled_from_context_window(second_context),
    )
    second_step = graph.step(request=second_request, tool_results=(), session=second_session)

    assert first_step.tool_call is not None
    assert first_step.tool_call.tool_call_id == "call-alpha"
    assert second_step.tool_call is not None
    assert second_step.tool_call.tool_call_id == "call-alpha"
    assert second_step.events != ()


def test_provider_graph_passes_runtime_abort_signal_to_provider() -> None:
    provider = _CapturingTurnProvider()
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(provider=provider, provider_model=provider_model)
    request_context = RuntimeContextWindow(prompt="stop")
    abort_signal = _AbortSignal(cancelled=True)

    _ = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="stop",
            available_tools=_tool_definitions(),
            context_window=request_context,
            assembled_context=_assembled_from_context_window(request_context),
            abort_signal=abort_signal,
        ),
        tool_results=(),
        session=_session(),
    )

    assert provider.requests[0].abort_signal is abort_signal
    assert provider.requests[0].abort_signal.cancelled is True


def test_provider_graph_resets_internal_abort_signal_between_requests() -> None:
    provider = _CapturingTurnProvider()
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(provider=provider, provider_model=provider_model)
    cancelled_context = RuntimeContextWindow(prompt="cancel")
    next_context = RuntimeContextWindow(prompt="continue")

    _ = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="cancel",
            context_window=cancelled_context,
            assembled_context=_assembled_from_context_window(cancelled_context),
            metadata={"abort_requested": True},
        ),
        tool_results=(),
        session=_session(),
    )
    _ = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="continue",
            context_window=next_context,
            assembled_context=_assembled_from_context_window(next_context),
        ),
        tool_results=(),
        session=_session(),
    )

    assert provider.abort_cancelled_values == [True, False]


def test_provider_provider_graph_finalizes_after_tool_result() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=StubTurnProvider(name="opencode"),
        provider_model=provider_model,
    )

    request_context = RuntimeContextWindow(
        prompt="read sample.txt",
        tool_results=(
            ToolResult(
                tool_name="read_file",
                content="alpha\n",
                status="ok",
                data={"path": "sample.txt", "content": "alpha\n"},
            ),
        ),
    )
    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=request_context,
            assembled_context=_assembled_from_context_window(request_context),
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

    request_context = RuntimeContextWindow(prompt="read sample.txt")
    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=request_context,
            assembled_context=_assembled_from_context_window(request_context),
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
        match="neither output nor tool calls",
    ) as exc_info:
        _ = graph.step(
            request=GraphRunRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                assembled_context=_assembled_from_context_window(
                    RuntimeContextWindow(prompt="read sample.txt")
                ),
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
                assembled_context=_assembled_from_context_window(
                    RuntimeContextWindow(prompt="read sample.txt")
                ),
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
        "guidance": "Retry later, reduce request volume, or configure a fallback model.",
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
                assembled_context=_assembled_from_context_window(
                    RuntimeContextWindow(prompt="read sample.txt")
                ),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.kind == "context_limit"


@pytest.mark.parametrize(
    "error_kind",
    ["missing_auth", "unsupported_feature", "stream_tool_feedback_shape"],
)
def test_provider_provider_graph_preserves_explicit_stream_error_kind(
    error_kind: ProviderErrorKind,
) -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    class _ExplicitKindStreamErrorTurnProvider:
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
                        error="provider stream failed",
                        error_kind=error_kind,
                    ),
                )
            )

    graph = ProviderGraph(
        provider=_ExplicitKindStreamErrorTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(ProviderExecutionError, match="provider stream failed") as exc_info:
        _ = graph.step(
            request=GraphRunRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                assembled_context=_assembled_from_context_window(
                    RuntimeContextWindow(prompt="read sample.txt")
                ),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.kind == error_kind


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
            assembled_context=RuntimeAssembledContext(
                prompt="read sample.txt",
                tool_results=(),
                continuity_state=None,
                segments=(
                    RuntimeContextSegment(
                        role="system", content="Runtime-managed skills are active."
                    ),
                    RuntimeContextSegment(role="user", content="read sample.txt"),
                ),
                metadata={},
            ),
            metadata={
                "applied_skills": [
                    {
                        "name": "summarize",
                        "description": "Summarize selected files.",
                        "content": "# Summarize\nUse concise bullet points.",
                        "prompt_context": (
                            "Skill: summarize\n"
                            "Description: Summarize selected files.\n"
                            "Instructions:\n# Summarize\nUse concise bullet points."
                        ),
                    }
                ],
            },
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.output == "done"
    assert provider.requests[0].assembled_context is not None
    assert provider.requests[0].assembled_context.prompt == "read sample.txt"
    assert provider.requests[0].assembled_context.segments[0].role == "system"
    assert provider.requests[0].assembled_context.segments[0].content == (
        "Runtime-managed skills are active."
    )


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
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(prompt="read sample.txt")
            ),
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
            assembled_context=_assembled_from_context_window(bounded_context),
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

    assert provider.requests[0].assembled_context is not None
    assert provider.requests[0].assembled_context.tool_results == bounded_context.tool_results
    assert provider.requests[0].assembled_context.continuity_state == RuntimeContinuityState(
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

    request_context = RuntimeContextWindow(
        prompt="use lsp feedback",
        tool_results=(lsp_result,),
    )
    _ = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="use lsp feedback",
            available_tools=_tool_definitions(),
            context_window=request_context,
            assembled_context=_assembled_from_context_window(request_context),
        ),
        tool_results=(lsp_result,),
        session=_session(),
    )

    assert provider.requests[0].assembled_context is not None
    assert provider.requests[0].assembled_context.tool_results == (lsp_result,)


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

    request_context = RuntimeContextWindow(
        prompt="use mcp feedback",
        tool_results=(mcp_result,),
    )
    _ = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="use mcp feedback",
            available_tools=_tool_definitions(),
            context_window=request_context,
            assembled_context=_assembled_from_context_window(request_context),
        ),
        tool_results=(mcp_result,),
        session=_session(),
    )

    assert provider.requests[0].assembled_context is not None
    assert provider.requests[0].assembled_context.tool_results == (mcp_result,)


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
                assembled_context=_assembled_from_context_window(
                    RuntimeContextWindow(prompt="read sample.txt")
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
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(prompt="read sample.txt")
            ),
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


def test_provider_graph_preserves_reasoning_stream_metadata() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(
        provider=_StreamReasoningMetadataTurnProvider(),
        provider_model=provider_model,
    )

    step = graph.step(
        request=GraphRunRequest(
            session=_session(),
            prompt="think",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="think"),
            assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="think")),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    stream_events = [event for event in step.events if event.event_type == "graph.provider_stream"]
    reasoning_event = next(
        event for event in stream_events if event.payload.get("channel") == "reasoning"
    )
    assert reasoning_event.payload["text"] == "private chain"
    assert reasoning_event.payload["metadata"] == {"source": "fixture"}
    assert step.output == "answer"


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
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(prompt="read sample.txt")
            ),
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
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(prompt="read sample.txt")
            ),
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
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(prompt="read sample.txt")
            ),
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
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(prompt="read sample.txt")
            ),
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
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(prompt="read sample.txt")
            ),
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


def test_provider_provider_graph_returns_streamed_tool_call_batch() -> None:
    provider_model = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderGraph(provider=_StreamToolBatchTurnProvider(), provider_model=provider_model)
    request_context = RuntimeContextWindow(prompt="read two files")
    request = GraphRunRequest(
        session=_session(),
        prompt="read two files",
        available_tools=_tool_definitions(),
        context_window=request_context,
        assembled_context=_assembled_from_context_window(request_context),
        metadata={"provider_stream": True},
    )

    first_step = graph.step(request=request, tool_results=(), session=_session())
    second_step = graph.step(
        request=request,
        tool_results=(ToolResult(tool_name="read_file", status="ok", content="alpha"),),
        session=_session(),
    )

    assert first_step.is_finished is False
    assert first_step.tool_call is not None
    assert first_step.tool_call.tool_call_id == "call-alpha"
    assert len(first_step.tool_calls) == 2
    assert second_step.tool_call is not None
    assert second_step.tool_call.tool_call_id == "call-beta"
    assert second_step.events == ()


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
                assembled_context=_assembled_from_context_window(
                    RuntimeContextWindow(prompt="read sample.txt")
                ),
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
                assembled_context=_assembled_from_context_window(
                    RuntimeContextWindow(prompt="read sample.txt")
                ),
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
            assembled_context=_assembled_from_context_window(
                RuntimeContextWindow(prompt="read sample.txt")
            ),
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
                assembled_context=_assembled_from_context_window(
                    RuntimeContextWindow(prompt="read sample.txt")
                ),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )
