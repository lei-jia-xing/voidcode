from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from voidcode.provider.anthropic import AnthropicModelProvider
from voidcode.provider.config import (
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    LiteLLMProviderConfig,
)
from voidcode.provider.copilot import CopilotModelProvider
from voidcode.provider.errors import (
    provider_execution_error_from_api_payload,
    provider_execution_error_from_stream_payload,
)
from voidcode.provider.google import GoogleModelProvider
from voidcode.provider.litellm import LiteLLMModelProvider
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.opencode_go import OpenCodeGoModelProvider
from voidcode.provider.protocol import (
    ModelProvider,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    StreamableTurnProvider,
)
from voidcode.tools.contracts import ToolDefinition, ToolResult


@dataclass(frozen=True, slots=True)
class _StubContinuityState:
    summary_text: str | None = None


@dataclass(frozen=True, slots=True)
class _StubContextWindow:
    prompt: str
    tool_results: tuple[ToolResult, ...]
    compacted: bool = False
    retained_tool_result_count: int = 0
    _continuity_state: object | None = None

    @property
    def continuity_state(self) -> object | None:
        return self._continuity_state


def _build_turn_request(*, model_name: str) -> ProviderTurnRequest:
    tool_results = (ToolResult(tool_name="read_file", status="ok", content="hello world"),)
    return ProviderTurnRequest(
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


def _build_turn_request_with_skill(*, model_name: str) -> ProviderTurnRequest:
    tool_results = (ToolResult(tool_name="read_file", status="ok", content="hello world"),)
    return ProviderTurnRequest(
        prompt="summarize sample.txt",
        available_tools=(
            ToolDefinition(name="read_file", description="read file", read_only=True),
        ),
        tool_results=tool_results,
        context_window=_StubContextWindow(prompt="summarize sample.txt", tool_results=tool_results),
        applied_skills=(
            {
                "name": "summarize",
                "description": "Summarize selected files.",
                "content": "# Summarize\nUse concise bullet points.",
            },
        ),
        raw_model=f"{model_name}/demo",
        provider_name=model_name,
        model_name="demo",
        attempt=0,
        abort_signal=None,
    )


def _build_turn_request_with_continuity(*, model_name: str) -> ProviderTurnRequest:
    tool_results = (ToolResult(tool_name="read_file", status="ok", content="hello world"),)
    return ProviderTurnRequest(
        prompt="summarize sample.txt",
        available_tools=(
            ToolDefinition(name="read_file", description="read file", read_only=True),
        ),
        tool_results=tool_results,
        context_window=_StubContextWindow(
            prompt="summarize sample.txt",
            tool_results=tool_results,
            compacted=True,
            retained_tool_result_count=1,
            _continuity_state=_StubContinuityState(
                summary_text=(
                    "Compacted 2 earlier tool results:\n"
                    '1. read_file ok path=sample.txt content_preview="old"\n'
                    '2. read_file ok path=sample.txt content_preview="older"'
                )
            ),
        ),
        applied_skills=(),
        raw_model=f"{model_name}/demo",
        provider_name=model_name,
        model_name="demo",
        attempt=0,
        abort_signal=None,
    )


class _StubStreamChunk:
    def __init__(
        self,
        text: str | None,
        finish_reason: str | None = None,
        *,
        tool_calls: list[dict[str, object]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict[str, object]] | None = None,
    ) -> None:
        self._text = text
        self._finish_reason = finish_reason
        self._tool_calls = tool_calls
        self._reasoning_content = reasoning_content
        self._thinking_blocks = thinking_blocks

    def model_dump(self) -> dict[str, object]:
        choice: dict[str, object] = {
            "delta": {
                "content": self._text,
                "tool_calls": self._tool_calls,
                "reasoning_content": self._reasoning_content,
                "thinking_blocks": self._thinking_blocks,
            },
            "finish_reason": self._finish_reason,
        }
        return {"choices": [choice]}


class _StubCompletionResponse:
    def __init__(
        self,
        *,
        content: str | None = "hello world",
        tool_calls: list[dict[str, object]] | None = None,
    ) -> None:
        self._content = content
        self._tool_calls = tool_calls

    def model_dump(self) -> dict[str, object]:
        return {
            "choices": [
                {
                    "message": {
                        "content": self._content,
                        "tool_calls": self._tool_calls,
                    }
                }
            ]
        }


class _StubAPIError(Exception):
    def __init__(
        self, message: str, status_code: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _patch_litellm_completion(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    completion_content: str = "hello world",
    stream_chunks: tuple[tuple[str | None, str | None], ...] = (),
    stream_tool_chunks: tuple[tuple[list[dict[str, object]] | None, str | None], ...] = (),
    api_error: _StubAPIError | None = None,
    tool_calls: list[dict[str, object]] | None = None,
) -> None:
    import voidcode.provider.litellm_backend as backend_module

    class _PatchedAPIError(_StubAPIError):
        pass

    def _completion(*args: Any, **kwargs: Any):
        _ = args
        _LAST_REQUEST_PAYLOAD["kwargs"] = dict(kwargs)
        if api_error is not None:
            raise _PatchedAPIError(
                str(api_error), status_code=api_error.status_code, code=api_error.code
            )
        if mode == "stream":
            chunks: list[_StubStreamChunk] = [
                _StubStreamChunk(text=text, finish_reason=finish) for text, finish in stream_chunks
            ]
            chunks.extend(
                _StubStreamChunk(text=None, finish_reason=finish, tool_calls=tool_calls_chunk)
                for tool_calls_chunk, finish in stream_tool_chunks
            )
            return iter(chunks)
        return _StubCompletionResponse(content=completion_content, tool_calls=tool_calls)

    monkeypatch.setattr(backend_module, "APIError", _PatchedAPIError)
    if backend_module.litellm_module is None:

        class _FakeLiteLLM:
            def completion(self, *args: Any, **kwargs: Any):
                return _completion(*args, **kwargs)

        monkeypatch.setattr(backend_module, "litellm_module", _FakeLiteLLM())
    else:
        monkeypatch.setattr(backend_module.litellm_module, "completion", _completion)


_LAST_REQUEST_PAYLOAD: dict[str, object] = {}


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
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    provider: ModelProvider,
) -> None:
    provider = provider.turn_provider()
    assert isinstance(provider, StreamableTurnProvider)

    _patch_litellm_completion(
        monkeypatch,
        mode="stream",
        stream_chunks=(
            ("hello ", None),
            ("world", None),
            (None, "stop"),
        ),
    )

    events = list(provider.stream_turn(_build_turn_request(model_name=provider_name)))

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload.get("tools")
    assert payload.get("tool_choice") == "auto"

    assert [event.kind for event in events] == ["delta", "delta", "done"]
    assert events[0] == ProviderStreamEvent(kind="delta", channel="text", text="hello ")
    assert events[1] == ProviderStreamEvent(kind="delta", channel="text", text="world")
    assert events[2] == ProviderStreamEvent(kind="done", done_reason="completed")


@pytest.mark.parametrize(
    ("provider_name", "provider"),
    [
        ("openai", OpenAIModelProvider()),
        ("anthropic", AnthropicModelProvider()),
        ("google", GoogleModelProvider()),
        ("copilot", CopilotModelProvider()),
    ],
)
def test_provider_adapter_stream_turn_maps_http_error_to_provider_execution_error(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    provider: ModelProvider,
) -> None:
    provider = provider.turn_provider()
    assert isinstance(provider, StreamableTurnProvider)

    _patch_litellm_completion(
        monkeypatch,
        mode="stream",
        api_error=_StubAPIError("Too many requests", status_code=429, code="rate_limit"),
    )

    with pytest.raises(ProviderExecutionError, match="Too many requests") as exc_info:
        _ = list(provider.stream_turn(_build_turn_request(model_name=provider_name)))

    assert exc_info.value.kind == "rate_limit"


@pytest.mark.parametrize(
    ("provider_name", "provider"),
    [
        ("openai", OpenAIModelProvider()),
        ("anthropic", AnthropicModelProvider()),
        ("google", GoogleModelProvider()),
        ("copilot", CopilotModelProvider()),
    ],
)
def test_provider_adapter_propose_turn_returns_text_output(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    provider: ModelProvider,
) -> None:
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
    )

    result = provider.propose_turn(_build_turn_request(model_name=provider_name))

    assert result.output == "hello world"


