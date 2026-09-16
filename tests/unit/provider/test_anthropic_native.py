from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import cast

import httpx2
import pytest

from voidcode.provider.anthropic_native import AnthropicMessagesProvider, AnthropicMessagesTransport, AnthropicTransportError
from voidcode.provider.config import AnthropicProviderConfig
from voidcode.provider.protocol import (
    ProviderAbortSignal,
    ProviderCacheRetention,
    ProviderContextSegment,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
)
from voidcode.tools.contracts import ToolDefinition, ToolResult


@dataclass(frozen=True)
class _Context:
    prompt: str
    segments: tuple[ProviderContextSegment, ...]
    tool_results: tuple[ToolResult, ...] = ()
    continuity_state: object | None = None
    metadata: dict[str, object] | None = None


class _FakeTransport:
    def __init__(self, *, response: dict[str, object] | None = None, events: tuple[dict[str, object], ...] = ()) -> None:
        self.response = response or {
            "id": "msg_test",
            "type": "message",
            "model": "claude-test",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
        }
        self.events = events
        self.payloads: list[dict[str, object]] = []
        self.closed = False

    def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object:
        _ = timeout_seconds
        self.payloads.append(payload)
        if payload.get("stream"):
            parent = self

            class _Stream:
                def __iter__(self):
                    yield from parent.events

                def close(self) -> None:
                    parent.closed = True

            return iter(_Stream())
        return self.response


def _request(
    *,
    segments: tuple[ProviderContextSegment, ...],
    tools: tuple[ToolDefinition, ...] = (),
    effort: str | None = None,
    abort_signal: ProviderAbortSignal | None = None,
    cache_retention: ProviderCacheRetention | None = None,
    session_id: str | None = None,
) -> ProviderTurnRequest:
    return ProviderTurnRequest(
        assembled_context=_Context(prompt="answer", segments=segments),
        available_tools=tools,
        provider_name="anthropic",
        model_name="claude-test",
        raw_model="anthropic/claude-test",
        reasoning_effort=effort,
        cache_retention=cache_retention,
        session_id=session_id,
        abort_signal=abort_signal,
    )


def test_transport_normalizes_endpoint_and_native_headers() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"type": "message", "content": [], "stop_reason": "end_turn"})

    transport = AnthropicMessagesTransport(
        base_url="https://anthropic.test/v1/",
        api_key="sk-ant-test",
        version="2024-01-01",
        beta_headers=("prompt-caching-2024-07-31", "prompt-caching-2024-07-31"),
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    transport.request({"model": "claude-test", "messages": [], "max_tokens": 8}, timeout_seconds=2)
    assert seen[0].url == "https://anthropic.test/v1/messages"
    assert seen[0].headers["x-api-key"] == "sk-ant-test"
    assert seen[0].headers["anthropic-version"] == "2024-01-01"
    assert seen[0].headers["anthropic-beta"] == "prompt-caching-2024-07-31"
    assert seen[0].headers["content-type"] == "application/json"
    assert seen[0].headers["accept"] == "application/json"

    bearer = AnthropicMessagesTransport(
        base_url="https://anthropic.test",
        auth_header="Authorization",
        bearer_token="token",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    bearer.request({"model": "claude-test", "messages": [], "max_tokens": 8}, timeout_seconds=2)
    assert seen[-1].headers["authorization"] == "Bearer token"
    assert "x-api-key" not in seen[-1].headers


def test_declared_request_headers_reach_the_wire_resolved_per_request() -> None:
    """A provider can declare headers its gateway requires on every request.

    They travel as the SDK's ``extra_headers`` request option -- so the JSON body
    stays clean -- and a ``{session_id}`` value is resolved from the request
    rather than frozen when the client was built.
    """
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"type": "message", "content": [], "stop_reason": "end_turn"})

    transport = AnthropicMessagesTransport(
        base_url="https://gateway.test", api_key="sk-gateway", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    provider = AnthropicMessagesProvider(
        transport=transport,
        extra_request_headers={"x-opencode-session": "{session_id}", "x-opencode-client": "voidcode"},
    )
    segments = (ProviderContextSegment(role="user", content="answer"),)

    provider.propose_turn(_request(segments=segments, session_id="conversation-a"))
    assert seen[-1].headers["x-opencode-session"] == "conversation-a"
    assert seen[-1].headers["x-opencode-client"] == "voidcode"

    provider.propose_turn(_request(segments=segments, session_id="conversation-b"))
    assert seen[-1].headers["x-opencode-session"] == "conversation-b"

    # No session id: the header names nothing, so it is omitted -- not sent empty.
    provider.propose_turn(_request(segments=segments))
    assert "x-opencode-session" not in seen[-1].headers
    assert seen[-1].headers["x-opencode-client"] == "voidcode"

    body = json.dumps(json.loads(seen[-1].content))
    assert "x-opencode-session" not in body
    assert "x-opencode-client" not in body


def test_gateway_request_headers_are_only_sent_by_the_provider_that_declares_them() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"type": "message", "content": [], "stop_reason": "end_turn"})

    transport = AnthropicMessagesTransport(
        base_url="https://api.anthropic.com", api_key="sk-ant-test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )

    AnthropicMessagesProvider(transport=transport).propose_turn(
        _request(segments=(ProviderContextSegment(role="user", content="answer"),), session_id="conversation-a")
    )

    assert "x-opencode-session" not in seen[-1].headers
    assert "x-opencode-client" not in seen[-1].headers


def test_native_messages_wire_uses_system_and_anthropic_tool_blocks() -> None:
    result = ToolResult(
        tool_name="read", status="ok", content="contents", data={"path": "sample.txt", "tool_call_id": "call-1", "arguments": {"path": "sample.txt"}}
    )
    segments = (
        ProviderContextSegment(role="system", content="Be concise."),
        ProviderContextSegment(role="user", content="Read sample.txt"),
        ProviderContextSegment(role="assistant", content=None, tool_name="read", tool_call_id="call-1", tool_arguments={"path": "sample.txt"}),
        ProviderContextSegment(
            role="tool", content=result.content, tool_name="read", tool_call_id="call-1", metadata={"status": "ok", "data": result.data}
        ),
    )
    definition = ToolDefinition(name="read", description="Read files", input_schema={"type": "object", "properties": {"path": {"type": "string"}}})
    fake = _FakeTransport()
    AnthropicMessagesProvider(transport=fake).propose_turn(_request(segments=segments, tools=(definition,)))
    payload = fake.payloads[0]
    assert payload["system"] == "Be concise."
    assert payload["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Read sample.txt"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "sample.txt"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "contents"}]},
    ]
    assert payload["tools"] == [{"name": "read", "description": "Read files", "input_schema": definition.input_schema}]
    assert payload["tool_choice"] == {"type": "auto"}
    assert "function" not in json.dumps(payload)


