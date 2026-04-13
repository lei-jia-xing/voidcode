from __future__ import annotations

from dataclasses import dataclass

import pytest

from voidcode.provider.anthropic import AnthropicModelProvider
from voidcode.provider.copilot import CopilotModelProvider
from voidcode.provider.errors import (
    provider_execution_error_from_api_payload,
    provider_execution_error_from_stream_payload,
)
from voidcode.provider.google import GoogleModelProvider
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.protocol import (
    ModelProvider,
    ProviderStreamEvent,
    SingleAgentTurnRequest,
    StreamableSingleAgentProvider,
)
from voidcode.tools.contracts import ToolDefinition, ToolResult


@dataclass(frozen=True, slots=True)
class _StubContextWindow:
    prompt: str
    tool_results: tuple[ToolResult, ...]
    compacted: bool = False
    retained_tool_result_count: int = 0


def _build_turn_request(*, model_name: str) -> SingleAgentTurnRequest:
    tool_results = (ToolResult(tool_name="read_file", status="ok", content="hello world"),)
    return SingleAgentTurnRequest(
        prompt="read sample.txt",
        available_tools=(
            ToolDefinition(name="read_file", description="read file", read_only=True),
        ),
        tool_results=tool_results,
        context_window=_StubContextWindow(prompt="read sample.txt", tool_results=tool_results),
        applied_skills=(),
        raw_model=f"{model_name}/demo",
        provider_name=model_name,
        model_name="demo",
        attempt=0,
        abort_signal=None,
    )


@pytest.mark.parametrize(
    ("provider_name", "provider"),
    [
        ("openai", OpenAIModelProvider()),
        ("anthropic", AnthropicModelProvider()),
        ("google", GoogleModelProvider()),
        ("copilot", CopilotModelProvider()),
    ],
)
def test_provider_adapter_stream_turn_emits_happy_path_chunks(
    provider_name: str,
    provider: ModelProvider,
) -> None:
    single_agent = provider.single_agent_provider()
    assert isinstance(single_agent, StreamableSingleAgentProvider)

    events = list(single_agent.stream_turn(_build_turn_request(model_name=provider_name)))

    assert [event.kind for event in events] == ["delta", "content", "done"]
    assert events[0] == ProviderStreamEvent(kind="delta", channel="text", text="hello world")
    assert events[1] == ProviderStreamEvent(kind="content", channel="text", text="hello world")
    assert events[2] == ProviderStreamEvent(kind="done", done_reason="completed")


@pytest.mark.parametrize("provider_name", ["openai", "anthropic", "google", "copilot"])
def test_provider_error_mapping_from_api_payload_preserves_provider_identity(
    provider_name: str,
) -> None:
    exc = provider_execution_error_from_api_payload(
        provider_name=provider_name,
        model_name="demo",
        payload={"status_code": 429, "error": {"message": "Too many requests"}},
    )

    assert exc.kind == "rate_limit"
    assert exc.provider_name == provider_name
    assert exc.model_name == "demo"
    assert exc.details is not None
    assert exc.details["source"] == "api"


@pytest.mark.parametrize("provider_name", ["openai", "anthropic", "google", "copilot"])
def test_provider_error_mapping_from_stream_payload_preserves_provider_identity(
    provider_name: str,
) -> None:
    exc = provider_execution_error_from_stream_payload(
        provider_name=provider_name,
        model_name="demo",
        payload={"status_code": 400, "error": {"code": "context_length_exceeded"}},
    )

    assert exc.kind == "context_limit"
    assert exc.provider_name == provider_name
    assert exc.model_name == "demo"
    assert exc.details is not None
    assert exc.details["source"] == "stream"
