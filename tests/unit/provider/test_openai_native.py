from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import cast

import httpx2
import pytest

from voidcode.core.transcript import AssembledContext, ContextSegment, ContextWindow
from voidcode.provider.config import OpenAIProviderConfig
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.openai_native import OpenAIChatCompletionsProvider, OpenAIChatCompletionsTransport
from voidcode.provider.protocol import (
    ProviderAbortSignal,
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
    segments: tuple[ContextSegment, ...]
    metadata: dict[str, object]
    tool_results: tuple[object, ...] = ()
    continuity_state: object | None = None


def _request(*, transport: object | None, abort_signal: ProviderAbortSignal | None = None, session_id: str | None = None) -> ProviderTurnRequest:
    context = _Context(prompt="hello", segments=(ContextSegment(role="user", content="hello"),), metadata={})
    return ProviderTurnRequest(
        assembled_context=cast(AssembledContext, context),
        bounded_context_window=cast(ContextWindow, _ContextWindow(prompt="hello")),
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
        session_id=session_id,
        abort_signal=abort_signal,
    )


def test_native_deepseek_rejects_replay_without_original_reasoning_before_http() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        raise AssertionError("invalid replay reached HTTP")

    context = _Context(
        prompt="read sample.txt",
        metadata={},
        segments=(
            ContextSegment("user", "read sample.txt"),
            ContextSegment("assistant", None, tool_name="read", tool_call_id="call-read-original", tool_arguments={"path": "sample.txt"}),
            ContextSegment("tool", "sample", tool_name="read", tool_call_id="call-read-original", metadata={"data": {}}),
        ),
    )
    request = replace(
        _request(transport=None),
        provider_name="deepseek",
        model_name="deepseek-v4-pro",
        raw_model="deepseek/deepseek-v4-pro",
        reasoning_effort="high",
        assembled_context=cast(AssembledContext, context),
    )
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        provider = OpenAIModelProvider(
            name="deepseek",
            config=OpenAIProviderConfig(api_key="sk-test"),
            transport=OpenAIChatCompletionsTransport(api_key="sk-test", http_client=client),
        ).turn_provider()
        with pytest.raises(ProviderExecutionError) as captured:
            provider.propose_turn(request)
        assert captured.value.kind == "stream_tool_feedback_shape"
        assert captured.value.retryable is False
        assert captured.value.fallback_allowed is False
        assert requests == []


def test_native_non_stream_uses_chat_completions_wire_and_auth_headers() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx2.Response(
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
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
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
    assert result.usage is not None and result.usage.input_tokens == 7 and result.usage.output_tokens == 3


def test_nonstream_missing_finish_reason_is_stop_and_reported_reasons_fail() -> None:
    def provider_for(reason: object = ...) -> OpenAIModelProvider:
        payload: dict[str, object] = {"choices": [{"message": {"content": "hello"}}]}
        if reason is not ...:
            payload["choices"] = [{"message": {"content": "hello"}, "finish_reason": reason}]
        transport = OpenAIChatCompletionsTransport(
            api_key="sk-test",
            http_client=httpx2.Client(transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, json=payload))),
        )
        return OpenAIModelProvider(config=OpenAIProviderConfig(api_key="sk-test"), transport=transport)

    result = provider_for().turn_provider().propose_turn(_request(transport=None))
    assert result.done_reason == "stop"
    assert result.finish_reason_reported is False

    for reason in ("content_filter", "eos_token"):
        with pytest.raises(ProviderExecutionError, match=f"finish_reason: {reason}"):
            provider_for(reason).turn_provider().propose_turn(_request(transport=None))

    class StreamingOpenAIProvider(OpenAIModelProvider):
        def turn_provider(self) -> OpenAIChatCompletionsProvider:
            return OpenAIChatCompletionsProvider(config=self.provider_config(), transport=self.transport)

    for reason, expected in ((None, "stop"), ("content_filter", None), ("eos_token", None)):
        payload: dict[str, object] = {"choices": [{"delta": {"content": "hello"}, "finish_reason": reason}]}
        if reason is None:
            payload["choices"] = [{"delta": {"content": "hello"}}]
        response_body = b"data: " + json.dumps(payload).encode() + b"\n\ndata: [DONE]\n\n"
        transport = OpenAIChatCompletionsTransport(
            api_key="sk-test",
            http_client=httpx2.Client(
                transport=httpx2.MockTransport(
                    lambda _request, body=response_body: httpx2.Response(
                        200,
                        content=body,
                        headers={"content-type": "text/event-stream"},
                    )
                )
            ),
        )
        provider = StreamingOpenAIProvider(config=OpenAIProviderConfig(api_key="sk-test"), transport=transport)
        stream_provider = provider.turn_provider()
        if expected is None:
            with pytest.raises(ProviderExecutionError, match=f"finish_reason: {reason}"):
                list(stream_provider.stream_turn(_request(transport=None)))
        else:
            events = list(stream_provider.stream_turn(_request(transport=None)))
            assert events[-1].done_reason == expected
            assert events[-1].metadata == {"finish_reason_reported": False}