def test_nonstream_text_thinking_tool_usage_and_stop_reason() -> None:
    fake = _FakeTransport(
        response={
            "id": "msg_1",
            "type": "message",
            "model": "claude-test",
            "content": [
                {"type": "thinking", "thinking": "private", "signature": "opaque"},
                {"type": "text", "text": "visible"},
                {"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "a.txt"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2},
        }
    )
    definition = ToolDefinition(name="read", description="Read", input_schema={"type": "object"})
    result = AnthropicMessagesProvider(transport=fake).propose_turn(
        _request(segments=(ProviderContextSegment(role="user", content="read"),), tools=(definition,), effort="high")
    )
    assert result.output == "visible"
    assert result.reasoning == "private"
    assert result.tool_calls[0].tool_name == "read"
    assert result.tool_calls[0].arguments == {"path": "a.txt"}
    assert result.done_reason == "tool_calls"
    assert result.finish_reason_reported is True
    assert result.usage == ProviderTokenUsage(input_tokens=13, output_tokens=4, cache_read_tokens=3, cache_write_tokens=2, uncached_input_tokens=10)
    assert fake.payloads[0]["thinking"] == {"type": "enabled", "budget_tokens": 8192}


@pytest.mark.parametrize(
    ("usage_payload", "expected"),
    [
        ({"input_tokens": 4, "cache_read_input_tokens": 0}, ProviderTokenUsage(input_tokens=4, cache_read_tokens=0, uncached_input_tokens=4)),
        ({"input_tokens": 4}, ProviderTokenUsage(input_tokens=4, uncached_input_tokens=4)),
        ({"cache_read_input_tokens": 3}, ProviderTokenUsage(input_tokens=None, cache_read_tokens=3, uncached_input_tokens=None)),
        ({"input_tokens": 4, "cache_read_input_tokens": 3}, ProviderTokenUsage(input_tokens=7, cache_read_tokens=3, uncached_input_tokens=4)),
    ],
)
def test_anthropic_usage_preserves_missing_zero_and_cache_dimensions(usage_payload: dict[str, int], expected: ProviderTokenUsage) -> None:
    fake = _FakeTransport(response={"type": "message", "content": [], "stop_reason": "end_turn", "usage": usage_payload})
    result = AnthropicMessagesProvider(transport=fake).propose_turn(_request(segments=(ProviderContextSegment(role="user", content="answer"),)))
    assert result.usage == expected


def test_stream_emits_thinking_text_tool_lifecycle_usage_and_message_stop() -> None:
    fake = _FakeTransport(
        events=(
            {"type": "message_start", "message": {"id": "msg_2", "model": "claude-test", "usage": {"input_tokens": 7}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "private"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "answer"}},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "call-1", "name": "read", "input": {}}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '"a.txt"}'}},
            {"type": "content_block_stop", "index": 2},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5, "cache_read_input_tokens": 2}},
            {"type": "message_stop"},
        )
    )
    definition = ToolDefinition(name="read", description="Read", input_schema={"type": "object"})
    events = list(
        AnthropicMessagesProvider(transport=fake).stream_turn(
            _request(segments=(ProviderContextSegment(role="user", content="read"),), tools=(definition,))
        )
    )
    assert [event.kind for event in events] == ["delta", "delta", "tool_call_start", "tool_call_delta", "tool_call_delta", "tool_call_end", "done"]
    assert events[0] == ProviderStreamEvent(kind="delta", channel="reasoning", text="private", metadata={"source": "delta.thinking"})
    assert events[1] == ProviderStreamEvent(kind="delta", channel="text", text="answer")
    assert events[5].parsed_arguments == {"path": "a.txt"}
    assert events[-1].done_reason == "tool_calls"
    assert events[-1].usage == ProviderTokenUsage(input_tokens=9, output_tokens=5, cache_read_tokens=2, uncached_input_tokens=7)


