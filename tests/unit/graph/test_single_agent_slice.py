from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from voidcode.core.deterministic_turns import DeterministicTurnProducer
from voidcode.core.engine import CallSeed, TurnEngine
from voidcode.core.memory_host import MemoryHost
from voidcode.core.provider_turns import ProviderTurnProducer
from voidcode.core.tool_context import ToolContext
from voidcode.core.transcript import ContextSegment, tool_result_output
from voidcode.core.turns import TurnRequest, TurnSessionSnapshot
from voidcode.provider.protocol import (
    ProviderErrorKind,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_model
from voidcode.runtime.context.window import (
    RuntimeAssembledContext,
    RuntimeContextWindow,
)
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolEffect, ToolResult
from voidcode.tools.read import ReadTool


class _StubTurnProvider:
    """Test double: emit the ``read <path>`` tool call a prompt's next command names."""

    def __init__(self, *, name: str) -> None:
        self.name = name

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        assembled_context = request.assembled_context
        commands = [line.strip() for line in assembled_context.prompt.splitlines() if line.strip()]
        if not commands:
            raise ValueError("request must not be empty")

        step_index = len(assembled_context.tool_results)
        if step_index >= len(commands):
            if not assembled_context.tool_results:
                raise ValueError("request must contain at least one actionable command")
            return ProviderTurnResult(output=tool_result_output(assembled_context.tool_results[-1]) or "")

        path = commands[step_index].removeprefix("read ").strip()
        return ProviderTurnResult(tool_call=ToolCall(tool_name="read", arguments={"path": path}))


def _tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(name="read", description="read", input_schema={}, effects=frozenset({ToolEffect.READ})),
        ToolDefinition(name="write", description="write", input_schema={}, effects=frozenset({ToolEffect.WRITE})),
    )


def _session(session_id: str = "s1") -> TurnSessionSnapshot:
    return TurnSessionSnapshot(session_id=session_id)


def _session_with_run(session_id: str = "s1", run_id: str = "run-one") -> TurnSessionSnapshot:
    return TurnSessionSnapshot(session_id=session_id, metadata={"run_id": run_id})


def _assembled_from_context_window(context_window: RuntimeContextWindow) -> RuntimeAssembledContext:
    segments: list[ContextSegment] = [ContextSegment(role="user", content=context_window.prompt)]
    for index, result in enumerate(context_window.tool_results, start=1):
        tool_call_id = f"test_tool_{index}"
        segments.append(
            ContextSegment(
                role="assistant",
                content=None,
                tool_call_id=tool_call_id,
                tool_name=result.tool_name,
                tool_arguments={},
            )
        )
        segments.append(
            ContextSegment(
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


class _MixedNonStreamingTurnProvider:
    name = "opencode-zen"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(
            tool_call=ToolCall(tool_name="read", arguments={"path": "sample.txt"}),
            output="done",
        )


class _BatchNonStreamingTurnProvider:
    name = "opencode-zen"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(
            tool_calls=(
                ToolCall(
                    tool_name="read",
                    arguments={"path": "alpha.txt"},
                    tool_call_id="call-alpha",
                ),
                ToolCall(
                    tool_name="read",
                    arguments={"path": "beta.txt"},
                    tool_call_id="call-beta",
                ),
            )
        )


class _MixedStreamingTurnProvider:
    name = "opencode-zen"

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
                    text='{"tool_name":"read","arguments":{"path":"sample.txt"}}',
                ),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


class _EmptyNonStreamingTurnProvider:
    name = "opencode-zen"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult()


class _StreamOutputTurnProvider:
    name = "opencode-zen"
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
    name = "opencode-zen"

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


class _StreamNoTextDoneTurnProvider:
    name = "opencode-zen"

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
    name = "opencode-zen"

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
                    text='{"tool_name":"read","arguments":{"path":"sample.txt"}}',
                ),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


class _StreamChunkedToolTurnProvider:
    name = "opencode-zen"

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
                    text='{"tool_name":"read",',
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
    name = "opencode-zen"

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
                    text='{"tool_name":"read","arguments":{}}',
                ),
                ProviderStreamEvent(
                    kind="content",
                    channel="tool",
                    text='{"tool_name":"read","arguments":{"path":"sample.txt"}}',
                ),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


class _StreamToolBatchTurnProvider:
    name = "opencode-zen"

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
                        '{"tool_name":"read","tool_call_id":"call-alpha",'
                        '"arguments":{"path":"alpha.txt"}},'
                        '{"tool_name":"read","tool_call_id":"call-beta",'
                        '"arguments":{"path":"beta.txt"}}]}'
                    ),
                ),
                ProviderStreamEvent(kind="done", done_reason="completed"),
            )
        )