@pytest.mark.parametrize(
    ("provider_name", "provider"),
    [
        ("openai", OpenAIModelProvider()),
        ("anthropic", AnthropicModelProvider()),
        ("google", GoogleModelProvider()),
        ("copilot", CopilotModelProvider()),
    ],
)
def test_provider_adapter_injects_applied_skills_into_system_messages(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    provider: ModelProvider,
) -> None:
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
    )

    result = provider.propose_turn(_build_turn_request_with_skill(model_name=provider_name))

    assert result.output == "hello world"
    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, str]], messages_obj)
    assert messages == [
        {
            "role": "system",
            "content": (
                "You must apply the following runtime-managed skills for this turn. "
                "Treat them as active task instructions in addition to the user's request.\n\n"
                "## summarize\n"
                "Description: Summarize selected files.\n"
                "# Summarize\nUse concise bullet points."
            ),
        },
        {"role": "user", "content": "summarize sample.txt"},
    ]


def test_provider_adapter_prefers_runtime_skill_prompt_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider()
    provider = provider.turn_provider()
    request = _build_turn_request_with_skill(model_name="openai")
    request = ProviderTurnRequest(
        prompt=request.prompt,
        available_tools=request.available_tools,
        tool_results=request.tool_results,
        context_window=request.context_window,
        applied_skills=request.applied_skills,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        skill_prompt_context="Runtime skill context\n\nSkill: summarize\nInstructions:\nBe brief.",
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, str]], messages_obj)
    assert messages[0] == {
        "role": "system",
        "content": "Runtime skill context\n\nSkill: summarize\nInstructions:\nBe brief.",
    }


