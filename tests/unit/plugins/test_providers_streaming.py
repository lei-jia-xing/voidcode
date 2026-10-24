"""Streaming behaviour of the ``providers`` plugin.

The streaming path is driven by a local loopback that writes real SSE frames --
including the frames a well-behaved provider would never send -- so every
assertion is about events the plugin actually produced from that wire, and about
what it refused to claim. Nothing here reaches a real host.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.capabilities.providers.dispatch import RegisteredProvider, build_registered_providers
from voidcode.sdk.provider import (
    ProviderExecutionError,
    ProviderMessage,
    ProviderStreamEvent,
    ProviderTurnRequest,
)

_DONE_FRAME = b"data: [DONE]\n\n"
_TERMINAL_STOP_USAGE: dict[str, object] = {
    "prompt_tokens": 1000,
    "completion_tokens": 500,
    "prompt_tokens_details": {"cached_tokens": 100},
}


def _frame(payload: dict[str, object]) -> bytes:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def _chunk(delta: dict[str, object], finish: str | None = None, usage: dict[str, object] | None = None) -> bytes:
    chunk: dict[str, object] = {
        "id": "chunk-loopback",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "glm-4.5",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return _frame(chunk)


class _SseLoopback(ThreadingHTTPServer):
    """Serves one queued frame list per request, recording each request payload."""

    def __init__(self, frame_groups: list[list[tuple[float, bytes]]]) -> None:
        super().__init__(("127.0.0.1", 0), _SseLoopbackHandler)
        self.frame_groups = frame_groups
        self.requests: list[dict[str, object]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"


class _SseLoopbackHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = cast(_SseLoopback, self.server)
        length = int(self.headers.get("Content-Length") or 0)
        server.requests.append(cast(dict[str, object], json.loads(self.rfile.read(length) or b"{}")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for delay, frame in server.frame_groups.pop(0):
                if delay:
                    time.sleep(delay)
                self.wfile.write(frame)
                self.wfile.flush()
        except OSError:
            # The client timed out or cancelled and hung up: that is the subject under test.
            return

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


class DummyAbortSignal:
    """The abort handle a run hands a provider; nothing else in this module mutates it."""

    def __init__(self, cancelled: bool = False, reason: str | None = None) -> None:
        self._cancelled = cancelled
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._cancelled = value
        self._reason = reason


@pytest.fixture
def start_sse_server() -> Iterator[Callable[[list[list[tuple[float, bytes]]]], _SseLoopback]]:
    started: list[tuple[_SseLoopback, threading.Thread]] = []

    def start(frame_groups: list[list[tuple[float, bytes]]]) -> _SseLoopback:
        server = _SseLoopback(frame_groups)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return server

    try:
        yield start
    finally:
        for server, thread in started:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


@pytest.fixture
def json_server() -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer([])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _provider(entries: dict[str, object], *, name: str = "zai") -> RegisteredProvider:
    providers = {provider.name: provider for provider in build_registered_providers({"providers": entries})}
    assert name in providers
    return providers[name]


def _streaming_provider(base_url: str, *, timeout_seconds: float | None = None) -> RegisteredProvider:
    entry: dict[str, object] = {"api_key": "loopback-key", "base_url": base_url}
    if timeout_seconds is not None:
        entry["timeout_seconds"] = timeout_seconds
    return _provider({"zai": entry}, name="zai")


def _request(*messages: ProviderMessage, **overrides: object) -> ProviderTurnRequest:
    fields: dict[str, object] = {
        "messages": messages or (ProviderMessage("user", "hi"),),
        "raw_model": "zai/glm-4.5",
        "provider_name": "zai",
        "model_name": "glm-4.5",
        "reasoning_effort": None,
    }
    fields.update(overrides)
    return ProviderTurnRequest(**fields)  # type: ignore[arg-type]


def _lifecycle(events: list[ProviderStreamEvent]) -> list[tuple[str, str | None, str | None]]:
    return [(event.kind, event.error_kind, event.done_reason) for event in events]


def test_a_stream_reports_reasoning_text_and_terminal_usage_as_events(start_sse_server: Callable[..., _SseLoopback]) -> None:
    server = start_sse_server(
        [
            [
                (0.0, _chunk({"role": "assistant", "reasoning_content": "weighing options"}, None)),
                (0.0, _chunk({"content": "alpha"}, None)),
                (0.0, _chunk({"content": "beta"}, None)),
                (0.0, _chunk({}, "stop", _TERMINAL_STOP_USAGE)),
                (0.0, _DONE_FRAME),
            ]
        ]
    )
    provider = _streaming_provider(server.base_url)

    events = list(provider.stream_turn(_request()))

    request = server.requests[0]
    assert request["stream"] is True
    # ``include_usage`` is what makes the terminal usage frame arrive at all.
    assert request["stream_options"] == {"include_usage": True}
    # Deltas stay deltas: each event carries exactly the wire's own fragment.
    assert [(event.kind, event.channel, event.text) for event in events] == [
        ("delta", "reasoning", "weighing options"),
        ("delta", "text", "alpha"),
        ("delta", "text", "beta"),
        ("done", "text", None),
    ]

    terminal = events[-1]
    assert terminal.done_reason == "stop"
    assert terminal.metadata == {"request_id": "chunk-loopback", "served_model": "glm-4.5", "finish_reason_reported": True}
    assert terminal.usage is not None
    assert (
        terminal.usage.input_tokens,
        terminal.usage.output_tokens,
        terminal.usage.cache_read_tokens,
        terminal.usage.uncached_input_tokens,
    ) == (1000, 500, 100, 900)
    # The streamed usage is priced by the same shipped rates as a non-streamed one.
    assert terminal.usage.cost_usd == pytest.approx(900 * 0.6e-06 + 500 * 2.2e-06 + 100 * 0.11e-06)


def test_tool_call_fragments_are_reported_in_order_and_parsed_at_the_end(start_sse_server: Callable[..., _SseLoopback]) -> None:
    server = start_sse_server(
        [
            [
                (
                    0.0,
                    _chunk(
                        {
                            "role": "assistant",
                            "tool_calls": [{"index": 0, "id": "call-7", "type": "function", "function": {"name": "read", "arguments": '{"pa'}}],
                        },
                        None,
                    ),
                ),
                (0.0, _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.txt"}'}}]}, None)),
                (0.0, _chunk({}, "tool_calls")),
                (0.0, _DONE_FRAME),
            ]
        ]
    )
    provider = _streaming_provider(server.base_url)

    events = list(provider.stream_turn(_request()))

    assert [(event.kind, event.tool_call_id, event.tool_name, event.arguments_delta, event.fragment_ordinal) for event in events[:-1]] == [
        ("tool_call_start", "call-7", "read", None, None),
        ("tool_call_delta", "call-7", None, '{"pa', 0),
        ("tool_call_delta", "call-7", None, 'th": "a.txt"}', 1),
        ("tool_call_end", "call-7", "read", None, None),
    ]
    assert events[0].tool_call_ordinal == 0
    assert events[3].parsed_arguments == {"path": "a.txt"}
    assert events[-1].kind == "done" and events[-1].done_reason == "tool_calls"
    # Arguments never ride along as aggregated text: only the parsed end event carries them.
    assert all(event.text is None for event in events)


def test_an_unrecognized_finish_reason_stays_unknown_never_completed(start_sse_server: Callable[..., _SseLoopback]) -> None:
    server = start_sse_server([[(0.0, _chunk({"role": "assistant", "content": "odd"}, "something-new")), (0.0, _DONE_FRAME)]])
    provider = _streaming_provider(server.base_url)

    events = list(provider.stream_turn(_request()))

    assert events[-1].kind == "done"
    assert events[-1].done_reason == "unknown"
    assert events[-1].done_reason != "completed"
    assert events[-1].metadata == {
        "request_id": "chunk-loopback",
        "served_model": "glm-4.5",
        "finish_reason_raw": "something-new",
        "finish_reason_reported": True,
    }


def test_a_stream_without_a_terminal_marker_never_reports_completed(start_sse_server: Callable[..., _SseLoopback]) -> None:
    """The terminal marker is the only thing that makes a turn durable.

    A stream that simply ends is refused typed (``missing_terminal_event``)
    instead of being reported as a completed turn: the deltas that arrived are
    the events the caller saw, and no terminal event ever claims success.
    """
    server = start_sse_server([[(0.0, _chunk({"role": "assistant", "content": "partial"}, None)), (0.0, _DONE_FRAME)]])
    provider = _streaming_provider(server.base_url)

    events: list[ProviderStreamEvent] = []
    with pytest.raises(ProviderExecutionError) as excinfo:
        for event in provider.stream_turn(_request()):
            events.append(event)

    error = excinfo.value
    assert error.kind == "provider_error"
    assert error.retryable is False
    assert error.details == {"source": "stream", "reason": "missing_terminal_event"}
    assert [event.kind for event in events] == ["delta"]
    assert all(event.kind != "done" for event in events)


def test_cancel_before_the_stream_sends_no_request_and_reports_cancelled(start_sse_server: Callable[..., _SseLoopback]) -> None:
    server = start_sse_server([[(0.0, _chunk({"role": "assistant", "content": "never sent"}, None)), (0.0, _DONE_FRAME)]])
    provider = _streaming_provider(server.base_url)
    abort = DummyAbortSignal(cancelled=True, reason="user")

    events = list(provider.stream_turn(_request(abort_signal=abort)))

    assert _lifecycle(events) == [("error", "cancelled", None), ("done", None, "cancelled")]
    assert events[0].error == "provider stream cancelled"
    assert server.requests == []


def test_cancel_mid_stream_reports_cancelled_after_the_delta_already_yielded(start_sse_server: Callable[..., _SseLoopback]) -> None:
    server = start_sse_server(
        [
            [
                (0.0, _chunk({"role": "assistant", "content": "first"}, None)),
                (0.0, _chunk({"content": "second"}, None)),
                (0.0, _chunk({}, "stop")),
                (0.0, _DONE_FRAME),
            ]
        ]
    )
    provider = _streaming_provider(server.base_url)
    abort = DummyAbortSignal()
    stream = provider.stream_turn(_request(abort_signal=abort))

    first = next(stream)
    assert (first.kind, first.channel, first.text) == ("delta", "text", "first")

    abort.set_cancelled(True, reason="user")
    rest = list(stream)

    assert _lifecycle(rest) == [("error", "cancelled", None), ("done", None, "cancelled")]
    # ``second`` was never delivered: cancellation ends the turn where the caller asked.
    assert all(event.text != "second" for event in rest)
    assert len(server.requests) == 1


def test_a_stalled_chunk_is_cut_off_by_the_configured_timeout(start_sse_server: Callable[..., _SseLoopback]) -> None:
    """The pump is bounded by the configured timeout, and the partial output already delivered is kept."""
    server = start_sse_server(
        [
            [
                (0.0, _chunk({"role": "assistant", "content": "partial"}, None)),
                (5.0, _chunk({}, "stop")),  # far beyond the configured 0.2s idle budget
                (0.0, _DONE_FRAME),
            ]
        ]
    )
    provider = _streaming_provider(server.base_url, timeout_seconds=0.2)

    events: list[ProviderStreamEvent] = []
    with pytest.raises(ProviderExecutionError) as excinfo:
        for event in provider.stream_turn(_request()):
            events.append(event)

    assert [event.text for event in events] == ["partial"]
    # Both ends enforce the same 0.2s stalled-read budget: the pump raises its typed chunk
    # timeout, while the SDK read that stalls in the worker raises through the transport's
    # typed provider_error mapping (network_failure, still retryable). Scheduling decides
    # which one lands, so either refusal is accepted; both keep the partial output, made one
    # request, and never completed.
    if excinfo.value.message == "provider stream chunk timeout exceeded":
        assert excinfo.value.kind == "transient_failure"
        assert excinfo.value.retryable is True
    else:
        assert excinfo.value.kind == "provider_error"
        assert excinfo.value.retryable is True
        assert excinfo.value.details.get("network_failure") is True
    assert len(server.requests) == 1


def test_a_stalled_chunk_with_a_pending_abort_is_cancelled_not_transient(start_sse_server: Callable[..., _SseLoopback]) -> None:
    """Caller intent dominates the timeout: a wait that expires after an abort is a cancellation."""
    server = start_sse_server(
        [
            [
                (0.0, _chunk({"role": "assistant", "content": "partial"}, None)),
                (5.0, _chunk({}, "stop")),
                (0.0, _DONE_FRAME),
            ]
        ]
    )
    provider = _streaming_provider(server.base_url, timeout_seconds=0.2)
    abort = DummyAbortSignal()
    stream = provider.stream_turn(_request(abort_signal=abort))
    assert next(stream).text == "partial"

    abort.set_cancelled(True, reason="user")
    with pytest.raises(ProviderExecutionError) as excinfo:
        list(stream)

    assert excinfo.value.kind == "cancelled"
    assert excinfo.value.retryable is False
    assert excinfo.value.fallback_allowed is False


def test_streaming_and_non_streaming_report_the_same_turn_facts(
    start_sse_server: Callable[..., _SseLoopback],
    json_server: OpenAILoopbackServer,
) -> None:
    """The durable turn a stream produces is the turn a non-streaming reply produces."""
    json_server.replies.append(
        openai_response(
            message={"role": "assistant", "content": "same answer", "reasoning_content": "same reasoning"},
            finish="stop",
            usage=dict(_TERMINAL_STOP_USAGE),
        )
    )
    server = start_sse_server(
        [
            [
                (0.0, _chunk({"role": "assistant", "reasoning_content": "same reasoning"}, None)),
                (0.0, _chunk({"content": "same answer"}, None)),
                (0.0, _chunk({}, "stop", dict(_TERMINAL_STOP_USAGE))),
                (0.0, _DONE_FRAME),
            ]
        ]
    )

    direct = _streaming_provider(f"http://127.0.0.1:{json_server.server_port}/v1").propose_turn(_request())
    streamed_events = list(_streaming_provider(server.base_url).stream_turn(_request()))
    streamed = streamed_events[-1]

    assert direct.output == "same answer"
    assert "".join(event.text or "" for event in streamed_events if event.channel == "reasoning") == "same reasoning"
    assert "".join(event.text or "" for event in streamed_events if event.kind == "delta" and event.channel == "text") == "same answer"
    assert direct.reasoning == "same reasoning"
    assert streamed.kind == "done"
    assert streamed.done_reason == direct.done_reason == "stop"
    assert streamed.usage == direct.usage
    assert direct.usage is not None and direct.usage.cost_usd == pytest.approx(900 * 0.6e-06 + 500 * 2.2e-06 + 100 * 0.11e-06)

    # The body the two paths send differs only in the streaming switch itself.
    direct_request = json_server.requests[0]
    stream_request = server.requests[0]
    assert direct_request["stream"] is False
    assert stream_request["stream"] is True
    assert direct_request["model"] == stream_request["model"]
    assert direct_request["messages"] == stream_request["messages"]


def test_streamed_tool_calls_carry_the_same_facts_as_a_non_streamed_turn(
    start_sse_server: Callable[..., _SseLoopback],
    json_server: OpenAILoopbackServer,
) -> None:
    call = {"id": "call-9", "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": "a.txt"})}}
    json_server.replies.append(openai_response(message={"role": "assistant", "content": None, "tool_calls": [call]}, finish="tool_calls"))
    server = start_sse_server(
        [
            [
                (0.0, _chunk({"role": "assistant", "tool_calls": [dict(call, index=0)]}, None)),
                (0.0, _chunk({}, "tool_calls")),
                (0.0, _DONE_FRAME),
            ]
        ]
    )

    direct = _streaming_provider(f"http://127.0.0.1:{json_server.server_port}/v1").propose_turn(_request())
    streamed_events = list(_streaming_provider(server.base_url).stream_turn(_request()))

    assert len(direct.tool_calls) == 1
    assert (direct.tool_calls[0].tool_name, direct.tool_calls[0].tool_call_id, dict(direct.tool_calls[0].arguments)) == (
        "read",
        "call-9",
        {"path": "a.txt"},
    )
    tool_event = next(event for event in streamed_events if event.channel == "tool")
    assert json.loads(tool_event.text or "") == {"tool_name": "read", "arguments": {"path": "a.txt"}, "tool_call_id": "call-9"}
    assert streamed_events[-1].done_reason == direct.done_reason == "tool_calls"


@pytest.mark.parametrize("cancel", [False, True])
def test_google_real_sdk_stream_preserves_terminal_and_cancellation_semantics(start_sse_server, cancel):
    first = {"candidates": [{"content": {"role": "model", "parts": [{"text": "first"}]}}]}
    final = {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "second"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
    }
    server = start_sse_server([[(0.0, _frame(first)), (0.0, _frame(final))]])
    provider = _provider({"google": {"api_key": "loopback-key", "base_url": server.base_url}}, name="google")
    abort = DummyAbortSignal()
    request = ProviderTurnRequest(
        messages=(ProviderMessage("user", "hi"),), provider_name="google", model_name="gemini-2.5-flash", abort_signal=abort
    )
    try:
        stream = provider.stream_turn(request)
        assert next(stream).text == "first"
        if cancel:
            abort.set_cancelled(True)
        remaining = list(stream)
        assert remaining[-1].done_reason == ("cancelled" if cancel else "stop")
        if cancel:
            assert remaining[0].error_kind == "cancelled"
            assert not any(event.kind == "delta" for event in remaining)
        else:
            assert remaining[0].text == "second"
            assert remaining[-1].usage.input_tokens == 3
            assert remaining[-1].usage.output_tokens == 2
        assert len(server.requests) == 1
    finally:
        provider.close()


def test_google_cancel_while_waiting_does_not_wait_for_sdk_timeout(start_sse_server):
    first = {"candidates": [{"content": {"role": "model", "parts": [{"text": "partial"}]}}]}
    final = {"candidates": [{"finishReason": "STOP"}]}
    server = start_sse_server([[(0.0, _frame(first)), (2.0, _frame(final))]])
    provider = _provider({"google": {"api_key": "loopback-key", "base_url": server.base_url, "timeout_seconds": 3}}, name="google")
    abort = DummyAbortSignal()
    request = ProviderTurnRequest(
        messages=(ProviderMessage("user", "hi"),), provider_name="google", model_name="gemini-2.5-flash", abort_signal=abort
    )
    timer = threading.Timer(0.05, lambda: abort.set_cancelled(True))
    try:
        stream = provider.stream_turn(request)
        assert next(stream).text == "partial"
        started = time.monotonic()
        timer.start()
        remaining = list(stream)
        assert time.monotonic() - started < 1
        assert remaining[0].error_kind == "cancelled"
        assert remaining[-1].done_reason == "cancelled"
        assert not any(event.kind == "delta" for event in remaining)
    finally:
        timer.cancel()
        provider.close()
