from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, cast

import pytest

from voidcode.provider.anthropic import AnthropicModelProvider
from voidcode.provider.config import (
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    LiteLLMProviderConfig,
    SimplifiedProviderConfig,
)
from voidcode.provider.copilot import CopilotModelProvider
from voidcode.provider.errors import (
    provider_execution_error_from_api_payload,
    provider_execution_error_from_stream_payload,
)
from voidcode.provider.glm import GLMModelProvider
from voidcode.provider.google import GoogleModelProvider
from voidcode.provider.litellm import LiteLLMModelProvider
from voidcode.provider.litellm_backend import LiteLLMBackendSingleAgentProvider
from voidcode.provider.model_catalog import ProviderModelMetadata, infer_model_metadata
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.opencode_go import OpenCodeGoModelProvider
from voidcode.provider.protocol import (
    ModelProvider,
    ProviderAssembledContext,
    ProviderContextSegment,
    ProviderContextSegmentLike,
    ProviderContextWindow,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
    StreamableTurnProvider,
    TurnProvider,
)
from voidcode.tools.contracts import ToolDefinition, ToolResult
from voidcode.tools.task import TaskTool


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


@dataclass(frozen=True, slots=True)
class _StubAssembledContext:
    prompt: str
    tool_results: tuple[ToolResult, ...]
    continuity_state: object | None
    segments: tuple[ProviderContextSegmentLike, ...]
    metadata: dict[str, object]


def _assembled_from_legacy(
    *,
    prompt: str,
    tool_results: tuple[ToolResult, ...],
    context_window: ProviderContextWindow,
    applied_skills: tuple[dict[str, str], ...],
    skill_prompt_context: str = "",
) -> ProviderAssembledContext:
    continuity_state = context_window.continuity_state
    segments: list[ProviderContextSegmentLike] = []
    skill_message = skill_prompt_context.strip()
    if not skill_message and applied_skills:
        rendered_skills: list[str] = []
        for skill in applied_skills:
            name = skill.get("name", "").strip() or "unnamed-skill"
            description = skill.get("description", "").strip()
            content = skill.get("prompt_context", "").strip() or skill.get("content", "").strip()
            lines = [f"## {name}"]
            if description:
                lines.append(f"Description: {description}")
            if content:
                lines.append(content)
            rendered_skills.append("\n".join(lines))
        if rendered_skills:
            skill_message = (
                "You must apply the following runtime-managed skills for this turn. "
                "Treat them as active task instructions in addition to the user's request.\n\n"
                + "\n\n".join(rendered_skills)
            )
    if skill_message:
        segments.append(ProviderContextSegment(role="system", content=skill_message))
    if continuity_state is not None:
        summary_text = getattr(continuity_state, "summary_text", None)
        if isinstance(summary_text, str) and summary_text.strip():
            segments.append(
                ProviderContextSegment(
                    role="system",
                    content=f"Runtime continuity summary:\n{summary_text.strip()}",
                )
            )
    segments.append(ProviderContextSegment(role="user", content=prompt))
    for index, result in enumerate(tool_results, start=1):
        raw_tool_call_id = result.data.get("tool_call_id")
        tool_call_id = (
            raw_tool_call_id
            if isinstance(raw_tool_call_id, str) and raw_tool_call_id.strip()
            else f"voidcode_tool_{index}"
        )
        raw_arguments = result.data.get("arguments")
        tool_arguments = (
            cast(dict[str, object], raw_arguments) if isinstance(raw_arguments, dict) else {}
        )
        segments.append(
            ProviderContextSegment(
                role="assistant",
                content=None,
                tool_call_id=tool_call_id,
                tool_name=result.tool_name,
                tool_arguments=tool_arguments,
            )
        )
        segments.append(
            ProviderContextSegment(
                role="tool",
                content=result.content or "",
                tool_call_id=tool_call_id,
                tool_name=result.tool_name,
                metadata={
                    "status": result.status,
                    "error": result.error,
                    "data": result.data,
                    "truncated": result.truncated,
                    "partial": result.partial,
                    "reference": result.reference,
                },
            )
        )
    return _StubAssembledContext(
        prompt=prompt,
        tool_results=tool_results,
        continuity_state=continuity_state,
        segments=tuple(segments),
        metadata={},
    )


def _build_turn_request(
    *, model_name: str, reasoning_effort: str | None = None
) -> ProviderTurnRequest:
    tool_results: tuple[ToolResult, ...] = ()
    return ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt="read sample.txt",
            tool_results=tool_results,
            context_window=_StubContextWindow(prompt="read sample.txt", tool_results=tool_results),
            applied_skills=(),
        ),
        available_tools=(
            ToolDefinition(name="read_file", description="read file", read_only=True),
        ),
        raw_model=f"{model_name}/demo",
        provider_name=model_name,
        model_name="demo",
        reasoning_effort=reasoning_effort,
        attempt=0,
        abort_signal=None,
    )


def _build_turn_request_with_skill(*, model_name: str) -> ProviderTurnRequest:
    tool_results: tuple[ToolResult, ...] = ()
    return ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt="summarize sample.txt",
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt="summarize sample.txt", tool_results=tool_results
            ),
            applied_skills=(
                {
                    "name": "summarize",
                    "description": "Summarize selected files.",
                    "content": "# Summarize\nUse concise bullet points.",
                },
            ),
        ),
        available_tools=(
            ToolDefinition(name="read_file", description="read file", read_only=True),
        ),
        raw_model=f"{model_name}/demo",
        provider_name=model_name,
        model_name="demo",
        attempt=0,
        abort_signal=None,
    )


