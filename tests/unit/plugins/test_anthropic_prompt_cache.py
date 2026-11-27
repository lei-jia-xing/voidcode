"""Unit tests for Anthropic prompt cache breakpoints across tools, system, and multi-turn history."""

from __future__ import annotations

from voidcode.capabilities.providers.native.anthropic_native import AnthropicMessagesProvider
from voidcode.sdk.provider import ProviderMessage, ProviderTurnRequest
from voidcode.sdk.tools import ToolCall, ToolDefinition


def _tool_def(name: str = "read") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Read a file.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )


def test_anthropic_caches_both_tools_and_system() -> None:
    provider = AnthropicMessagesProvider()
    req = ProviderTurnRequest(
        messages=(
            ProviderMessage(role="system", content="You are VoidCode."),
            ProviderMessage(role="user", content="hello"),
        ),
        available_tools=(_tool_def("read"), _tool_def("grep")),
        model_name="claude-sonnet-4-5",
        provider_name="anthropic",
    )
    payload = provider._payload(req, stream=False)

    tools = payload["tools"]
    assert isinstance(tools, list) and len(tools) == 2
    assert "cache_control" not in tools[0]
    assert tools[-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert payload["system"] == [{"type": "text", "text": "You are VoidCode.", "cache_control": {"type": "ephemeral", "ttl": "5m"}}]
    # Single-turn message blocks stay unmarked.
    messages = payload["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    assert "cache_control" not in messages[0]["content"][0]


def test_anthropic_places_rolling_breakpoints_on_multiturn_history() -> None:
    provider = AnthropicMessagesProvider()
    req = ProviderTurnRequest(
        messages=(
            ProviderMessage(role="system", content="System rules"),
            ProviderMessage(role="user", content="step 1"),
            ProviderMessage(role="assistant", tool_calls=(ToolCall("read", {"path": "a.py"}, "call-1"),)),
            ProviderMessage(role="tool", content="file a", tool_call_id="call-1", tool_name="read"),
            ProviderMessage(role="assistant", tool_calls=(ToolCall("read", {"path": "b.py"}, "call-2"),)),
            ProviderMessage(role="tool", content="file b", tool_call_id="call-2", tool_name="read"),
        ),
        available_tools=(_tool_def(),),
        model_name="claude-sonnet-4-5",
        provider_name="anthropic",
        cache_retention="long",
    )
    payload = provider._payload(req, stream=False)

    expected_cc = {"type": "ephemeral", "ttl": "1h"}
    tools = payload["tools"]
    assert isinstance(tools, list)
    assert tools[-1]["cache_control"] == expected_cc
    assert payload["system"] == [{"type": "text", "text": "System rules", "cache_control": expected_cc}]

    messages = payload["messages"]
    assert isinstance(messages, list) and len(messages) == 5
    # Breakpoints stay bounded (<= 4 total): prior turn (-3 = index 2) and current tail (-1 = index 4).
    assert "cache_control" not in messages[0]["content"][-1]
    assert "cache_control" not in messages[1]["content"][-1]
    assert messages[2]["content"][-1]["cache_control"] == expected_cc
    assert "cache_control" not in messages[3]["content"][-1]
    assert messages[4]["content"][-1]["cache_control"] == expected_cc


def test_anthropic_skips_thinking_and_empty_text_when_marking_cache_breakpoints() -> None:
    provider = AnthropicMessagesProvider()
    req = ProviderTurnRequest(
        messages=(
            ProviderMessage(role="user", content="start"),
            ProviderMessage(
                role="assistant",
                content="checking",
                metadata={"thinking_blocks": [{"type": "thinking", "thinking": "ponder", "signature": "sig-1"}]},
            ),
            ProviderMessage(
                role="assistant",
                content="",
                metadata={"thinking_blocks": [{"type": "thinking", "thinking": "only thinking", "signature": "sig-2"}]},
            ),
        ),
        model_name="claude-sonnet-4-5",
        provider_name="anthropic",
    )
    payload = provider._payload(req, stream=False)
    messages = payload["messages"]
    assert isinstance(messages, list) and len(messages) == 2

    assistant_blocks = messages[1]["content"]
    assert isinstance(assistant_blocks, list)
    # Thinking blocks must stay unmodified; text block gets the breakpoint.
    for block in assistant_blocks:
        if block.get("type") in {"thinking", "redacted_thinking"}:
            assert "cache_control" not in block
    assert assistant_blocks[1] == {"type": "text", "text": "checking", "cache_control": {"type": "ephemeral", "ttl": "5m"}}
    assert messages[0]["content"][-1] == {"type": "text", "text": "start", "cache_control": {"type": "ephemeral", "ttl": "5m"}}


def test_anthropic_respects_cache_retention_none() -> None:
    provider = AnthropicMessagesProvider()
    req = ProviderTurnRequest(
        messages=(
            ProviderMessage(role="system", content="System rules"),
            ProviderMessage(role="user", content="first"),
            ProviderMessage(role="assistant", content="second"),
        ),
        available_tools=(_tool_def(),),
        model_name="claude-sonnet-4-5",
        provider_name="anthropic",
        cache_retention="none",
    )
    payload = provider._payload(req, stream=False)
    tools = payload["tools"]
    assert isinstance(tools, list)
    assert "cache_control" not in tools[-1]
    assert isinstance(payload["system"], str)
    messages = payload["messages"]
    assert isinstance(messages, list)
    for msg in messages:
        for block in msg["content"]:
            assert "cache_control" not in block