@pytest.mark.parametrize(
    "events", [({"type": "message_start", "message": {}},), ({"type": "message_start", "message": {}}, {"type": "message_stop"})]
)
def test_stream_requires_stop_reason_and_message_stop(events: tuple[dict[str, object], ...]) -> None:
    provider = AnthropicMessagesProvider(transport=_FakeTransport(events=events))
    with pytest.raises(ProviderExecutionError, match="terminal message_stop/stop_reason"):
        list(provider.stream_turn(_request(segments=(ProviderContextSegment(role="user", content="answer"),))))


def test_stream_rejects_incomplete_tool_json_and_provider_errors() -> None:
    events = (
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call-1", "name": "read", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )
    definition = ToolDefinition(name="read", description="Read", input_schema={"type": "object"})
    with pytest.raises(ProviderExecutionError, match="incomplete tool-call arguments"):
        list(
            AnthropicMessagesProvider(transport=_FakeTransport(events=events)).stream_turn(
                _request(segments=(ProviderContextSegment(role="user", content="read"),), tools=(definition,))
            )
        )

    error = AnthropicTransportError(
        {"type": "error", "error": {"message": "secret sk-ant-should-not-leak", "code": "rate_limit"}, "status_code": 429}
    )

    class _ErrorTransport:
        def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object:
            _ = payload, timeout_seconds
            raise error

    with pytest.raises(ProviderExecutionError, match="secret") as exc_info:
        list(
            AnthropicMessagesProvider(transport=_ErrorTransport()).stream_turn(
                _request(segments=(ProviderContextSegment(role="user", content="answer"),))
            )
        )
    assert exc_info.value.kind == "rate_limit"
    assert "sk-ant-should-not-leak" not in str(exc_info.value)


@dataclass(frozen=True)
class _Cancelled:
    cancelled: bool


def test_abort_emits_cancelled_error_and_done() -> None:
    events = list(
        AnthropicMessagesProvider(transport=_FakeTransport()).stream_turn(
            _request(segments=(ProviderContextSegment(role="user", content="answer"),), abort_signal=_Cancelled(True))
        )
    )
    assert events == [
        ProviderStreamEvent(kind="error", channel="error", error="provider stream cancelled", error_kind="cancelled"),
        ProviderStreamEvent(kind="done", done_reason="cancelled"),
    ]


def test_stream_timeout_closes_iterator() -> None:
    class _Slow:
        def __iter__(self):
            return self

        def __next__(self):
            time.sleep(0.05)
            raise StopIteration

        def close(self) -> None:
            self.closed = True

    class _SlowTransport:
        def __init__(self) -> None:
            self.stream = _Slow()

        def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object:
            _ = payload, timeout_seconds
            return self.stream

    transport = _SlowTransport()
    provider = AnthropicMessagesProvider(config=AnthropicProviderConfig(timeout_seconds=0.01), transport=transport)
    with pytest.raises(ProviderExecutionError, match="chunk timeout"):
        list(provider.stream_turn(_request(segments=(ProviderContextSegment(role="user", content="answer"),))))
    assert getattr(transport.stream, "closed", False) is True


def _cache_tools() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(name="read", description="Read files", input_schema={"type": "object"}),
        ToolDefinition(name="glob", description="Find files", input_schema={"type": "object"}),
    )