def _build_turn_request_with_continuity(*, model_name: str) -> ProviderTurnRequest:
    tool_results: tuple[ToolResult, ...] = ()
    return ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt="summarize sample.txt",
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
        ),
        available_tools=(
            ToolDefinition(name="read_file", description="read file", read_only=True),
        ),
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
        reasoning: str | None = None,
        thinking_blocks: list[dict[str, object]] | None = None,
        usage: dict[str, object] | None = None,
    ) -> None:
        self._text = text
        self._finish_reason = finish_reason
        self._tool_calls = tool_calls
        self._reasoning_content = reasoning_content
        self._reasoning = reasoning
        self._thinking_blocks = thinking_blocks
        self._usage = usage

    def model_dump(self) -> dict[str, object]:
        choice: dict[str, object] = {
            "delta": {
                "content": self._text,
                "tool_calls": self._tool_calls,
                "reasoning_content": self._reasoning_content,
                "reasoning": self._reasoning,
                "thinking_blocks": self._thinking_blocks,
            },
            "finish_reason": self._finish_reason,
        }
        payload: dict[str, object] = {"choices": [choice]}
        if self._usage is not None:
            payload["usage"] = self._usage
        return payload


class _StubStreamUsageChunk:
    def __init__(self, usage: dict[str, object]) -> None:
        self._usage = usage

    def model_dump(self) -> dict[str, object]:
        return {"choices": [], "usage": self._usage}


class _StubCompletionResponse:
    def __init__(
        self,
        *,
        content: str | None = "hello world",
        tool_calls: list[dict[str, object]] | None = None,
        usage: dict[str, object] | None = None,
    ) -> None:
        self._content = content
        self._tool_calls = tool_calls
        self._usage = usage

    def model_dump(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "choices": [
                {
                    "message": {
                        "content": self._content,
                        "tool_calls": self._tool_calls,
                    }
                }
            ]
        }
        if self._usage is not None:
            payload["usage"] = self._usage
        return payload


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
    stream_usage_tail: bool = False,
    api_error: _StubAPIError | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    usage: dict[str, object] | None = None,
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
            chunks: list[object] = [
                _StubStreamChunk(
                    text=text,
                    finish_reason=finish,
                    usage=usage if finish is not None else None,
                )
                for text, finish in stream_chunks
            ]
            chunks.extend(
                _StubStreamChunk(text=None, finish_reason=finish, tool_calls=tool_calls_chunk)
                for tool_calls_chunk, finish in stream_tool_chunks
            )
            if stream_usage_tail and usage is not None:
                chunks.append(_StubStreamUsageChunk(usage))
            return iter(chunks)
        return _StubCompletionResponse(
            content=completion_content,
            tool_calls=tool_calls,
            usage=usage,
        )

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
    turn_provider = provider.turn_provider()
    assert isinstance(turn_provider, StreamableTurnProvider)

    _patch_litellm_completion(
        monkeypatch,
        mode="stream",
        stream_chunks=(
            ("hello ", None),
            ("world", None),
            (None, "stop"),
        ),
    )

    events = list(turn_provider.stream_turn(_build_turn_request(model_name=provider_name)))

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload.get("tools")
    assert payload.get("tool_choice") == "auto"

    assert [event.kind for event in events] == ["delta", "delta", "done"]
    assert events[0] == ProviderStreamEvent(kind="delta", channel="text", text="hello ")
    assert events[1] == ProviderStreamEvent(kind="delta", channel="text", text="world")
    assert events[2] == ProviderStreamEvent(kind="done", done_reason="completed")


def test_provider_adapter_wraps_internal_tool_property_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=request.tool_results,
            context_window=request.context_window,
            applied_skills=request.applied_skills,
        ),
        available_tools=(
            ToolDefinition(
                name="write_file",
                description="write file",
                input_schema={
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                read_only=False,
            ),
        ),
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
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
    tools_obj = payload.get("tools")
    assert isinstance(tools_obj, list)
    tool_payload = cast(dict[str, object], tools_obj[0])
    function_obj = tool_payload.get("function")
    assert isinstance(function_obj, dict)
    function = cast(dict[str, object], function_obj)
    assert function["parameters"] == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "additionalProperties": True,
    }


def test_provider_adapter_wraps_property_schema_with_description_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=request.tool_results,
            context_window=request.context_window,
            applied_skills=request.applied_skills,
        ),
        available_tools=(
            ToolDefinition(
                name="demo_tool",
                description="demo",
                input_schema={"description": {"type": "string"}},
                read_only=True,
            ),
        ),
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
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
    tools_obj = payload.get("tools")
    assert isinstance(tools_obj, list)
    tool_payload = cast(dict[str, object], tools_obj[0])
    function_obj = tool_payload.get("function")
    assert isinstance(function_obj, dict)
    function = cast(dict[str, object], function_obj)
    assert function["parameters"] == {
        "type": "object",
        "properties": {"description": {"type": "string"}},
        "additionalProperties": True,
    }


def test_provider_adapter_wraps_task_tool_description_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=request.tool_results,
            context_window=request.context_window,
            applied_skills=request.applied_skills,
        ),
        available_tools=(TaskTool.definition,),
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
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
    tools_obj = payload.get("tools")
    assert isinstance(tools_obj, list)
    tool_payload = cast(dict[str, object], tools_obj[0])
    function_obj = tool_payload.get("function")
    assert isinstance(function_obj, dict)
    function = cast(dict[str, object], function_obj)
    parameters_obj = function.get("parameters")
    assert isinstance(parameters_obj, dict)
    parameters = cast(dict[str, object], parameters_obj)
    properties_obj = parameters.get("properties")
    assert isinstance(properties_obj, dict)
    properties = cast(dict[str, object], properties_obj)

    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["prompt", "run_in_background", "load_skills"]
    one_of = cast(list[object], parameters["oneOf"])
    assert len(one_of) == 2
    examples = cast(list[object], parameters["examples"])
    assert cast(dict[str, object], examples[0])["run_in_background"] is True
    assert cast(dict[str, object], examples[1])["run_in_background"] is False
    description_property = cast(dict[str, object], properties["description"])
    prompt_property = cast(dict[str, object], properties["prompt"])
    assert description_property["type"] == "string"
    assert prompt_property["type"] == "string"
    assert "Full delegated task prompt" in cast(str, prompt_property["description"])