@pytest.mark.parametrize(
    ("provider_name", "provider"),
    [
        ("openai", OpenAIModelProvider()),
        ("anthropic", AnthropicModelProvider()),
        ("google", GoogleModelProvider()),
        ("copilot", CopilotModelProvider()),
    ],
)
def test_provider_adapter_injects_continuity_summary_into_system_messages(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    provider: ModelProvider,
) -> None:
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
    )

    result = provider.propose_turn(_build_turn_request_with_continuity(model_name=provider_name))

    assert result.output == "hello world"
    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, str]], messages_obj)
    assert messages == [
        {
            "role": "system",
            "content": (
                "Runtime continuity summary:\n"
                "Compacted 2 earlier tool results:\n"
                '1. read_file ok path=sample.txt content_preview="old"\n'
                '2. read_file ok path=sample.txt content_preview="older"'
            ),
        },
        {"role": "user", "content": "summarize sample.txt"},
    ]


def test_provider_adapter_omits_continuity_message_without_summary_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider()
    provider = provider.turn_provider()
    request = _build_turn_request(model_name="openai")
    request = ProviderTurnRequest(
        prompt=request.prompt,
        available_tools=request.available_tools,
        tool_results=request.tool_results,
        context_window=_StubContextWindow(
            prompt=request.context_window.prompt,
            tool_results=request.context_window.tool_results,
            _continuity_state=_StubContinuityState(summary_text="   "),
        ),
        applied_skills=request.applied_skills,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, str]], messages_obj)
    assert messages == [{"role": "user", "content": "read sample.txt"}]


def test_provider_adapter_injects_leader_prompt_profile_system_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider()
    provider = provider.turn_provider()
    request = _build_turn_request(model_name="openai")
    request = ProviderTurnRequest(
        prompt=request.prompt,
        available_tools=request.available_tools,
        tool_results=request.tool_results,
        context_window=request.context_window,
        applied_skills=request.applied_skills,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        agent_preset={
            "preset": "leader",
            "prompt_profile": "leader",
            "model": "openai/demo",
            "execution_engine": "provider",
        },
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, str]], messages_obj)
    assert messages[0]["role"] == "system"
    assert "VoidCode's leader agent" in messages[0]["content"]
    assert "single active execution agent path" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "read sample.txt"}


def test_provider_adapter_propose_turn_uses_model_map_for_litellm_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LiteLLMModelProvider(
        config=LiteLLMProviderConfig(
            model_map={"demo": "openrouter/openai/gpt-4o"},
            base_url="http://localhost:4000",
        ),
    )
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
    )

    _ = provider.propose_turn(_build_turn_request(model_name="demo"))

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload["model"] == "openrouter/openai/gpt-4o"