@pytest.mark.parametrize(("retention", "expected_ttl"), [("short", "5m"), ("long", "1h")])
def test_prompt_cache_retention_marks_last_tool_with_ttl(retention: ProviderCacheRetention, expected_ttl: str) -> None:
    fake = _FakeTransport()
    AnthropicMessagesProvider(transport=fake).propose_turn(
        _request(
            segments=(ProviderContextSegment(role="system", content="Be concise."), ProviderContextSegment(role="user", content="read")),
            tools=_cache_tools(),
            cache_retention=retention,
        )
    )

    payload = fake.payloads[0]
    tools = cast(list[dict[str, object]], payload["tools"])
    assert [tool["name"] for tool in tools] == ["read", "glob"]
    assert "cache_control" not in tools[0]
    assert tools[-1]["cache_control"] == {"type": "ephemeral", "ttl": expected_ttl}
    # The tool prefix carries the marker, so the system prompt stays a plain string.
    assert payload["system"] == "Be concise."


def test_prompt_cache_retention_marks_system_block_when_no_tools_are_present() -> None:
    fake = _FakeTransport()
    AnthropicMessagesProvider(transport=fake).propose_turn(
        _request(
            segments=(ProviderContextSegment(role="system", content="Be concise."), ProviderContextSegment(role="user", content="answer")),
            cache_retention="long",
        )
    )

    payload = fake.payloads[0]
    assert "tools" not in payload
    assert payload["system"] == [{"type": "text", "text": "Be concise.", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]


def test_prompt_cache_retention_none_leaves_payload_unmarked() -> None:
    fake = _FakeTransport()
    AnthropicMessagesProvider(transport=fake).propose_turn(
        _request(
            segments=(ProviderContextSegment(role="system", content="Be concise."), ProviderContextSegment(role="user", content="read")),
            tools=_cache_tools(),
        )
    )

    payload = fake.payloads[0]
    tools = cast(list[dict[str, object]], payload["tools"])
    assert all("cache_control" not in tool for tool in tools)
    assert payload["system"] == "Be concise."


def test_prompt_cache_retention_falls_back_to_configured_default() -> None:
    fake = _FakeTransport()
    provider = AnthropicMessagesProvider(config=AnthropicProviderConfig(cache_retention="short"), transport=fake)
    provider.propose_turn(
        _request(segments=(ProviderContextSegment(role="system", content="Be concise."), ProviderContextSegment(role="user", content="answer")))
    )

    payload = fake.payloads[0]
    assert payload["system"] == [{"type": "text", "text": "Be concise.", "cache_control": {"type": "ephemeral", "ttl": "5m"}}]


def test_prompt_cache_retention_request_override_wins_over_configured_default() -> None:
    fake = _FakeTransport()
    provider = AnthropicMessagesProvider(config=AnthropicProviderConfig(cache_retention="short"), transport=fake)
    provider.propose_turn(
        _request(
            segments=(ProviderContextSegment(role="system", content="Be concise."), ProviderContextSegment(role="user", content="answer")),
            cache_retention="long",
        )
    )

    payload = fake.payloads[0]
    assert payload["system"] == [{"type": "text", "text": "Be concise.", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]


def test_usage_metadata_payload_reports_cache_read_write_and_hit_rate() -> None:
    fake = _FakeTransport(
        response={
            "type": "message",
            "content": [],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 20, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 5},
        }
    )
    result = AnthropicMessagesProvider(transport=fake).propose_turn(_request(segments=(ProviderContextSegment(role="user", content="answer"),)))

    usage = result.usage
    assert usage is not None
    assert usage.metadata_payload() == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 80,
        "cache_write_tokens": 5,
        "uncached_input_tokens": 20,
    }
    assert usage.cache_hit_rate == 0.8


@pytest.mark.parametrize("auth_header", ["x-api-key", "X-Api-Key", "X-API-KEY"])
def test_x_api_key_auth_header_is_not_stripped_by_the_sdk_omit_sentinel(auth_header: str) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"type": "message", "content": [], "stop_reason": "end_turn"})

    transport = AnthropicMessagesTransport(
        base_url="https://anthropic.test",
        api_key="sk-ant-real",
        auth_header=auth_header,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    transport.request({"model": "claude-test", "messages": [], "max_tokens": 8}, timeout_seconds=2)

    assert seen[0].headers["x-api-key"] == "sk-ant-real"


def _recording_sdk(built: list[dict[str, object]]) -> object:
    class _Messages:
        def create(self, **payload: object) -> dict[str, object]:
            _ = payload
            return {"id": "msg_1", "type": "message", "model": "claude-test", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}

    class _SDK:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)
            self.messages = _Messages()

    return _SDK


