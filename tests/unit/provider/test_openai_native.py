from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

import httpx
import pytest

from voidcode.provider.config import OpenAIProviderConfig
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.openai_native import OpenAIChatCompletionsTransport
from voidcode.provider.protocol import (
    ProviderAssembledContext,
    ProviderContextSegment,
    ProviderContextWindow,
    ProviderExecutionError,
    ProviderTurnRequest,
)
from voidcode.tools.contracts import ToolDefinition


@dataclass(frozen=True, slots=True)
class _ContextWindow:
    prompt: str
    tool_results: tuple[object, ...] = ()
    compacted: bool = False
    retained_tool_result_count: int = 0
    continuity_state: object | None = None


@dataclass(frozen=True, slots=True)
class _Context:
    prompt: str
    segments: tuple[ProviderContextSegment, ...]
    metadata: dict[str, object]
    tool_results: tuple[object, ...] = ()
    continuity_state: object | None = None


def _request(*, transport: object | None, abort_signal: object | None = None) -> ProviderTurnRequest:
    context = _Context(prompt="hello", segments=(ProviderContextSegment(role="user", content="hello"),), metadata={})
    return ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        bounded_context_window=cast(ProviderContextWindow, _ContextWindow(prompt="hello")),
        available_tools=(
            ToolDefinition(
                name="read",
                description="read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        ),
        provider_name="openai",
        model_name="gpt-4o",
        raw_model="openai/gpt-4o",
        abort_signal=cast(object, abort_signal),
    )


def test_native_non_stream_uses_chat_completions_wire_and_auth_headers() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "gpt-4o",
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    transport = OpenAIChatCompletionsTransport(
        api_key="sk-test",
        organization="org-test",
        project="proj-test",
        http_transport=httpx.MockTransport(handler),
    )
    result = (
        OpenAIModelProvider(config=OpenAIProviderConfig(api_key="sk-test"), transport=transport)
        .turn_provider()
        .propose_turn(_request(transport=transport))
    )
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    headers = cast(dict[str, str], seen["headers"])
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["openai-organization"] == "org-test"
    assert headers["openai-project"] == "proj-test"
    payload = cast(dict[str, object], seen["payload"])
    assert payload["model"] == "gpt-4o"
    assert payload["stream"] is False
    assert result.output == "hello"
    assert result.done_reason == "stop"
    assert result.usage is not None and result.usage.total_tokens == 10


def test_native_stream_emits_text_tool_lifecycle_and_trailing_usage() -> None:
    body = "\n".join(
        [
            'data: {"id":"chatcmpl-2","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
            (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
                '"function":{"name":"read","arguments":"{\\"path\\":\\"a"}}]},'
                '"finish_reason":null}]}'
            ),
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":".txt\\"}"}}]},"finish_reason":"tool_calls"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":4}}',
            "data: [DONE]",
            "",
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    transport = OpenAIChatCompletionsTransport(http_transport=httpx.MockTransport(handler))
    events = list(OpenAIModelProvider(transport=transport).turn_provider().stream_turn(_request(transport=transport)))
    assert [(event.kind, event.channel) for event in events] == [
        ("delta", "text"),
        ("tool_call_start", "tool"),
        ("tool_call_delta", "tool"),
        ("tool_call_delta", "tool"),
        ("tool_call_end", "tool"),
        ("done", "text"),
    ]
    assert events[-1].done_reason == "tool_calls"
    assert events[-1].usage is not None and events[-1].usage.output_tokens == 4
    assert events[-2].parsed_arguments == {"path": "a.txt"}


def test_native_stream_abort_before_first_event_never_calls_transport() -> None:
    class _Abort:
        cancelled = True

    class _FailingTransport:
        def request(self, _payload: dict[str, object], *, timeout_seconds: float) -> object:
            raise AssertionError("transport must not be called after abort")

    events = list(
        OpenAIModelProvider(transport=cast(object, _FailingTransport())).turn_provider().stream_turn(_request(transport=None, abort_signal=_Abort()))
    )
    assert events[0].error_kind == "cancelled"
    assert events[-1].done_reason == "cancelled"


def test_native_http_error_is_typed_and_redacted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down", "code": "rate_limit"}, "token": "sk-secret"})

    transport = OpenAIChatCompletionsTransport(http_transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderExecutionError) as raised:
        OpenAIModelProvider(transport=transport).turn_provider().propose_turn(_request(transport=transport))
    assert raised.value.kind == "rate_limit"
    assert "sk-secret" not in repr(raised.value.details)


def test_openai_provider_does_not_invoke_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    import voidcode.provider.litellm_backend as backend

    monkeypatch.setattr(backend, "litellm_module", None)
    transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
        )
    )
    result = OpenAIModelProvider(transport=transport).turn_provider().propose_turn(_request(transport=transport))
    assert result.output == "ok"


def test_native_stream_eof_without_finish_reason_is_not_success() -> None:
    class _Transport:
        def request(self, _payload: dict[str, object], *, timeout_seconds: float) -> object:
            return iter(({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]},))

    with pytest.raises(ProviderExecutionError, match="without finish_reason") as raised:
        list(OpenAIModelProvider(transport=cast(object, _Transport())).turn_provider().stream_turn(_request(transport=None)))
    assert raised.value.kind == "transient_failure"


def test_native_stream_first_event_timeout_is_typed_and_closes_stream() -> None:
    class _ClosableStream:
        def __init__(self) -> None:
            self.closed = False

        def __iter__(self) -> _ClosableStream:
            return self

        def __next__(self) -> dict[str, object]:
            import time

            time.sleep(0.05)
            if self.closed:
                raise StopIteration
            return {"choices": [{"delta": {"content": "late"}, "finish_reason": "stop"}]}

        def close(self) -> None:
            self.closed = True

    class _Transport:
        def __init__(self) -> None:
            self.stream = _ClosableStream()

        def request(self, _payload: dict[str, object], *, timeout_seconds: float) -> object:
            return self.stream

    transport = _Transport()
    provider = OpenAIModelProvider(config=OpenAIProviderConfig(timeout_seconds=0.01), transport=cast(object, transport)).turn_provider()
    with pytest.raises(ProviderExecutionError, match="chunk timeout") as raised:
        list(provider.stream_turn(_request(transport=None)))
    assert raised.value.kind == "transient_failure"
    assert transport.stream.closed


def test_native_usage_never_reports_negative_uncached_tokens() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 4}},
            },
        )

    transport = OpenAIChatCompletionsTransport(http_transport=httpx.MockTransport(handler))
    result = OpenAIModelProvider(transport=transport).turn_provider().propose_turn(_request(transport=transport))
    assert result.usage is not None and result.usage.uncached_input_tokens == 0