@pytest.mark.parametrize(
    ("model_name", "custom_provider"),
    [
        ("glm-5.1", "openai"),
        ("kimi-k2.6", "openai"),
        ("mimo-v2.5-pro", "openai"),
        ("minimax-m2.7", "anthropic"),
        ("minimax-m2.5", "anthropic"),
        ("qwen3.6-plus", "dashscope"),
        ("qwen3.5-plus", "dashscope"),
    ],
)
def test_opencode_go_provider_routes_model_families_to_required_sdk_adapter(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    custom_provider: str,
) -> None:
    from voidcode.provider.config import SimplifiedProviderConfig

    provider = OpenCodeGoModelProvider(config=SimplifiedProviderConfig(api_key="opencode-go-key"))
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    result = provider.propose_turn(
        ProviderTurnRequest(
            prompt="read sample.txt",
            available_tools=(
                ToolDefinition(name="read_file", description="read file", read_only=True),
            ),
            tool_results=(),
            context_window=_StubContextWindow(prompt="read sample.txt", tool_results=()),
            applied_skills=(),
            raw_model=f"opencode-go/{model_name}",
            provider_name="opencode-go",
            model_name=model_name,
            attempt=0,
            abort_signal=None,
        )
    )

    assert result.output == "ok"
    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload["model"] == model_name
    assert payload["custom_llm_provider"] == custom_provider
    expected_api_base = (
        "https://opencode.ai/zen/go"
        if model_name in {"minimax-m2.7", "minimax-m2.5"}
        else "https://opencode.ai/zen/go/v1"
    )
    assert payload["api_base"] == expected_api_base
    assert payload["api_key"] == "opencode-go-key"
    if model_name in {"minimax-m2.7", "minimax-m2.5"}:
        assert payload["extra_headers"] == {
            "anthropic-version": "2023-06-01",
            "user-agent": "@ai-sdk/anthropic",
        }
    assert payload.get("tools")
    assert payload.get("tool_choice") == "auto"


def test_glm_provider_does_not_append_v1_to_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.provider.config import SimplifiedProviderConfig
    from voidcode.provider.glm import GLMModelProvider

    provider = GLMModelProvider(config=SimplifiedProviderConfig(api_key="glm-key"))
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    _ = provider.propose_turn(_build_turn_request(model_name="glm"))

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload["api_base"] == "https://open.bigmodel.cn/api/paas/v4"


def test_provider_adapter_propose_turn_returns_tool_call_when_model_requests_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider()
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        tool_calls=[
            {
                "function": {
                    "name": "read_file",
                    "arguments": '{"path":"sample.txt"}',
                }
            }
        ],
    )

    result = provider.propose_turn(_build_turn_request(model_name="openai"))

    assert result.tool_call is not None
    assert result.tool_call.tool_name == "read_file"
    assert result.tool_call.arguments == {"path": "sample.txt"}


def test_google_provider_api_key_uses_google_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GoogleModelProvider(
        config=GoogleProviderConfig(
            auth=GoogleProviderAuthConfig(method="api_key", api_key="AIza-test")
        )
    )
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    result = provider.propose_turn(_build_turn_request(model_name="google"))

    assert result.output == "ok"
    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert "api_key" not in payload
    extra_headers = payload.get("extra_headers")
    assert isinstance(extra_headers, dict)
    assert extra_headers == {"x-goog-api-key": "AIza-test"}


def test_provider_adapter_stream_turn_emits_tool_event_when_model_streams_tool_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider()
    provider = provider.turn_provider()
    assert isinstance(provider, StreamableTurnProvider)

    _patch_litellm_completion(
        monkeypatch,
        mode="stream",
        stream_tool_chunks=(
            (
                [
                    {
                        "index": 0,
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"sample.txt"}',
                        },
                    }
                ],
                None,
            ),
            (None, "tool_calls"),
        ),
    )

    events = list(provider.stream_turn(_build_turn_request(model_name="openai")))

    assert events == [
        ProviderStreamEvent(
            kind="content",
            channel="tool",
            text='{"tool_name": "read_file", "arguments": {"path": "sample.txt"}}',
        ),
        ProviderStreamEvent(kind="done", done_reason="completed"),
    ]