def test_provider_builds_one_sdk_client_for_many_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, object]] = []
    monkeypatch.setattr("voidcode.provider.anthropic_native.Anthropic", _recording_sdk(built))
    provider = AnthropicMessagesProvider(config=AnthropicProviderConfig(base_url="https://anthropic.test", api_key="sk-ant-test"))

    outputs = [provider.propose_turn(_request(segments=(ProviderContextSegment(role="user", content="answer"),))).output for _ in range(3)]

    assert outputs == ["ok", "ok", "ok"]
    assert len(built) == 1


def _sse_transport(body: str, seen: list[httpx2.Request]) -> AnthropicMessagesTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8"))

    return AnthropicMessagesTransport(
        base_url="https://anthropic.test", api_key="sk-ant-test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )


def test_sdk_stream_keeps_json_accept_and_maps_named_events() -> None:
    # The official client never sends ``Accept: text/event-stream``; SSE is
    # selected by the ``stream`` body field, so the SDK default is pinned here
    # (the request payload below carries the streaming flag).
    body = "\n\n".join(
        [
            (
                'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message",'
                '"role":"assistant","model":"claude-test","content":[],"stop_reason":null,"stop_sequence":null,'
                '"usage":{"input_tokens":5,"output_tokens":1}}}'
            ),
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
            'event: message_stop\ndata: {"type":"message_stop"}',
            "",
        ]
    )
    seen: list[httpx2.Request] = []
    events = list(
        AnthropicMessagesProvider(transport=_sse_transport(body, seen)).stream_turn(
            _request(segments=(ProviderContextSegment(role="user", content="answer"),))
        )
    )

    assert [event.kind for event in events] == ["delta", "done"]
    assert events[0].text == "hi"
    assert events[-1].done_reason == "stop"
    assert events[-1].usage == ProviderTokenUsage(input_tokens=5, output_tokens=2, uncached_input_tokens=5)
    assert seen[0].headers["accept"] == "application/json"
    assert json.loads(seen[0].content)["stream"] is True


def test_sdk_stream_drops_data_frames_without_a_recognized_event_name() -> None:
    # Documented limitation: the SDK's SSE reader only yields frames whose
    # ``event:`` name it recognizes, so a gateway that forwards bare ``data:``
    # frames produces no provider events at all.
    seen: list[httpx2.Request] = []
    body = 'data: {"type":"message_stop"}\n\n'
    provider = AnthropicMessagesProvider(transport=_sse_transport(body, seen))

    with pytest.raises(ProviderExecutionError, match="terminal message_stop/stop_reason"):
        list(provider.stream_turn(_request(segments=(ProviderContextSegment(role="user", content="answer"),))))


def test_sdk_http_error_payload_is_typed_and_redacted() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down sk-secret"}})

    transport = AnthropicMessagesTransport(
        base_url="https://anthropic.test", api_key="sk-ant-test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )

    with pytest.raises(ProviderExecutionError) as raised:
        AnthropicMessagesProvider(transport=transport).propose_turn(_request(segments=(ProviderContextSegment(role="user", content="answer"),)))

    assert raised.value.kind == "rate_limit"
    assert raised.value.retryable is True
    assert "sk-secret" not in raised.value.message
    assert "sk-secret" not in repr(raised.value.details)


def test_credential_headers_are_absent_when_keyless_and_exact_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # The constructor placeholder (``_PLACEHOLDER_API_KEY``) only satisfies the SDK
    # and must never reach the wire; an explicit credential also stops the SDK from
    # reading ambient ones (env, profile, workload identity). So a keyless transport
    # sends no credential header at all and a configured one sends exactly the key
    # it was given. (The provider layer, not the transport, reads the environment.)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient-should-not-be-used")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"type": "message", "content": [], "stop_reason": "end_turn"})

    def request_with(api_key: str | None) -> dict[str, str]:
        transport = AnthropicMessagesTransport(
            base_url="https://anthropic.test", api_key=api_key, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
        )
        transport.request({"model": "claude-test", "messages": [], "max_tokens": 8}, timeout_seconds=2)
        return dict(seen[-1].headers)

    keyless = request_with(None)
    assert keyless.get("x-api-key") is None
    assert keyless.get("authorization") is None
    assert not any("sk-ambient-should-not-be-used" in value for value in keyless.values())

    configured = request_with("sk-ant-real")
    assert configured["x-api-key"] == "sk-ant-real"
    assert configured.get("authorization") is None
