"""Behavior tests for the providers plugin dispatch, pricing and reasoning rules.

The plugin owns provider policy; these tests drive it over a real loopback HTTP
server so the asserted facts are wire facts, not internal calls.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.capabilities.providers.dispatch import RegisteredProvider, build_registered_providers
from voidcode.capabilities.providers.pricing import price_usage
from voidcode.sdk.provider import (
    ProviderExecutionError,
    ProviderMessage,
    ProviderTurnRequest,
)
from voidcode.sdk.tools import ToolCall, ToolDefinition


@pytest.fixture
def loopback_server() -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer([])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _provider(entries: dict[str, object], *, name: str = "deepseek") -> RegisteredProvider:
    providers = {provider.name: provider for provider in build_registered_providers({"providers": entries})}
    assert name in providers
    return providers[name]


def _base_url(server: OpenAILoopbackServer) -> str:
    return f"http://127.0.0.1:{server.server_port}/v1"


def _tool() -> ToolDefinition:
    return ToolDefinition(name="read", description="Read a file", input_schema={"type": "object", "properties": {"path": {"type": "string"}}})


def _request(*messages: ProviderMessage, **overrides: object) -> ProviderTurnRequest:
    fields: dict[str, object] = {
        "messages": messages,
        "raw_model": "deepseek/deepseek-chat",
        "provider_name": "deepseek",
        "model_name": "deepseek-chat",
        "reasoning_effort": None,
    }
    fields.update(overrides)
    return ProviderTurnRequest(**fields)  # type: ignore[arg-type]


def test_flat_core_entries_are_mapped_into_vendor_shapes() -> None:
    """google/copilot nest their credential under ``auth``; the core sends it flat."""
    entries: dict[str, object] = {
        "google": {"api_key": "k", "base_url": "https://example.invalid"},
        "github-copilot": {"api_key": "t", "base_url": "https://example.invalid"},
    }
    providers = {provider.name: provider for provider in build_registered_providers({"providers": entries})}

    assert providers["google"]._provider_for("google") is not None
    assert providers["github-copilot"]._provider_for("github-copilot") is not None


def test_unconfigured_provider_is_refused_without_a_request(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider({"deepseek": {"api_key": "key", "base_url": _base_url(loopback_server)}})

    with pytest.raises(ProviderExecutionError) as excinfo:
        provider.propose_turn(_request(ProviderMessage("user", "hi"), provider_name="ghost"))

    assert excinfo.value.kind == "not_configured"
    assert loopback_server.requests == []


def test_missing_provider_name_is_refused(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider({"deepseek": {"api_key": "key", "base_url": _base_url(loopback_server)}})

    with pytest.raises(ProviderExecutionError) as excinfo:
        provider.propose_turn(_request(ProviderMessage("user", "hi"), provider_name=None))

    assert excinfo.value.kind == "not_configured"
    assert loopback_server.requests == []


def test_tool_call_replay_sends_reasoning_and_omits_tool_choice(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider({"deepseek": {"api_key": "key", "base_url": _base_url(loopback_server)}})
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))
    request = _request(
        ProviderMessage("user", "read a"),
        ProviderMessage("assistant", reasoning="authentic-thought", tool_calls=(ToolCall("read", {"path": "a"}, "call-1"),)),
        ProviderMessage("tool", content="ok", tool_call_id="call-1", tool_name="read"),
        available_tools=(_tool(),),
    )

    result = provider.propose_turn(request)

    assert result.output == "done"
    wire_request = loopback_server.requests[0]
    assistant = next(message for message in wire_request["messages"] if message["role"] == "assistant")
    assert assistant["reasoning_content"] == "authentic-thought"
    assert "tool_choice" not in wire_request


def test_tool_choice_is_auto_for_a_model_that_does_not_require_reasoning(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider({"zai": {"api_key": "key", "base_url": _base_url(loopback_server)}}, name="zai")
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))

    result = provider.propose_turn(
        _request(
            ProviderMessage("user", "hi"),
            available_tools=(_tool(),),
            raw_model="zai/glm-4.5",
            provider_name="zai",
            model_name="glm-4.5",
        )
    )

    assert result.output == "done"
    wire_request = loopback_server.requests[0]
    assert wire_request["tool_choice"] == "auto"
    assert wire_request["tools"][0]["function"]["name"] == "read"


def test_usage_none_and_zero_are_distinct_and_unknown_cost_stays_none(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider({"deepseek": {"api_key": "key", "base_url": _base_url(loopback_server)}})
    loopback_server.replies.append(
        openai_response(
            message={"content": "done"},
            finish="stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0}},
        )
    )

    result = provider.propose_turn(_request(ProviderMessage("user", "hi")))

    assert result.usage is not None
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    # deepseek-chat ships no catalog row: the cost must stay unknown, never 0.
    assert result.usage.cost_usd is None


def test_catalog_cost_is_attached_once(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider({"zai": {"api_key": "key", "base_url": _base_url(loopback_server)}}, name="zai")
    loopback_server.replies.append(
        openai_response(
            message={"content": "done"},
            finish="stop",
            usage={"prompt_tokens": 1000, "completion_tokens": 500, "prompt_tokens_details": {"cached_tokens": 100}},
        )
    )

    result = provider.propose_turn(
        _request(
            ProviderMessage("user", "hi"),
            raw_model="zai/glm-4.5",
            provider_name="zai",
            model_name="glm-4.5",
        )
    )

    assert result.usage is not None
    # 900 uncached * 0.6/M + 500 out * 2.2/M + 100 cached * 0.11/M
    assert result.usage.cost_usd == pytest.approx(0.001651)
    assert price_usage(provider_name="zai", model_name="glm-4.5", usage=result.usage) is result.usage


def test_streaming_yields_deltas_and_a_terminal_event(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider({"deepseek": {"api_key": "key", "base_url": _base_url(loopback_server)}})
    loopback_server.replies.append(openai_response(message={"content": "hi there"}, finish="stop"))

    events = list(provider.stream_turn(_request(ProviderMessage("user", "hi"))))

    assert loopback_server.requests[0]["stream"] is True
    deltas = [event for event in events if event.kind == "delta" and event.channel == "text"]
    assert any(event.text == "hi there" for event in deltas)
    assert events[-1].kind == "done"
    assert events[-1].done_reason == "stop"


def test_custom_endpoint_auth_scheme_and_header_are_sent(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider(
        {
            "my-gateway": {
                "api_key": "loopback-key",
                "base_url": _base_url(loopback_server),
                "auth_header": "x-api-key",
                "auth_scheme": "token",
            }
        },
        name="endpoint",
    )
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))

    provider.propose_turn(
        _request(
            ProviderMessage("user", "hi"),
            raw_model="my-gateway/some-model",
            provider_name="my-gateway",
            model_name="some-model",
        )
    )

    headers = loopback_server.request_headers[0]
    assert headers["x-api-key"] == "loopback-key"
    assert "Authorization" not in headers


def test_opaque_metadata_does_not_select_a_different_tool_protocol(loopback_server):
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))
    provider = _provider({"endpoint": {"api_key": "loopback-key", "base_url": _base_url(loopback_server)}}, name="endpoint")
    request = _request(
        ProviderMessage("user", "request"),
        ProviderMessage("assistant", tool_calls=(ToolCall("read", {"path": "a"}, tool_call_id="call-1"),)),
        ProviderMessage("tool", "body", tool_name="read", tool_call_id="call-1"),
        provider_name="endpoint",
        model_name="model",
        raw_model="endpoint/model",
        available_tools=(_tool(),),
        metadata={"tool_feedback_mode": "synthetic_user_message"},
    )
    try:
        provider.propose_turn(request)
        assert [message["role"] for message in loopback_server.requests[0]["messages"]] == ["user", "assistant", "tool"]
    finally:
        provider.close()