def test_provider_adapter_stream_turn_emits_reasoning_events_from_reasoning_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider()
    provider = provider.turn_provider()
    assert isinstance(provider, StreamableTurnProvider)

    import voidcode.provider.litellm_backend as backend_module

    def _completion(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return iter(
            [
                _StubStreamChunk(
                    text=None,
                    finish_reason=None,
                    reasoning_content="Thinking step.",
                ),
                _StubStreamChunk(text="Done.", finish_reason=None),
                _StubStreamChunk(text=None, finish_reason="stop"),
            ]
        )

    if backend_module.litellm_module is None:

        class _FakeLiteLLM:
            def completion(self, *args: Any, **kwargs: Any):
                return _completion(*args, **kwargs)

        monkeypatch.setattr(backend_module, "litellm_module", _FakeLiteLLM())
    else:
        monkeypatch.setattr(backend_module.litellm_module, "completion", _completion)

    events = list(provider.stream_turn(_build_turn_request(model_name="openai")))

    assert events == [
        ProviderStreamEvent(kind="delta", channel="reasoning", text="Thinking step."),
        ProviderStreamEvent(kind="delta", channel="text", text="Done."),
        ProviderStreamEvent(kind="done", done_reason="completed"),
    ]


def test_provider_adapter_stream_turn_emits_reasoning_events_from_thinking_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AnthropicModelProvider()
    provider = provider.turn_provider()
    assert isinstance(provider, StreamableTurnProvider)

    import voidcode.provider.litellm_backend as backend_module

    def _completion(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return iter(
            [
                _StubStreamChunk(
                    text=None,
                    finish_reason=None,
                    thinking_blocks=[{"type": "thinking", "thinking": "Private chain."}],
                ),
                _StubStreamChunk(text="Visible answer", finish_reason=None),
                _StubStreamChunk(text=None, finish_reason="stop"),
            ]
        )

    if backend_module.litellm_module is None:

        class _FakeLiteLLM:
            def completion(self, *args: Any, **kwargs: Any):
                return _completion(*args, **kwargs)

        monkeypatch.setattr(backend_module, "litellm_module", _FakeLiteLLM())
    else:
        monkeypatch.setattr(backend_module.litellm_module, "completion", _completion)

    events = list(provider.stream_turn(_build_turn_request(model_name="anthropic")))

    assert events == [
        ProviderStreamEvent(kind="delta", channel="reasoning", text="Private chain."),
        ProviderStreamEvent(kind="delta", channel="text", text="Visible answer"),
        ProviderStreamEvent(kind="done", done_reason="completed"),
    ]


@pytest.mark.parametrize(
    ("provider_name", "provider"),
    [
        ("openai", OpenAIModelProvider()),
        ("anthropic", AnthropicModelProvider()),
        ("google", GoogleModelProvider()),
        ("copilot", CopilotModelProvider()),
    ],
)
def test_provider_adapters_call_litellm_directly_without_internal_bridge(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    provider: ModelProvider,
) -> None:
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    result = provider.propose_turn(_build_turn_request(model_name=provider_name))

    assert result.output == "ok"
    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload["model"] == f"{provider_name}/demo"


def test_copilot_provider_reads_token_from_configured_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voidcode.provider.config import CopilotProviderAuthConfig, CopilotProviderConfig

    monkeypatch.setenv("COPILOT_TOKEN", "env-copilot-token")
    provider = CopilotModelProvider(
        config=CopilotProviderConfig(
            auth=CopilotProviderAuthConfig(method="token", token_env_var="COPILOT_TOKEN")
        )
    )
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    result = provider.propose_turn(_build_turn_request(model_name="copilot"))

    assert result.output == "ok"
    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload.get("api_key") == "env-copilot-token"


def test_google_provider_service_account_forwards_vertex_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GoogleModelProvider(
        config=GoogleProviderConfig(
            auth=GoogleProviderAuthConfig(
                method="service_account",
                service_account_json_path="C:/keys/google-sa.json",
            )
        )
    )
    provider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    result = provider.propose_turn(_build_turn_request(model_name="google"))

    assert result.output == "ok"
    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload.get("vertex_credentials") == "C:/keys/google-sa.json"


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
