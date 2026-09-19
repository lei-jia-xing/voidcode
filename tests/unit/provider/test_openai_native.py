from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

import httpx2

from voidcode.provider.config import (
    OpenAIProviderConfig,
)
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.openai_native import OpenAIChatCompletionsTransport
from voidcode.provider.protocol import (
    ProviderAssembledContext,
    ProviderContextSegment,
    ProviderContextWindow,
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


def _request(*, transport: object | None, abort_signal: object | None = None, session_id: str | None = None) -> ProviderTurnRequest:
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
        session_id=session_id,
        abort_signal=cast(object, abort_signal),
    )


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
    assert result.usage is not None and result.usage.total_tokens == 10