def test_provider_adapter_preserves_object_schema_without_explicit_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=request.tool_results,
            context_window=request.context_window,
            applied_skills=request.applied_skills,
        ),
        available_tools=(
            ToolDefinition(
                name="mcp_search",
                description="search MCP data",
                input_schema={
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                read_only=True,
            ),
        ),
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
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
    tools_obj = payload.get("tools")
    assert isinstance(tools_obj, list)
    tool_payload = cast(dict[str, object], tools_obj[0])
    function_obj = tool_payload.get("function")
    assert isinstance(function_obj, dict)
    function = cast(dict[str, object], function_obj)
    assert function["parameters"] == {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
        "additionalProperties": False,
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
def test_provider_adapter_stream_turn_maps_http_error_to_provider_execution_error(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    provider: ModelProvider,
) -> None:
    turn_provider = provider.turn_provider()
    assert isinstance(turn_provider, StreamableTurnProvider)

    _patch_litellm_completion(
        monkeypatch,
        mode="stream",
        api_error=_StubAPIError("Too many requests", status_code=429, code="rate_limit"),
    )

    with pytest.raises(ProviderExecutionError, match="Too many requests") as exc_info:
        _ = list(turn_provider.stream_turn(_build_turn_request(model_name=provider_name)))

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
    turn_provider: TurnProvider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
    )

    result = turn_provider.propose_turn(_build_turn_request(model_name=provider_name))

    assert result.output == "hello world"


def test_provider_adapter_propose_turn_returns_token_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
        usage={
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 3},
        },
    )

    result = provider.propose_turn(_build_turn_request(model_name="openai"))

    assert result.usage == ProviderTokenUsage(
        input_tokens=12,
        output_tokens=4,
        cache_read_tokens=3,
    )


def test_provider_adapter_stream_turn_returns_final_token_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    assert isinstance(provider, StreamableTurnProvider)

    _patch_litellm_completion(
        monkeypatch,
        mode="stream",
        stream_chunks=((None, "stop"),),
        usage={"prompt_tokens": 10, "completion_tokens": 2},
    )

    events = list(provider.stream_turn(_build_turn_request(model_name="openai")))

    assert events == [
        ProviderStreamEvent(
            kind="done",
            done_reason="completed",
            usage=ProviderTokenUsage(input_tokens=10, output_tokens=2),
        )
    ]


def test_provider_adapter_stream_turn_consumes_trailing_usage_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    assert isinstance(provider, StreamableTurnProvider)

    _patch_litellm_completion(
        monkeypatch,
        mode="stream",
        stream_chunks=((None, "stop"),),
        stream_usage_tail=True,
        usage={"prompt_tokens": 10, "completion_tokens": 2},
    )

    events = list(provider.stream_turn(_build_turn_request(model_name="openai")))

    assert events == [
        ProviderStreamEvent(
            kind="done",
            done_reason="completed",
            usage=ProviderTokenUsage(input_tokens=10, output_tokens=2),
        )
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
def test_provider_adapter_injects_applied_skills_into_system_messages(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    provider: ModelProvider,
) -> None:
    turn_provider: TurnProvider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
    )

    result = turn_provider.propose_turn(_build_turn_request_with_skill(model_name=provider_name))

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
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=request.tool_results,
            context_window=request.context_window,
            applied_skills=(),
            skill_prompt_context=(
                "Runtime skill context\n\nSkill: summarize\nInstructions:\nBe brief."
            ),
        ),
        available_tools=request.available_tools,
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
    turn_provider: TurnProvider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="hello world",
    )

    result = turn_provider.propose_turn(
        _build_turn_request_with_continuity(model_name=provider_name)
    )

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
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=request.tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=request.context_window.tool_results,
                _continuity_state=_StubContinuityState(summary_text="   "),
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
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


def test_provider_adapter_includes_tool_result_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    tool_results = (ToolResult(tool_name="read_file", status="ok", content="hello world"),)
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assert messages[0] == {"role": "user", "content": "read sample.txt"}
    assert messages[1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "voidcode_tool_1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": "{}",
                },
            }
        ],
    }
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "voidcode_tool_1"
    tool_content = messages[2]["content"]
    assert isinstance(tool_content, str)
    assert '"status": "ok"' in tool_content
    assert '"content": "hello world"' in tool_content