class _StreamMalformedToolTurnProvider:
    name = "opencode-zen"

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
    name = "opencode-zen"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="should-not-be-used")

    def stream_turn(self, request: ProviderTurnRequest):
        _ = request
        return iter((ProviderStreamEvent(kind="delta", channel="text", text="partial"),))


def test_provider_provider_graph_requests_tool_on_first_turn() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderTurnProducer(
        provider=_StubTurnProvider(name="opencode-zen"),
        provider_model=provider_model,
    )

    request_context = RuntimeContextWindow(prompt="read sample.txt")
    step = graph.produce(
        request=TurnRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=request_context,
            assembled_context=_assembled_from_context_window(request_context),
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.tool_calls[0] is not None
    assert step.tool_calls[0].tool_name == "read"
    assert step.output is None
    assert step.is_finished is False
    assert [event.kind for event in step.facts] == ["loop_step", "model_turn"]
    assert step.facts[0].payload == {"step": 1, "phase": "plan"}
    assert step.facts[1].payload == {
        "turn": 1,
        "mode": "provider",
        "provider": "opencode-zen",
        "model": "gpt-5.4",
        "attempt": 0,
        "streaming": False,
        "prompt": "read sample.txt",
    }


def test_deterministic_read_preserves_actual_body_whitespace(tmp_path: Path) -> None:
    body = "\n    alpha: beta\n(parenthetical user text)\n\n  gamma"
    (tmp_path / "sample.txt").write_text(f"{body}\n", encoding="utf-8")
    tool = ReadTool()
    result = tool.invoke(ToolCall("read", {"path": "sample.txt"}), context=ToolContext(workspace=tmp_path))
    context = RuntimeContextWindow(prompt="read sample.txt", tool_results=(result,))
    request = TurnRequest(
        session=_session(),
        prompt="read sample.txt",
        available_tools=(tool.definition,),
        context_window=context,
        assembled_context=_assembled_from_context_window(context),
        run_step=2,
    )

    plan = DeterministicTurnProducer().produce(request, (result,), session=request.session)

    assert plan.output == body


def test_provider_provider_graph_prefers_nonstream_tool_call_over_text() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderTurnProducer(
        provider=_MixedNonStreamingTurnProvider(),
        provider_model=provider_model,
    )

    request_context = RuntimeContextWindow(prompt="read sample.txt")
    step = graph.produce(
        request=TurnRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=request_context,
            assembled_context=_assembled_from_context_window(request_context),
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.tool_calls[0] is not None
    assert step.tool_calls[0].tool_name == "read"
    assert step.output is None


def test_provider_provider_graph_rejects_nonstream_missing_terminal_outcome() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderTurnProducer(
        provider=_EmptyNonStreamingTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(
        ProviderExecutionError,
        match="neither output nor tool calls",
    ) as exc_info:
        _ = graph.produce(
            request=TurnRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.kind == "transient_failure"


def test_provider_graph_treats_unrecognized_nonstream_finish_reason_as_completed() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    class _UnknownDoneReasonTurnProvider:
        name = "opencode-zen"

        def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            _ = request
            return ProviderTurnResult(
                output="done",
                done_reason="unknown",
                finish_reason_reported=True,
                metadata={"finish_reason_raw": "eos_token"},
            )

    graph = ProviderTurnProducer(provider=_UnknownDoneReasonTurnProvider(), provider_model=provider_model)

    step = graph.produce(
        request=TurnRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
        ),
        tool_results=(),
        session=_session(),
    )

    # The upstream declared a terminal outcome; an unrecognized reason token is a
    # completed (stop-equivalent) turn, not a user-visible failure.
    assert step.is_finished is True
    assert step.output == "done"


def test_provider_graph_treats_unrecognized_stream_finish_reason_as_completed() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    class _UnknownDoneReasonStreamTurnProvider:
        name = "opencode-zen"

        def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            _ = request
            return ProviderTurnResult(output="should-not-be-used")

        def stream_turn(self, request: ProviderTurnRequest):
            _ = request
            return iter(
                (
                    ProviderStreamEvent(kind="delta", channel="text", text="done"),
                    ProviderStreamEvent(kind="done", done_reason="unknown", metadata={"finish_reason_raw": "eos_token"}),
                )
            )

    graph = ProviderTurnProducer(provider=_UnknownDoneReasonStreamTurnProvider(), provider_model=provider_model)

    step = graph.produce(
        request=TurnRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.is_finished is True
    assert step.output == "done"


def test_provider_graph_treats_omitted_finish_reason_as_completed() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    class _UnknownNonStreamingTurnProvider:
        name = "opencode-zen"

        def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            _ = request
            # No metadata: the mapped reason is all the graph has to go on.
            return ProviderTurnResult(output="done", done_reason="unknown", finish_reason_reported=True)

    class _UnknownStreamingTurnProvider:
        name = "opencode-zen"

        def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            _ = request
            return ProviderTurnResult(output="should-not-be-used")

        def stream_turn(self, request: ProviderTurnRequest):
            _ = request
            return iter(
                (
                    ProviderStreamEvent(kind="delta", channel="text", text="done"),
                    ProviderStreamEvent(kind="done", done_reason="unknown"),
                )
            )

    cases = ((_UnknownNonStreamingTurnProvider(), {}), (_UnknownStreamingTurnProvider(), {"provider_stream": True}))
    for provider, metadata in cases:
        graph = ProviderTurnProducer(provider=provider, provider_model=provider_model)
        step = graph.produce(
            request=TurnRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
                metadata=metadata,
            ),
            tool_results=(),
            session=_session(),
        )

        assert step.is_finished is True
        assert step.output == "done"


def test_provider_provider_graph_preserves_stream_error_details() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    class _DetailedStreamErrorTurnProvider:
        name = "opencode-zen"

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

    graph = ProviderTurnProducer(
        provider=_DetailedStreamErrorTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(ProviderExecutionError, match="network interrupted") as exc_info:
        _ = graph.produce(
            request=TurnRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
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
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    class _ContextLimitStreamErrorTurnProvider:
        name = "opencode-zen"

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

    graph = ProviderTurnProducer(
        provider=_ContextLimitStreamErrorTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(
        ProviderExecutionError,
        match="prompt exceeds the context window",
    ) as exc_info:
        _ = graph.produce(
            request=TurnRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
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
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    class _ExplicitKindStreamErrorTurnProvider:
        name = "opencode-zen"

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

    graph = ProviderTurnProducer(
        provider=_ExplicitKindStreamErrorTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(ProviderExecutionError, match="provider stream failed") as exc_info:
        _ = graph.produce(
            request=TurnRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.kind == error_kind


def test_provider_provider_graph_streams_ordered_events_and_deterministic_output() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderTurnProducer(
        provider=_StreamOutputTurnProvider(),
        provider_model=provider_model,
    )

    step = graph.produce(
        request=TurnRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.is_finished is True
    assert step.output == "stream-final"
    stream_events = [event for event in step.facts if event.kind == "provider_stream"]
    assert [event.payload["kind"] for event in stream_events] == ["delta", "delta", "done"]
    model_turn_events = [event for event in step.facts if event.kind == "model_turn"]
    assert model_turn_events
    assert model_turn_events[0].payload["streaming"] is True


def test_provider_graph_preserves_reasoning_stream_metadata() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderTurnProducer(
        provider=_StreamReasoningMetadataTurnProvider(),
        provider_model=provider_model,
    )

    step = graph.produce(
        request=TurnRequest(
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

    stream_events = [event for event in step.facts if event.kind == "provider_stream"]
    reasoning_event = next(event for event in stream_events if event.payload.get("channel") == "reasoning")
    assert reasoning_event.payload["text"] == "private chain"
    assert reasoning_event.payload["metadata"] == {"source": "fixture"}
    assert step.output == "answer"


def test_provider_provider_graph_stream_done_without_text_is_rejected() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    provider = _StreamNoTextDoneTurnProvider()
    graph = ProviderTurnProducer(provider=provider, provider_model=provider_model)

    with pytest.raises(ProviderExecutionError, match="neither output nor tool calls"):
        _ = graph.produce(
            request=TurnRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )
    assert provider.stream_calls == 1
    assert provider.propose_calls == 0


@pytest.mark.parametrize(
    "provider_class",
    (
        pytest.param(_StreamToolTurnProvider, id="single-complete-payload"),
        pytest.param(_StreamChunkedToolTurnProvider, id="chunked-join"),
        pytest.param(_StreamToolSnapshotTurnProvider, id="latest-complete-snapshot"),
    ),
)
def test_provider_provider_graph_parses_streamed_tool_call(provider_class: type[Any]) -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderTurnProducer(
        provider=provider_class(),
        provider_model=provider_model,
    )

    step = graph.produce(
        request=TurnRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.is_finished is False
    assert step.output is None
    assert step.tool_calls[0] is not None
    assert step.tool_calls[0].tool_name == "read"
    assert step.tool_calls[0].arguments == {"path": "sample.txt"}


def test_provider_provider_graph_prefers_streamed_tool_call_over_text() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderTurnProducer(
        provider=_MixedStreamingTurnProvider(),
        provider_model=provider_model,
    )

    step = graph.produce(
        request=TurnRequest(
            session=_session(),
            prompt="read sample.txt",
            available_tools=_tool_definitions(),
            context_window=RuntimeContextWindow(prompt="read sample.txt"),
            assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
            metadata={"provider_stream": True},
        ),
        tool_results=(),
        session=_session(),
    )

    assert step.tool_calls[0] is not None
    assert step.tool_calls[0].tool_name == "read"
    assert step.output is None


def test_provider_provider_graph_rejects_malformed_streamed_tool_payload() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderTurnProducer(
        provider=_StreamMalformedToolTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(ProviderExecutionError, match="malformed tool payload") as exc_info:
        _ = graph.produce(
            request=TurnRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.kind == "transient_failure"


def test_provider_provider_graph_requires_done_event_for_stream_completion() -> None:
    provider_model = resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )
    graph = ProviderTurnProducer(
        provider=_StreamMissingDoneTurnProvider(),
        provider_model=provider_model,
    )

    with pytest.raises(ProviderExecutionError, match="without a done event") as exc_info:
        _ = graph.produce(
            request=TurnRequest(
                session=_session(),
                prompt="read sample.txt",
                available_tools=_tool_definitions(),
                context_window=RuntimeContextWindow(prompt="read sample.txt"),
                assembled_context=_assembled_from_context_window(RuntimeContextWindow(prompt="read sample.txt")),
                metadata={"provider_stream": True},
            ),
            tool_results=(),
            session=_session(),
        )

    assert exc_info.value.kind == "transient_failure"


def test_provider_graph_maps_tool_call_lifecycle_and_builds_final_call() -> None:
    class _LifecycleProvider:
        name = "opencode-zen"

        def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            _ = request
            return ProviderTurnResult(output="unused")

        def stream_turn(self, request: ProviderTurnRequest):
            _ = request
            return iter(
                (
                    ProviderStreamEvent(
                        kind="tool_call_start",
                        channel="tool",
                        tool_call_id="call-1",
                        tool_name="read",
                        tool_call_ordinal=0,
                    ),
                    ProviderStreamEvent(
                        kind="tool_call_delta",
                        channel="tool",
                        tool_call_id="call-1",
                        arguments_delta='{"path":',
                        tool_call_ordinal=0,
                    ),
                    ProviderStreamEvent(
                        kind="tool_call_delta",
                        channel="tool",
                        tool_call_id="call-1",
                        arguments_delta='"sample.txt"}',
                        tool_call_ordinal=0,
                    ),
                    ProviderStreamEvent(
                        kind="tool_call_end",
                        channel="tool",
                        tool_call_id="call-1",
                        tool_call_ordinal=0,
                        parsed_arguments={"path": "sample.txt"},
                    ),
                    ProviderStreamEvent(kind="done", done_reason="tool_calls"),
                )
            )

    graph = ProviderTurnProducer(
        provider=_LifecycleProvider(),
        provider_model=resolve_provider_model("opencode-zen/gpt-5.4", registry=ModelProviderRegistry.with_defaults()),
    )
    context = RuntimeContextWindow(prompt="read sample.txt")
    step = graph.produce(
        TurnRequest(
            session=_session(),
            prompt=context.prompt,
            available_tools=_tool_definitions(),
            context_window=context,
            assembled_context=_assembled_from_context_window(context),
            metadata={"provider_stream": True},
        ),
        (),
        session=_session(),
    )
    assert step.tool_calls[0] is not None and step.tool_calls[0].arguments == {"path": "sample.txt"}
    assert [event.kind for event in step.facts if event.kind.startswith("tool_call_")] == [
        "tool_call_start",
        "tool_call_delta",
        "tool_call_delta",
        "tool_call_end",
    ]


def test_provider_graph_rejects_incomplete_lifecycle_call() -> None:
    class _IncompleteProvider:
        name = "opencode-zen"

        def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            _ = request
            return ProviderTurnResult(output="unused")

        def stream_turn(self, request: ProviderTurnRequest):
            _ = request
            return iter(
                (
                    ProviderStreamEvent(
                        kind="tool_call_start",
                        channel="tool",
                        tool_call_id="call-1",
                        tool_name="read",
                        tool_call_ordinal=0,
                    ),
                    ProviderStreamEvent(
                        kind="tool_call_delta",
                        channel="tool",
                        tool_call_id="call-1",
                        arguments_delta='{"path":',
                        tool_call_ordinal=0,
                    ),
                    ProviderStreamEvent(kind="done", done_reason="tool_calls"),
                )
            )

    graph = ProviderTurnProducer(
        provider=_IncompleteProvider(),
        provider_model=resolve_provider_model("opencode-zen/gpt-5.4", registry=ModelProviderRegistry.with_defaults()),
    )
    context = RuntimeContextWindow(prompt="read sample.txt")
    with pytest.raises(ProviderExecutionError, match="neither output nor tool calls"):
        graph.produce(
            TurnRequest(
                session=_session(),
                prompt=context.prompt,
                available_tools=_tool_definitions(),
                context_window=context,
                assembled_context=_assembled_from_context_window(context),
                metadata={"provider_stream": True},
            ),
            (),
            session=_session(),
        )


def test_provider_graph_merges_explicit_and_complete_streamed_tool_calls() -> None:
    class _MixedToolCallProvider:
        name = "opencode-zen"

        def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            _ = request
            return ProviderTurnResult(output="unused")

        def stream_turn(self, request: ProviderTurnRequest):
            _ = request
            return iter(
                (
                    ProviderStreamEvent(
                        kind="content",
                        channel="tool",
                        text=(
                            '{"tool_calls":['
                            '{"tool_name":"read","tool_call_id":"complete",'
                            '"arguments":{"path":"complete.txt"}},'
                            '{"tool_name":"write","tool_call_id":"explicit",'
                            '"arguments":{"path":"out.txt"}}]}'
                        ),
                    ),
                    ProviderStreamEvent(
                        kind="tool_call_start",
                        channel="tool",
                        tool_call_id="explicit",
                        tool_name="write",
                        tool_call_ordinal=1,
                    ),
                    ProviderStreamEvent(
                        kind="tool_call_delta",
                        channel="tool",
                        tool_call_id="explicit",
                        arguments_delta='{"path":"out.txt"}',
                        tool_call_ordinal=1,
                        fragment_ordinal=0,
                    ),
                    ProviderStreamEvent(
                        kind="tool_call_end",
                        channel="tool",
                        tool_call_id="explicit",
                        tool_call_ordinal=1,
                        fragment_ordinal=0,
                        parsed_arguments={"path": "out.txt"},
                    ),
                    ProviderStreamEvent(kind="done", done_reason="tool_calls"),
                )
            )

    graph = ProviderTurnProducer(
        provider=_MixedToolCallProvider(),
        provider_model=resolve_provider_model("opencode-zen/gpt-5.4", registry=ModelProviderRegistry.with_defaults()),
    )
    context = RuntimeContextWindow(prompt="read and write")
    step = graph.produce(
        TurnRequest(
            session=_session(),
            prompt=context.prompt,
            available_tools=_tool_definitions(),
            context_window=context,
            assembled_context=_assembled_from_context_window(context),
            metadata={"provider_stream": True},
        ),
        (),
        session=_session(),
    )

    assert [call.tool_call_id for call in step.tool_calls] == ["complete", "explicit"]
    assert [call.tool_name for call in step.tool_calls] == ["read", "write"]


def test_turn_engine_restores_completed_seed_result_without_replaying_it(tmp_path: Path) -> None:
    class _WorkspaceReadTool:
        definition = ReadTool.definition

        def __init__(self, workspace: Path) -> None:
            self.workspace = workspace
            self.delegate = ReadTool()
            self.call_ids: list[str | None] = []

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            self.call_ids.append(call.tool_call_id)
            return self.delegate.invoke(call, context=replace(context, workspace=self.workspace))

    body = "real seeded tool output"
    _ = (tmp_path / "sample.txt").write_text(body, encoding="utf-8")
    tool = _WorkspaceReadTool(tmp_path)
    call = ToolCall("read", {"path": "sample.txt"}, "call-authenticated-read")
    actual_result = tool.invoke(
        call,
        context=ToolContext(session_id="seed-session", invocation_id=call.tool_call_id),
    )
    seed_result = replace(
        actual_result,
        data={**actual_result.data, "tool_call_id": call.tool_call_id, "arguments": dict(call.arguments)},
    )
    invocations_before = tuple(tool.call_ids)

    host = MemoryHost(tools=(tool,))
    engine = TurnEngine(DeterministicTurnProducer()).run(
        host.request("read sample.txt"),
        host=host,
        seed=CallSeed((call,), completed_results=(seed_result,)),
    )
    while True:
        try:
            _ = next(engine)
        except StopIteration as completed:
            result = completed.value
            break

    assert invocations_before == ("call-authenticated-read",)
    assert tuple(tool.call_ids) == invocations_before
    assert result.status == "completed"
    assert result.output == body
