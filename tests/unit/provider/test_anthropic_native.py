from __future__ import annotations

from dataclasses import dataclass

from voidcode.provider.anthropic_native import AnthropicMessagesProvider, AnthropicMessagesTransport
from voidcode.provider.config import AnthropicProviderConfig
from voidcode.provider.protocol import (
    ProviderAbortSignal,
    ProviderCacheRetention,
    ProviderContextSegment,
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


def test_unconfigured_vendor_never_borrows_the_ambient_anthropic_key(monkeypatch) -> None:
    """One shared adapter serves every Anthropic-wire vendor, so an ambient key
    resolved for ``anthropic`` must not travel to another vendor's host."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")

    transport = AnthropicMessagesProvider(name="kimi-coding")._transport()

    assert isinstance(transport, AnthropicMessagesTransport)
    assert transport.base_url == "https://api.kimi.com/coding"
    assert transport.api_key is None


def test_configured_vendor_sends_only_its_own_credential() -> None:
    transport = AnthropicMessagesProvider(
        name="minimax-cn",
        config=AnthropicProviderConfig(api_key="vendor-key"),
    )._transport()

    assert isinstance(transport, AnthropicMessagesTransport)
    assert transport.base_url == "https://api.minimaxi.com/anthropic"
    assert transport.api_key == "vendor-key"
