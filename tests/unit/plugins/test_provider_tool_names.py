"""The native wires share one tool-name mapping and one length cap.

Chat Completions and Messages both require tool names to match
``^[a-zA-Z0-9_-]{1,64}$``. These tests build the real payloads from both
adapters and compare the names they serialize, rather than inspecting source
text or matching substrings.
"""

from __future__ import annotations

import re

from voidcode.capabilities.providers.native._wire_common import (
    _MAX_TOOL_NAME_LENGTH,
    safe_tool_name,
    tool_name_maps,
)
from voidcode.capabilities.providers.native.anthropic_native import AnthropicMessagesProvider
from voidcode.capabilities.providers.native.openai_native import OpenAIChatCompletionsProvider
from voidcode.sdk.provider import ProviderMessage, ProviderTurnRequest
from voidcode.sdk.tools import ToolCall, ToolDefinition

_WIRE_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
_INVALID_TOOL_NAME = "mcp:server/tool"
_OVERLONG_TOOL_NAME = "overlong_" + "x" * _MAX_TOOL_NAME_LENGTH
_SANITIZED_INVALID_NAME = safe_tool_name(_INVALID_TOOL_NAME)
_HISTORY_TOOL_NAMES = (_INVALID_TOOL_NAME, _OVERLONG_TOOL_NAME, _SANITIZED_INVALID_NAME)


def _tools() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition("read", "Read a file", {"type": "object"}),
        ToolDefinition(_INVALID_TOOL_NAME, "MCP tool", {"type": "object"}),
        ToolDefinition(_OVERLONG_TOOL_NAME, "Overlong tool", {"type": "object"}),
        # A legal name equal to the sanitized form of the invalid name above:
        # the shared map must separate the two instead of serializing a
        # duplicate wire name.
        ToolDefinition(_SANITIZED_INVALID_NAME, "Colliding tool", {"type": "object"}),
    )


def _request(*, provider_name: str, model_name: str) -> ProviderTurnRequest:
    return ProviderTurnRequest(
        messages=(
            ProviderMessage("user", "use the tools"),
            ProviderMessage(
                "assistant",
                tool_calls=(
                    ToolCall(_INVALID_TOOL_NAME, {}, "call-invalid"),
                    ToolCall(_OVERLONG_TOOL_NAME, {}, "call-overlong"),
                    ToolCall(_SANITIZED_INVALID_NAME, {}, "call-colliding"),
                ),
            ),
            ProviderMessage("tool", "invalid", tool_call_id="call-invalid", tool_name=_INVALID_TOOL_NAME),
            ProviderMessage("tool", "overlong", tool_call_id="call-overlong", tool_name=_OVERLONG_TOOL_NAME),
            ProviderMessage("tool", "colliding", tool_call_id="call-colliding", tool_name=_SANITIZED_INVALID_NAME),
        ),
        available_tools=_tools(),
        provider_name=provider_name,
        model_name=model_name,
    )


def _openai_tool_names(payload: dict[str, object]) -> list[str]:
    tools = payload["tools"]
    assert isinstance(tools, list)
    names: list[str] = []
    for tool in tools:
        assert isinstance(tool, dict)
        function = tool["function"]
        assert isinstance(function, dict)
        name = function["name"]
        assert isinstance(name, str)
        names.append(name)
    return names


def _openai_call_names(payload: dict[str, object]) -> list[str]:
    messages = payload["messages"]
    assert isinstance(messages, list)
    names: list[str] = []
    for message in messages:
        assert isinstance(message, dict)
        tool_calls = message.get("tool_calls", [])
        assert isinstance(tool_calls, list)
        for call in tool_calls:
            assert isinstance(call, dict)
            function = call["function"]
            assert isinstance(function, dict)
            name = function["name"]
            assert isinstance(name, str)
            names.append(name)
    return names


def _anthropic_tool_names(payload: dict[str, object]) -> list[str]:
    tools = payload["tools"]
    assert isinstance(tools, list)
    names: list[str] = []
    for tool in tools:
        assert isinstance(tool, dict)
        name = tool["name"]
        assert isinstance(name, str)
        names.append(name)
    return names


def _anthropic_call_names(payload: dict[str, object]) -> list[str]:
    messages = payload["messages"]
    assert isinstance(messages, list)
    names: list[str] = []
    for message in messages:
        assert isinstance(message, dict)
        content = message["content"]
        assert isinstance(content, list)
        for block in content:
            assert isinstance(block, dict)
            if block.get("type") == "tool_use":
                name = block["name"]
                assert isinstance(name, str)
                names.append(name)
    return names


def test_both_adapters_serialize_the_shared_tool_name_map() -> None:
    openai_request = _request(provider_name="openai", model_name="gpt-4o")
    anthropic_request = _request(provider_name="anthropic", model_name="claude-sonnet-4-5")
    openai_payload = OpenAIChatCompletionsProvider()._payload(openai_request, stream=False)
    anthropic_payload = AnthropicMessagesProvider()._payload(anthropic_request, stream=False)

    mapping, _ = tool_name_maps(openai_request)
    expected_definitions = [mapping[tool.name] for tool in openai_request.available_tools]
    expected_calls = [mapping[name] for name in _HISTORY_TOOL_NAMES]

    assert _openai_tool_names(openai_payload) == expected_definitions
    assert _anthropic_tool_names(anthropic_payload) == expected_definitions
    assert _openai_call_names(openai_payload) == expected_calls
    assert _anthropic_call_names(anthropic_payload) == expected_calls


def test_shared_map_is_injective_and_bounded_for_definitions_and_history() -> None:
    request = _request(provider_name="openai", model_name="gpt-4o")
    mapping, reverse = tool_name_maps(request)

    expected_originals = [tool.name for tool in request.available_tools]
    expected_originals.extend(call.tool_name for message in request.messages for call in message.tool_calls)
    expected_originals.extend(message.tool_name for message in request.messages if message.tool_name)
    assert set(mapping) == set(expected_originals)
    assert len(set(mapping.values())) == len(mapping)
    assert all(reverse[wire_name] == original for original, wire_name in mapping.items())

    for wire_name in mapping.values():
        assert _WIRE_TOOL_NAME.fullmatch(wire_name) is not None
        assert len(wire_name) <= _MAX_TOOL_NAME_LENGTH

    assert mapping["read"] == "read"
    assert mapping[_INVALID_TOOL_NAME] == _SANITIZED_INVALID_NAME
    assert mapping[_SANITIZED_INVALID_NAME] != _SANITIZED_INVALID_NAME
    assert len(mapping[_OVERLONG_TOOL_NAME]) == _MAX_TOOL_NAME_LENGTH


def test_shared_cap_accepts_the_exact_limit_and_truncates_beyond_it() -> None:
    at_cap = "t" * _MAX_TOOL_NAME_LENGTH
    assert safe_tool_name(at_cap) == at_cap

    over_cap = at_cap + "t"
    mapped = safe_tool_name(over_cap)
    assert len(mapped) == _MAX_TOOL_NAME_LENGTH
    assert mapped != over_cap