def test_provider_adapter_includes_truncated_tool_reference_without_full_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    tool_results = (
        ToolResult(
            tool_name="web_fetch",
            status="ok",
            content=(
                "small preview\n\n[Tool output truncated: Full output saved to: "
                ".voidcode/tool-output/web_fetch.txt]"
            ),
            data={
                "url": "https://example.com",
                "truncated": True,
                "output_path": ".voidcode/tool-output/web_fetch.txt",
            },
            truncated=True,
            partial=True,
            reference=".voidcode/tool-output/web_fetch.txt",
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    tool_content = messages[2]["content"]
    assert isinstance(tool_content, str)
    assert '"truncated": true' in tool_content
    assert ".voidcode/tool-output/web_fetch.txt" in tool_content
    assert "FULL WEBSITE BODY" not in tool_content


def test_provider_adapter_sanitizes_tool_arguments_and_inline_blobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    raw_content = "RAW FILE CONTENT SHOULD NOT REACH MODEL"
    raw_data_uri = "data:image/png;base64," + "A" * 64
    tool_results = (
        ToolResult(
            tool_name="write_file",
            status="ok",
            content="Wrote file successfully: out.txt",
            data={
                "tool_call_id": "call-write",
                "arguments": {"path": "out.txt", "content": raw_content},
                "attachment": {"mime": "image/png", "data_uri": raw_data_uri},
            },
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assistant_call = messages[1]
    tool_calls = cast(list[dict[str, object]], assistant_call["tool_calls"])
    function = cast(dict[str, object], tool_calls[0]["function"])
    tool_content = messages[2]["content"]
    assert isinstance(tool_content, str)
    raw_arguments = function["arguments"]
    assert isinstance(raw_arguments, str)
    assert raw_content not in raw_arguments
    assert raw_content not in tool_content
    assert raw_data_uri not in tool_content
    assert '"content": ""' in raw_arguments
    assert '"omitted": true' not in raw_arguments
    assert '"byte_count"' not in raw_arguments
    assert '"data_uri": {"byte_count"' in tool_content


def test_provider_adapter_strips_redaction_sentinels_from_todo_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    raw_todo_content = "Secret todo text should not become a reusable schema example"
    tool_results = (
        ToolResult(
            tool_name="todo_write",
            status="ok",
            content="Updated todos",
            data={
                "tool_call_id": "call-todo",
                "arguments": {
                    "todos": [
                        {
                            "content": raw_todo_content,
                            "status": "pending",
                            "priority": "high",
                        }
                    ]
                },
            },
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assistant_call = messages[1]
    tool_calls = cast(list[dict[str, object]], assistant_call["tool_calls"])
    function = cast(dict[str, object], tool_calls[0]["function"])
    raw_arguments = function["arguments"]
    assert isinstance(raw_arguments, str)
    assert raw_todo_content not in raw_arguments
    assert '"content": ""' in raw_arguments
    assert '"omitted": true' not in raw_arguments
    assert '"byte_count"' not in raw_arguments


def test_provider_adapter_preserves_truncated_safe_argument_previews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    query_prefix = "safe oversized query prefix"
    large_query = query_prefix + (" x" * 2500)
    tool_results = (
        ToolResult(
            tool_name="code_search",
            status="ok",
            content="Found 3 matches",
            data={
                "tool_call_id": "call-search",
                "arguments": {"query": large_query},
            },
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assistant_call = messages[1]
    tool_calls = cast(list[dict[str, object]], assistant_call["tool_calls"])
    function = cast(dict[str, object], tool_calls[0]["function"])
    raw_arguments = function["arguments"]
    assert isinstance(raw_arguments, str)
    arguments_payload = cast(dict[str, object], json.loads(raw_arguments))
    query_summary = cast(dict[str, object], arguments_payload["query"])
    preview = query_summary["preview"]
    assert isinstance(preview, str)
    assert query_summary["omitted"] is True
    assert query_summary["omitted_chars"] == len(large_query) - 4000
    assert preview == large_query[:4000]
    assert query_prefix in raw_arguments
    assert '"preview"' in raw_arguments
    assert '"omitted_chars"' in raw_arguments
    assert large_query not in raw_arguments


def test_provider_adapter_preserves_custom_metadata_shaped_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    metadata_argument = {"omitted": True, "byte_count": 42, "line_count": 2}
    tool_results = (
        ToolResult(
            tool_name="mcp/demo/custom",
            status="ok",
            content="Completed custom tool",
            data={
                "tool_call_id": "call-custom",
                "arguments": {"content": metadata_argument},
            },
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assistant_call = messages[1]
    tool_calls = cast(list[dict[str, object]], assistant_call["tool_calls"])
    function = cast(dict[str, object], tool_calls[0]["function"])
    raw_arguments = function["arguments"]
    assert isinstance(raw_arguments, str)
    arguments_payload = cast(dict[str, object], json.loads(raw_arguments))
    assert arguments_payload["content"] == metadata_argument


def test_provider_adapter_includes_tool_result_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    tool_results = (
        ToolResult(tool_name="read_file", status="error", error="sample.txt not found"),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assert messages[0] == {"role": "user", "content": "read sample.txt"}
    assert messages[1]["role"] == "assistant"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "voidcode_tool_1"
    tool_content = messages[2]["content"]
    assert isinstance(tool_content, str)
    assert '"status": "error"' in tool_content
    assert '"error": "sample.txt not found"' in tool_content


def test_provider_adapter_preserves_tool_call_id_and_arguments_in_tool_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    request = _build_turn_request(model_name="openai")
    tool_results = (
        ToolResult(
            tool_name="glob",
            status="ok",
            content="./\n  CMakeLists.txt",
            data={
                "tool_call_id": "call-glob-1",
                "arguments": {"pattern": "**/*"},
                "path": "/workspace",
            },
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model=request.raw_model,
        provider_name=request.provider_name,
        model_name=request.model_name,
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assert messages[1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-glob-1",
                "type": "function",
                "function": {
                    "name": "glob",
                    "arguments": '{"pattern": "**/*"}',
                },
            }
        ],
    }
    assert messages[2]["tool_call_id"] == "call-glob-1"
    tool_content = messages[2]["content"]
    assert isinstance(tool_content, str)
    assert '"path": "/workspace"' in tool_content
    assert "tool_call_id" not in tool_content


def test_opencode_go_openai_compatible_provider_uses_tool_call_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenCodeGoModelProvider().turn_provider()
    request = _build_turn_request(model_name="opencode-go")
    tool_results = (
        ToolResult(
            tool_name="glob",
            status="ok",
            content="./\n  .voidcode.json",
            data={"tool_call_id": "glob_0", "arguments": {"pattern": "**/*"}},
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model="opencode-go/kimi-k2.6",
        provider_name="opencode-go",
        model_name="kimi-k2.6",
        model_metadata=infer_model_metadata("opencode-go", "kimi-k2.6"),
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assert messages[0] == {"role": "user", "content": "read sample.txt"}
    assert messages[1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "glob_0",
                "type": "function",
                "function": {"name": "glob", "arguments": '{"pattern": "**/*"}'},
            }
        ],
    }
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "glob_0"
    assert "Completed tool calls for current request:" not in str(messages)


def test_opencode_go_openai_compatible_provider_sanitizes_tool_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenCodeGoModelProvider().turn_provider()
    request = _build_turn_request(model_name="opencode-go")
    raw_content = "RAW FILE CONTENT SHOULD NOT REACH OPENCODE GO"
    raw_patch = "diff --git a/a b/a\n" + "SECRET PATCH CONTENT"
    raw_data_uri = "data:image/png;base64," + "A" * 64
    tool_results = (
        ToolResult(
            tool_name="write_file",
            status="ok",
            content="Wrote file successfully: out.txt",
            data={
                "tool_call_id": "write_0",
                "arguments": {"path": "out.txt", "content": raw_content, "patch": raw_patch},
                "attachment": {"mime": "image/png", "data_uri": raw_data_uri},
            },
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model="opencode-go/glm-5.1",
        provider_name="opencode-go",
        model_name="glm-5.1",
        model_metadata=infer_model_metadata("opencode-go", "glm-5.1"),
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assert messages[1]["role"] == "assistant"
    assert "tool_calls" in messages[1]
    tool_content = messages[2]["content"]
    assert isinstance(tool_content, str)
    assert raw_content not in tool_content
    assert raw_patch not in tool_content
    assert raw_data_uri not in tool_content
    assert '"omitted": true' in tool_content
    assert '"data_uri": {"byte_count"' in tool_content


def test_opencode_go_mimo_preserves_standard_tool_pairing_with_model_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenCodeGoModelProvider().turn_provider()
    request = _build_turn_request(model_name="opencode-go")
    tool_results = (
        ToolResult(
            tool_name="glob",
            status="ok",
            content="./\n  README.md",
            data={"tool_call_id": "glob_0", "arguments": {"pattern": "**/*"}},
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model="opencode-go/mimo-v2.5-pro",
        provider_name="opencode-go",
        model_name="mimo-v2.5-pro",
        model_metadata=infer_model_metadata("opencode-go", "mimo-v2.5-pro"),
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assert messages[1]["role"] == "assistant"
    assert "tool_calls" in messages[1]
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "glob_0"
    assert "Completed tool calls for current request:" not in str(messages)


def test_provider_adapter_synthetic_tool_feedback_policy_is_provider_agnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LiteLLMBackendSingleAgentProvider(
        name="custom",
        config=None,
    )
    request = _build_turn_request(model_name="custom")
    tool_results = (
        ToolResult(
            tool_name="glob",
            status="ok",
            content="./\n  .voidcode.json",
            data={"tool_call_id": "glob_0", "arguments": {"pattern": "**/*"}},
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model="custom/demo",
        provider_name="custom",
        model_name="demo",
        model_metadata=ProviderModelMetadata(tool_feedback_mode="synthetic_user_message"),
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assert messages[1]["role"] == "user"
    feedback = messages[1]["content"]
    assert isinstance(feedback, str)
    assert "Completed tool calls for current request:" in feedback
    assert '"tool_name": "glob"' in feedback
    assert "tool_calls" not in messages[1]


def test_provider_adapter_synthetic_feedback_strips_argument_sentinels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LiteLLMBackendSingleAgentProvider(
        name="custom",
        config=None,
    )
    request = _build_turn_request(model_name="custom")
    raw_todo_content = "Secret todo text should not appear in synthetic feedback arguments"
    tool_results = (
        ToolResult(
            tool_name="todo_write",
            status="ok",
            content="Updated todos",
            data={
                "tool_call_id": "todo_0",
                "arguments": {
                    "todos": [
                        {
                            "content": raw_todo_content,
                            "status": "pending",
                            "priority": "high",
                        }
                    ]
                },
            },
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model="custom/demo",
        provider_name="custom",
        model_name="demo",
        model_metadata=ProviderModelMetadata(tool_feedback_mode="synthetic_user_message"),
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    feedback = messages[1]["content"]
    assert isinstance(feedback, str)
    assert raw_todo_content not in feedback
    assert '"content": ""' in feedback
    assert '"omitted": true' not in feedback
    assert '"byte_count"' not in feedback


def test_provider_adapter_infers_tool_feedback_when_metadata_omits_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenCodeGoModelProvider().turn_provider()
    request = _build_turn_request(model_name="opencode-go")
    tool_results = (
        ToolResult(
            tool_name="glob",
            status="ok",
            content="./\n  .voidcode.json",
            data={"tool_call_id": "glob_0", "arguments": {"pattern": "**/*"}},
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model="opencode-go/minimax-m2.7",
        provider_name="opencode-go",
        model_name="minimax-m2.7",
        model_metadata=ProviderModelMetadata(context_window=204_800),
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assert messages[1]["role"] == "user"
    feedback = messages[1]["content"]
    assert isinstance(feedback, str)
    assert "Completed tool calls for current request:" in feedback


def test_provider_adapter_infers_tool_feedback_from_mapped_model_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenCodeGoModelProvider(
        config=SimplifiedProviderConfig(
            api_key="opencode-go-key",
            model_map={"my-minimax": "minimax-m2.7"},
        )
    ).turn_provider()
    request = _build_turn_request(model_name="opencode-go")
    tool_results = (
        ToolResult(
            tool_name="glob",
            status="ok",
            content="./\n  .voidcode.json",
            data={"tool_call_id": "glob_0", "arguments": {"pattern": "**/*"}},
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model="opencode-go/my-minimax",
        provider_name="opencode-go",
        model_name="my-minimax",
        model_metadata=ProviderModelMetadata(context_window=204_800),
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload["model"] == "minimax-m2.7"
    assert payload["custom_llm_provider"] == "anthropic"
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assert messages[1]["role"] == "user"
    feedback = messages[1]["content"]
    assert isinstance(feedback, str)
    assert "Completed tool calls for current request:" in feedback


def test_opencode_go_non_openai_families_declare_synthetic_tool_feedback_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenCodeGoModelProvider().turn_provider()
    request = _build_turn_request(model_name="opencode-go")
    tool_results = (
        ToolResult(
            tool_name="glob",
            status="ok",
            content="./\n  .voidcode.json",
            data={"tool_call_id": "glob_0", "arguments": {"pattern": "**/*"}},
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model="opencode-go/minimax-m2.7",
        provider_name="opencode-go",
        model_name="minimax-m2.7",
        model_metadata=infer_model_metadata("opencode-go", "minimax-m2.7"),
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    messages_obj = payload.get("messages")
    assert isinstance(messages_obj, list)
    messages = cast(list[dict[str, object]], messages_obj)
    assert messages[1]["role"] == "user"
    feedback = messages[1]["content"]
    assert isinstance(feedback, str)
    assert "Completed tool calls for current request:" in feedback
    assert '"tool_name": "glob"' in feedback
    assert "tool_calls" not in messages[1]


def test_provider_adapter_logs_bounded_request_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="voidcode.provider.litellm_backend")
    provider = OpenCodeGoModelProvider().turn_provider()
    request = _build_turn_request(model_name="opencode-go")
    tool_results = (
        ToolResult(
            tool_name="glob",
            status="ok",
            content="./\n  .voidcode.json",
            data={"tool_call_id": "glob_0", "arguments": {"pattern": "**/*"}},
        ),
    )
    request = ProviderTurnRequest(
        assembled_context=_assembled_from_legacy(
            prompt=request.prompt,
            tool_results=tool_results,
            context_window=_StubContextWindow(
                prompt=request.context_window.prompt,
                tool_results=tool_results,
                compacted=True,
                retained_tool_result_count=1,
                _continuity_state=_StubContinuityState(summary_text="Compacted old result"),
            ),
            applied_skills=request.applied_skills,
        ),
        available_tools=request.available_tools,
        raw_model="opencode-go/minimax-m2.7",
        provider_name="opencode-go",
        model_name="minimax-m2.7",
        model_metadata=infer_model_metadata("opencode-go", "minimax-m2.7"),
        attempt=request.attempt,
        abort_signal=request.abort_signal,
    )
    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="done",
    )

    _ = provider.propose_turn(request)

    diagnostic_records = [
        record
        for record in caplog.records
        if record.message.startswith("provider request diagnostics:")
    ]
    assert diagnostic_records
    message = diagnostic_records[-1].message
    assert "provider=opencode-go" in message
    assert "model=minimax-m2.7" in message
    assert "messages=3" in message
    assert "retained_tool_results=1" in message
    assert "synthetic_tool_feedback_size=" in message
    assert "continuity_summary_size=" in message
    assert "largest_message=" in message


def test_provider_adapter_uses_runtime_assembled_context_for_agent_system_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider()
    provider = provider.turn_provider()
    request = _build_turn_request(model_name="openai")
    request = ProviderTurnRequest(
        assembled_context=_StubAssembledContext(
            prompt="read sample.txt",
            tool_results=(),
            continuity_state=None,
            segments=(
                ProviderContextSegment(role="system", content="Runtime agent system prompt."),
                ProviderContextSegment(role="user", content="read sample.txt"),
            ),
            metadata={},
        ),
        available_tools=request.available_tools,
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
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "Runtime agent system prompt."
    assert messages[1] == {"role": "user", "content": "read sample.txt"}


def test_provider_adapter_uses_runtime_assembled_context_for_model_family_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider()
    provider = provider.turn_provider()
    request = _build_turn_request(model_name="openai")
    request = ProviderTurnRequest(
        assembled_context=_StubAssembledContext(
            prompt="read sample.txt",
            tool_results=(),
            continuity_state=None,
            segments=(
                ProviderContextSegment(
                    role="system", content="Runtime model-family override prompt."
                ),
                ProviderContextSegment(role="user", content="read sample.txt"),
            ),
            metadata={},
        ),
        available_tools=request.available_tools,
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
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "Runtime model-family override prompt."
    assert messages[1] == {"role": "user", "content": "read sample.txt"}


def test_provider_adapter_uses_runtime_assembled_context_for_prompt_profile_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider()
    provider = provider.turn_provider()
    request = _build_turn_request(model_name="openai")
    request = ProviderTurnRequest(
        assembled_context=_StubAssembledContext(
            prompt="read sample.txt",
            tool_results=(),
            continuity_state=None,
            segments=(
                ProviderContextSegment(role="system", content="Runtime prompt-profile message."),
                ProviderContextSegment(role="user", content="read sample.txt"),
            ),
            metadata={},
        ),
        available_tools=request.available_tools,
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
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "Runtime prompt-profile message."
    assert messages[1] == {"role": "user", "content": "read sample.txt"}


def test_provider_adapter_uses_runtime_assembled_context_for_unknown_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider()
    provider = provider.turn_provider()
    request = _build_turn_request(model_name="openai")
    request = ProviderTurnRequest(
        assembled_context=_StubAssembledContext(
            prompt="read sample.txt",
            tool_results=(),
            continuity_state=None,
            segments=(
                ProviderContextSegment(role="system", content="Runtime custom-review prompt."),
                ProviderContextSegment(role="user", content="read sample.txt"),
            ),
            metadata={},
        ),
        available_tools=request.available_tools,
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
    assert messages[0] == {
        "role": "system",
        "content": "Runtime custom-review prompt.",
    }


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


def test_provider_adapter_passes_reasoning_effort_for_direct_litellm_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LiteLLMModelProvider(
        config=LiteLLMProviderConfig(base_url="http://localhost:4000")
    ).turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    _ = provider.propose_turn(_build_turn_request(model_name="litellm", reasoning_effort="high"))

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload["reasoning_effort"] == "high"


def test_glm_provider_passes_reasoning_effort_through_without_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GLMModelProvider(config=SimplifiedProviderConfig(api_key="glm-key")).turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    _ = provider.propose_turn(_build_turn_request(model_name="glm", reasoning_effort="high"))

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload["reasoning_effort"] == "high"
    assert "extra_body" not in payload
    assert "allowed_openai_params" not in payload


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
            assembled_context=_assembled_from_legacy(
                prompt="read sample.txt",
                tool_results=(),
                context_window=_StubContextWindow(prompt="read sample.txt", tool_results=()),
                applied_skills=(),
            ),
            available_tools=(
                ToolDefinition(name="read_file", description="read file", read_only=True),
            ),
            raw_model=f"opencode-go/{model_name}",
            provider_name="opencode-go",
            model_name=model_name,
            reasoning_effort="high",
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
    assert payload["timeout"] == 300.0
    assert "thinking" not in payload
    assert payload["reasoning_effort"] == "high"
    assert "extra_body" not in payload
    if model_name in {"minimax-m2.7", "minimax-m2.5"}:
        assert payload["extra_headers"] == {
            "anthropic-version": "2023-06-01",
            "user-agent": "@ai-sdk/anthropic",
        }
    assert payload.get("tools")
    assert payload.get("tool_choice") == "auto"


def test_opencode_go_glm_stream_turn_does_not_send_rejected_tool_stream_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voidcode.provider.config import SimplifiedProviderConfig

    provider = OpenCodeGoModelProvider(config=SimplifiedProviderConfig(api_key="opencode-go-key"))
    turn_provider = provider.turn_provider()
    assert isinstance(turn_provider, StreamableTurnProvider)

    _patch_litellm_completion(
        monkeypatch,
        mode="stream",
        stream_tool_chunks=(
            (
                [
                    {
                        "index": 0,
                        "id": "call-read",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"filePath":"README.md"}',
                        },
                    }
                ],
                None,
            ),
            (None, "tool_calls"),
        ),
    )

    events = list(
        turn_provider.stream_turn(
            ProviderTurnRequest(
                assembled_context=_assembled_from_legacy(
                    prompt="read README.md",
                    tool_results=(),
                    context_window=_StubContextWindow(prompt="read README.md", tool_results=()),
                    applied_skills=(),
                ),
                available_tools=(
                    ToolDefinition(name="read_file", description="read file", read_only=True),
                ),
                raw_model="opencode-go/glm-5.1",
                provider_name="opencode-go",
                model_name="glm-5.1",
                attempt=0,
                abort_signal=None,
            )
        )
    )

    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload["stream"] is True
    assert payload["custom_llm_provider"] == "openai"
    assert "extra_body" not in payload
    assert payload["tool_choice"] == "auto"

    tool_events = [event for event in events if event.channel == "tool"]
    assert len(tool_events) == 1
    assert tool_events[0].text is not None
    assert json.loads(tool_events[0].text) == {
        "arguments": {"filePath": "README.md"},
        "tool_call_id": "call-read",
        "tool_name": "read_file",
    }


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


def test_litellm_backend_propose_turn_forwards_ssl_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = LiteLLMBackendSingleAgentProvider(
        name="litellm",
        config=LiteLLMProviderConfig(
            api_key="litellm-key",
            base_url="https://litellm.local",
            ssl_verify=False,
        ),
    )

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    result = provider.propose_turn(_build_turn_request(model_name="litellm"))

    assert result.output == "ok"
    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload["ssl_verify"] is False


def test_litellm_backend_omits_ssl_verify_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LiteLLMBackendSingleAgentProvider(
        name="custom-gateway",
        config=LiteLLMProviderConfig(
            api_key="gateway-key",
            base_url="https://gateway.example.test",
        ),
    )

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    result = provider.propose_turn(_build_turn_request(model_name="custom-gateway"))

    assert result.output == "ok"
    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert "ssl_verify" not in payload


@pytest.mark.parametrize(
    ("first_provider", "second_provider", "first_key", "second_key"),
    [
        ("default", "explicit", None, False),
        ("explicit", "default", False, None),
    ],
)
def test_litellm_backend_ssl_verify_is_request_scoped_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
    first_provider: str,
    second_provider: str,
    first_key: bool | None,
    second_key: bool | None,
) -> None:
    import voidcode.provider.litellm_backend as backend_module

    explicit_provider = LiteLLMBackendSingleAgentProvider(
        name="explicit-gateway",
        config=LiteLLMProviderConfig(
            api_key="explicit-key",
            base_url="https://explicit.example.test",
            ssl_verify=False,
        ),
    )
    default_provider = LiteLLMBackendSingleAgentProvider(
        name="default-gateway",
        config=LiteLLMProviderConfig(
            api_key="default-key",
            base_url="https://default.example.test",
        ),
    )
    providers = {
        "explicit": explicit_provider,
        "default": default_provider,
    }
    request_models = {
        "explicit": "explicit-gateway",
        "default": "default-gateway",
    }
    entered_calls: dict[str, threading.Event] = {
        "explicit": threading.Event(),
        "default": threading.Event(),
    }
    release_calls: dict[str, threading.Event] = {
        "explicit": threading.Event(),
        "default": threading.Event(),
    }
    observations: list[tuple[str, object, object]] = []
    thread_errors: list[BaseException] = []

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )
    monkeypatch.setattr(
        backend_module.litellm_module,
        "ssl_verify",
        "module-sentinel",
        raising=False,
    )

    def _blocking_completion(*args: object, **kwargs: object) -> object:
        _ = args, kwargs
        api_base = kwargs.get("api_base")
        if api_base == "https://explicit.example.test/v1":
            observations.append(
                ("explicit", backend_module.litellm_module.ssl_verify, kwargs.get("ssl_verify"))
            )
            entered_calls["explicit"].set()
            assert release_calls["explicit"].wait(timeout=2)
        else:
            observations.append(
                ("default", backend_module.litellm_module.ssl_verify, kwargs.get("ssl_verify"))
            )
            entered_calls["default"].set()
            assert release_calls["default"].wait(timeout=2)
        return _StubCompletionResponse(content="ok")

    monkeypatch.setattr(backend_module.litellm_module, "completion", _blocking_completion)

    first_entered = entered_calls[first_provider]
    second_entered = entered_calls[second_provider]
    first_release = release_calls[first_provider]
    second_release = release_calls[second_provider]

    def _run_provider(provider_name: str) -> None:
        try:
            result = providers[provider_name].propose_turn(
                _build_turn_request(model_name=request_models[provider_name])
            )
            assert result.output == "ok"
        except BaseException as exc:  # pragma: no cover - re-raised in main test thread
            thread_errors.append(exc)
            raise

    first_thread = threading.Thread(target=_run_provider, args=(first_provider,))
    second_thread = threading.Thread(target=_run_provider, args=(second_provider,))
    first_thread.start()
    assert first_entered.wait(timeout=2)
    second_thread.start()
    assert second_entered.wait(timeout=2)
    second_release.set()
    first_release.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert first_thread.is_alive() is False
    assert second_thread.is_alive() is False

    assert thread_errors == []
    assert observations == [
        (
            first_provider,
            "module-sentinel",
            first_key,
        ),
        (
            second_provider,
            "module-sentinel",
            second_key,
        ),
    ]
    assert backend_module.litellm_module.ssl_verify == "module-sentinel"


def test_litellm_backend_stream_turn_forwards_ssl_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = LiteLLMBackendSingleAgentProvider(
        name="litellm",
        config=LiteLLMProviderConfig(
            api_key="litellm-key",
            base_url="https://litellm.local",
            ssl_verify=False,
        ),
    )

    _patch_litellm_completion(
        monkeypatch,
        mode="stream",
        stream_chunks=(("ok", "stop"),),
    )

    events = list(provider.stream_turn(_build_turn_request(model_name="litellm")))

    assert events[-1].kind == "done"
    payload_obj = _LAST_REQUEST_PAYLOAD.get("kwargs")
    assert isinstance(payload_obj, dict)
    payload = cast(dict[str, object], payload_obj)
    assert payload["ssl_verify"] is False


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
                "id": "read:file:1",
                "function": {
                    "name": "read_file",
                    "arguments": '{"filePath":"sample.txt"}',
                },
            }
        ],
    )

    result = provider.propose_turn(_build_turn_request(model_name="openai"))

    assert result.tool_call is not None
    assert result.tool_call.tool_name == "read_file"
    assert result.tool_call.arguments == {"filePath": "sample.txt"}
    assert result.tool_call.tool_call_id == "read_file_1"


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
                            "arguments": '{"filePath":"sample.txt"}',
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
            text=(
                '{"tool_name": "read_file", "arguments": {"filePath": "sample.txt"}, '
                '"tool_call_id": "read_file"}'
            ),
        ),
        ProviderStreamEvent(kind="done", done_reason="completed"),
    ]


def test_provider_adapter_stream_turn_emits_final_tool_snapshot_for_updates(
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
                            "arguments": '{"filePath":',
                        },
                    }
                ],
                None,
            ),
            (
                [
                    {
                        "index": 0,
                        "function": {
                            "arguments": '"sample.txt"}',
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
            text=(
                '{"tool_name": "read_file", "arguments": {"filePath": "sample.txt"}, '
                '"tool_call_id": "read_file"}'
            ),
        ),
        ProviderStreamEvent(kind="done", done_reason="completed"),
    ]


def test_provider_adapter_stream_turn_coalesces_tool_arguments_by_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIModelProvider().turn_provider()
    assert isinstance(provider, StreamableTurnProvider)

    _patch_litellm_completion(
        monkeypatch,
        mode="stream",
        stream_tool_chunks=(
            (
                [
                    {
                        "index": 0,
                        "function": {"name": "write_file", "arguments": '{"path":'},
                    },
                    {
                        "index": 1,
                        "function": {"name": "read_file", "arguments": '{"filePath":'},
                    },
                ],
                None,
            ),
            (
                [
                    {"index": 1, "function": {"arguments": '"sample.txt"}'}},
                    {"index": 0, "function": {"arguments": '"out.txt","content":"ok"}'}},
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
            text=(
                '{"tool_name": "write_file", '
                '"arguments": {"path": "out.txt", "content": "ok"}, '
                '"tool_call_id": "write_file"}'
            ),
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
        ProviderStreamEvent(
            kind="delta",
            channel="reasoning",
            text="Thinking step.",
            metadata={"source": "delta.reasoning"},
        ),
        ProviderStreamEvent(kind="delta", channel="text", text="Done."),
        ProviderStreamEvent(kind="done", done_reason="completed"),
    ]


def test_provider_adapter_stream_turn_emits_reasoning_events_from_reasoning(
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
                    reasoning="Reasoning step.",
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
        ProviderStreamEvent(
            kind="delta",
            channel="reasoning",
            text="Reasoning step.",
            metadata={"source": "delta.reasoning"},
        ),
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
        ProviderStreamEvent(
            kind="delta",
            channel="reasoning",
            text="Private chain.",
            metadata={"source": "delta.thinking_blocks"},
        ),
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
    turn_provider: TurnProvider = provider.turn_provider()

    _patch_litellm_completion(
        monkeypatch,
        mode="completion",
        completion_content="ok",
    )

    result = turn_provider.propose_turn(_build_turn_request(model_name=provider_name))

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
