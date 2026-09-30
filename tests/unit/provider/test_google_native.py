from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from voidcode.core.transcript import AssembledContext, ContextSegment
from voidcode.provider.config import GoogleProviderConfig
from voidcode.provider.google_native import GoogleGenAIProvider
from voidcode.provider.model_catalog import ProviderModelMetadata
from voidcode.provider.protocol import (
    ProviderTurnRequest,
)


@dataclass(frozen=True)
class _Context:
    prompt: str
    segments: tuple[ContextSegment, ...]
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _FunctionCall:
    name: str
    args: dict[str, object]


@dataclass(frozen=True)
class _Part:
    function_call: _FunctionCall | None = None
    text: str | None = None
    thought: bool = False


@dataclass(frozen=True)
class _Content:
    parts: list[_Part]


@dataclass(frozen=True)
class _Candidate:
    content: _Content
    finish_reason: object = "STOP"


@dataclass(frozen=True)
class _Response:
    candidates: list[_Candidate]
    text: str = ""


class _FakeModels:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.non_stream_calls = 0
        self.stream_calls = 0
        self.last_config: Any | None = None

    def generate_content(self, **kwargs: object) -> _Response:
        self.non_stream_calls += 1
        self.last_config = kwargs.get("config")
        return self.response

    def generate_content_stream(self, **kwargs: object) -> Any:
        self.stream_calls += 1
        self.last_config = kwargs.get("config")
        return iter([self.response])


class _FakeClient:
    def __init__(self, response: _Response) -> None:
        self.models = _FakeModels(response)


def _request(
    *,
    tools: tuple[object, ...] = (),
    model_name: str = "gemini-2.5-flash",
    reasoning_effort: str | None = None,
    model_metadata: ProviderModelMetadata | None = None,
    session_id: str | None = None,
) -> ProviderTurnRequest:
    context = _Context(prompt="hello", segments=(ContextSegment(role="user", content="hello"),))
    return ProviderTurnRequest(
        assembled_context=cast(AssembledContext, context),
        available_tools=cast(Any, tools),
        provider_name="google",
        model_name=model_name,
        reasoning_effort=reasoning_effort,
        model_metadata=model_metadata,
        session_id=session_id,
    )


def _response_with_repeated_function_names() -> _Response:
    return _Response(
        candidates=[
            _Candidate(
                content=_Content(
                    parts=[
                        _Part(function_call=_FunctionCall(name="read", args={"path": "a.txt"})),
                        _Part(text="calling read twice"),
                        _Part(function_call=_FunctionCall(name="read", args={"path": "b.txt"})),
                        _Part(function_call=_FunctionCall(name="grep", args={"query": "cta"})),
                    ]
                )
            )
        ],
        text="",
    )


def _provider(response: _Response) -> tuple[GoogleGenAIProvider, _FakeClient]:
    client = _FakeClient(response)
    return GoogleGenAIProvider(config=GoogleProviderConfig(), client=client), client


def test_non_stream_tool_calls_get_distinct_ids_and_keep_response_order() -> None:
    provider, _client = _provider(_response_with_repeated_function_names())

    result = provider.propose_turn(_request())

    assert [call.tool_name for call in result.tool_calls] == ["read", "read", "grep"]
    assert [call.arguments for call in result.tool_calls] == [
        {"path": "a.txt"},
        {"path": "b.txt"},
        {"query": "cta"},
    ]
    ids = [call.tool_call_id for call in result.tool_calls]
    assert ids == ["read_1", "read_2", "grep_3"]
    assert len(set(ids)) == len(ids)
