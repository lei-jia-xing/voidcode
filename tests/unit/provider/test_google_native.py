from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest

from voidcode.core.transcript import AssembledContext, ContextSegment
from voidcode.provider.config import GoogleProviderConfig
from voidcode.provider.google_native import GoogleGenAIProvider
from voidcode.provider.model_catalog import ProviderModelMetadata
from voidcode.provider.protocol import ProviderExecutionError, ProviderTurnRequest


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
    prompt_feedback: object | None = None


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
    segments: tuple[ContextSegment, ...] = (ContextSegment(role="user", content="hello"),),
) -> ProviderTurnRequest:
    context = _Context(prompt="hello", segments=segments)
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


def test_google_replays_native_batches_as_grouped_contents() -> None:
    segments = (
        ContextSegment(role="user", content="first prompt"),
        ContextSegment(
            role="assistant",
            content=None,
            tool_call_id="read_1",
            tool_name="read",
            tool_arguments={"path": "a.txt"},
            tool_batch_id="batch-1",
        ),
        ContextSegment(
            role="assistant",
            content=None,
            tool_call_id="read_2",
            tool_name="read",
            tool_arguments={"path": "b.txt"},
            tool_batch_id="batch-1",
        ),
        ContextSegment(role="tool", content="a contents", tool_call_id="read_1", tool_name="read", tool_batch_id="batch-1"),
        ContextSegment(role="tool", content="b contents", tool_call_id="read_2", tool_name="read", tool_batch_id="batch-1"),
        ContextSegment(role="user", content="second prompt"),
        ContextSegment(
            role="assistant",
            content=None,
            tool_call_id="read_1",
            tool_name="read",
            tool_arguments={"path": "c.txt"},
            tool_batch_id="batch-2",
        ),
        ContextSegment(role="tool", content="c contents", tool_call_id="read_1", tool_name="read", tool_batch_id="batch-2"),
    )
    provider, _client = _provider(_Response(candidates=[]))

    system, contents = provider._contents(_request(segments=segments))

    assert system is None
    assert contents == [
        {"role": "user", "parts": [{"text": "first prompt"}]},
        {
            "role": "model",
            "parts": [
                {"function_call": {"name": "read", "args": {"path": "a.txt"}}},
                {"function_call": {"name": "read", "args": {"path": "b.txt"}}},
            ],
        },
        {
            "role": "user",
            "parts": [
                {"function_response": {"name": "read", "response": {"content": "a contents"}}},
                {"function_response": {"name": "read", "response": {"content": "b contents"}}},
            ],
        },
        {"role": "user", "parts": [{"text": "second prompt"}]},
        {"role": "model", "parts": [{"function_call": {"name": "read", "args": {"path": "c.txt"}}}]},
        {"role": "user", "parts": [{"function_response": {"name": "read", "response": {"content": "c contents"}}}]},
    ]


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


def test_stream_tool_call_ordinals_are_global_across_response_chunks() -> None:
    first = _Response(
        candidates=[
            _Candidate(
                content=_Content(parts=[_Part(function_call=_FunctionCall(name="read", args={"path": "a.txt"}))]),
                finish_reason=None,
            )
        ]
    )
    second = _Response(
        candidates=[
            _Candidate(
                content=_Content(parts=[_Part(function_call=_FunctionCall(name="read", args={"path": "b.txt"}))]),
                finish_reason="STOP",
            )
        ]
    )
    provider, client = _provider(first)
    client.models.generate_content_stream = lambda **kwargs: iter((first, second))

    events = tuple(provider.stream_turn(_request()))
    starts = [event for event in events if event.kind == "tool_call_start"]
    ends = [event for event in events if event.kind == "tool_call_end"]

    assert [event.tool_call_id for event in starts] == ["read_1", "read_2"]
    assert [event.tool_call_ordinal for event in starts] == [0, 1]
    assert [event.tool_call_id for event in ends] == ["read_1", "read_2"]
    assert [event.tool_call_ordinal for event in ends] == [0, 1]
    assert events[-1].kind == "done"


def test_google_missing_candidates_and_prompt_block_are_provider_errors() -> None:
    missing, _client = _provider(_Response(candidates=[]))
    with pytest.raises(ProviderExecutionError) as missing_error:
        missing.propose_turn(_request())
    assert missing_error.value.kind == "provider_error"
    assert missing_error.value.details["reason"] == "malformed_response"

    blocked, _client = _provider(_Response(candidates=[], prompt_feedback=SimpleNamespace(block_reason="SAFETY")))
    with pytest.raises(ProviderExecutionError) as blocked_error:
        blocked.propose_turn(_request())
    assert blocked_error.value.kind == "provider_error"
    assert blocked_error.value.details["reason"] == "prompt_blocked"

    with pytest.raises(ProviderExecutionError) as stream_error:
        list(missing.stream_turn(_request()))
    assert stream_error.value.kind == "provider_error"
    assert stream_error.value.details["reason"] == "malformed_response"
