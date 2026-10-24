"""Reasoning replay and reasoning-effort rules of the ``providers`` plugin.

Every assertion here is a wire fact recorded by a real loopback HTTP server: a
replayed tool batch carries the provider's own persisted reasoning content
verbatim when that turn reasoned, and never fabricates the field when the
vendor legitimately returned zero reasoning tokens.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.capabilities.providers.dispatch import RegisteredProvider, build_registered_providers
from voidcode.sdk.provider import ProviderExecutionError, ProviderMessage, ProviderTurnRequest
from voidcode.sdk.tools import ToolCall, ToolDefinition

#: A reasoning string with the shapes a naive implementation would normalise
#: away: leading/trailing whitespace, interior newline, quoted text, non-ASCII.
_AUTHENTIC_REASONING = '  weigh the two options\npick "b" \u2014 caf\u00e9  '


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


def _base_url(server: OpenAILoopbackServer) -> str:
    return f"http://127.0.0.1:{server.server_port}/v1"


def _entries(server: OpenAILoopbackServer, *, provider: str = "deepseek", **extra: object) -> dict[str, object]:
    entry: dict[str, object] = {"api_key": f"{provider}-loopback-key", "base_url": _base_url(server)}
    entry.update(extra)
    return {provider: entry}


def _provider(entries: dict[str, object], *, name: str = "deepseek") -> RegisteredProvider:
    providers = {provider.name: provider for provider in build_registered_providers({"providers": entries})}
    assert name in providers
    return providers[name]


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="read",
        description="Read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )


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


def _replayed_turn(
    *,
    reasoning: str | None = None,
    assistant_metadata: Mapping[str, object] | None = None,
    provider_name: str = "deepseek",
    model_name: str = "deepseek-chat",
    **overrides: object,
) -> ProviderTurnRequest:
    """One tool batch already replayed: assistant call, then its result."""
    return _request(
        ProviderMessage("user", "read a.txt"),
        ProviderMessage(
            "assistant",
            tool_calls=(ToolCall("read", {"path": "a.txt"}, "call-1"),),
            reasoning=reasoning,
            metadata={} if assistant_metadata is None else assistant_metadata,
        ),
        ProviderMessage("tool", content="file contents", tool_call_id="call-1", tool_name="read"),
        available_tools=(_tool(),),
        provider_name=provider_name,
        model_name=model_name,
        raw_model=f"{provider_name}/{model_name}",
        **overrides,
    )


def _wire_requests(server: OpenAILoopbackServer) -> Sequence[dict[str, object]]:
    return server.requests


def _assistant_message(server: OpenAILoopbackServer) -> Mapping[str, object]:
    request = _wire_requests(server)[0]
    messages = request["messages"]
    assert isinstance(messages, list)
    assistant = next(message for message in messages if isinstance(message, Mapping) and message.get("role") == "assistant")
    return assistant


def test_tool_call_replay_without_reasoning_sends_the_recorded_shape_without_synthesis(loopback_server: OpenAILoopbackServer) -> None:
    """A genuine zero-reasoning tool turn replays without a fabricated field.

    DeepSeek returns tool-call turns with ``reasoning_tokens: 0`` for some
    requests even though thinking is enabled; the persisted absence is the
    provider's own shape and must reach the wire as absence.
    """
    provider = _provider(_entries(loopback_server))
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))

    provider.propose_turn(_replayed_turn())

    assistant = _assistant_message(loopback_server)
    assert "reasoning_content" not in assistant


@pytest.mark.parametrize(
    ("reasoning", "assistant_metadata"),
    [
        pytest.param(None, None, id="no reasoning at all"),
        pytest.param("", None, id="empty reasoning string"),
        pytest.param(
            None,
            {"data": {"reasoning_content": "legacy-carried thought"}},
            id="reasoning smuggled through the tool result's own data",
        ),
        pytest.param(None, {"source": "replayed_conversation"}, id="replay marked as a replayed conversation"),
    ],
)
def test_cross_provider_replay_without_authentic_reasoning_never_synthesizes_a_field(
    loopback_server: OpenAILoopbackServer,
    reasoning: str | None,
    assistant_metadata: Mapping[str, object] | None,
) -> None:
    """A turn whose history came from somewhere else has no authentic reasoning to replay.

    Nothing else can stand in for it: an empty string is not reasoning content,
    and a ``reasoning_content`` field carried by the tool result's own data is
    untrusted payload the model or transport could have written. The request
    proceeds with the field absent rather than fabricating the provider's own
    thinking.
    """
    provider = _provider(_entries(loopback_server))
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))

    provider.propose_turn(_replayed_turn(reasoning=reasoning, assistant_metadata=assistant_metadata))

    assert "reasoning_content" not in _assistant_message(loopback_server)


def test_reasoning_on_an_assistant_turn_without_tool_calls_is_replayed_with_tools(loopback_server: OpenAILoopbackServer) -> None:
    """DeepSeek's tools contract requires prior reasoning on every assistant turn.

    The request carries tools, so an earlier assistant answer that reasoned but
    called no tool must still carry its authentic reasoning_content.
    """
    provider = _provider(_entries(loopback_server))
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))

    provider.propose_turn(
        _request(
            ProviderMessage("user", "read a.txt"),
            ProviderMessage("assistant", content="I will read it.", reasoning=_AUTHENTIC_REASONING),
            ProviderMessage("user", "go ahead"),
            available_tools=(_tool(),),
            provider_name="deepseek",
            model_name="deepseek-chat",
            raw_model="deepseek/deepseek-chat",
        )
    )

    assistant = _assistant_message(loopback_server)
    assert assistant["reasoning_content"] == _AUTHENTIC_REASONING


def test_authentic_reasoning_is_replayed_verbatim_and_tool_choice_is_omitted(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider(_entries(loopback_server))
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))

    provider.propose_turn(_replayed_turn(reasoning=_AUTHENTIC_REASONING))

    assistant = _assistant_message(loopback_server)
    assert assistant["reasoning_content"] == _AUTHENTIC_REASONING
    # A model that requires reasoning content is never asked to choose a tool.
    assert "tool_choice" not in _wire_requests(loopback_server)[0]


def test_a_model_map_alias_onto_a_reasoning_locked_model_replays_without_synthesis(loopback_server: OpenAILoopbackServer) -> None:
    """The mapped name is what reaches the provider, so the mapped model's rule decides."""
    entries = _entries(loopback_server, model_map={"legacy-ds": "deepseek-v4-pro"})
    provider = _provider(entries)
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))

    provider.propose_turn(_replayed_turn(model_name="legacy-ds"))

    assert "reasoning_content" not in _assistant_message(loopback_server)

    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))
    provider.propose_turn(_replayed_turn(reasoning=_AUTHENTIC_REASONING, model_name="legacy-ds"))

    wire_request = _wire_requests(loopback_server)[1]
    assert wire_request["model"] == "deepseek-v4-pro"
    assistant = next(message for message in wire_request["messages"] if message.get("role") == "assistant")
    assert assistant["reasoning_content"] == _AUTHENTIC_REASONING


def test_a_provider_without_the_reasoning_requirement_replays_without_it(loopback_server: OpenAILoopbackServer) -> None:
    """The rule, not the provider name, decides: zai gets the standard tool replay."""
    provider = _provider(_entries(loopback_server, provider="zai"), name="zai")
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop"))

    provider.propose_turn(_replayed_turn(provider_name="zai", model_name="glm-4.5"))

    assistant = _assistant_message(loopback_server)
    assert "reasoning_content" not in assistant
    assert _wire_requests(loopback_server)[0]["tool_choice"] == "auto"


@pytest.mark.parametrize(
    ("requested", "on_the_wire"),
    [("minimal", "low"), ("low", "low"), ("medium", "low"), ("high", "high"), ("xhigh", "high"), ("max", "max")],
)
def test_unsupported_reasoning_effort_is_clamped_to_a_supported_level(
    loopback_server: OpenAILoopbackServer,
    requested: str,
    on_the_wire: str,
) -> None:
    """``deepseek-v4-pro`` ships low/high/max: a level between them snaps down, one below all snaps up."""
    provider = _provider(_entries(loopback_server))
    loopback_server.replies.append(openai_response(message={"content": "ok"}, finish="stop"))

    provider.propose_turn(
        _request(
            ProviderMessage("user", "hi"),
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            raw_model="deepseek/deepseek-v4-pro",
            reasoning_effort=requested,
        )
    )

    assert _wire_requests(loopback_server)[0]["reasoning_effort"] == on_the_wire


def test_a_binary_thinking_provider_spells_effort_as_its_own_thinking_switch(loopback_server: OpenAILoopbackServer) -> None:
    """zai's row is a binary thinking knob, so the ladder level never reaches the wire."""
    provider = _provider(_entries(loopback_server, provider="zai"), name="zai")
    loopback_server.replies.append(openai_response(message={"content": "ok"}, finish="stop"))
    loopback_server.replies.append(openai_response(message={"content": "ok"}, finish="stop"))

    provider.propose_turn(_request(ProviderMessage("user", "hi"), provider_name="zai", model_name="glm-4.5", reasoning_effort="off"))
    provider.propose_turn(_request(ProviderMessage("user", "hi"), provider_name="zai", model_name="glm-4.5", reasoning_effort="high"))

    assert _wire_requests(loopback_server)[0]["thinking"] == {"type": "disabled"}
    assert _wire_requests(loopback_server)[1]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in _wire_requests(loopback_server)[0]


def test_explicit_off_uses_the_none_level_declared_upstream(loopback_server: OpenAILoopbackServer) -> None:
    """The current upstream Grok effort list explicitly supports none."""
    provider = _provider(_entries(loopback_server, provider="xai"), name="xai")
    loopback_server.replies.append(openai_response(message={"content": "ok"}, finish="stop"))

    provider.propose_turn(_request(ProviderMessage("user", "hi"), provider_name="xai", model_name="grok-4.3", reasoning_effort="off"))

    assert _wire_requests(loopback_server)[0]["reasoning_effort"] == "none"
    assert "thinking" not in _wire_requests(loopback_server)[0]


def test_a_model_that_cannot_reason_gets_no_reasoning_knob(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider(_entries(loopback_server, provider="openai"), name="openai")
    loopback_server.replies.append(openai_response(message={"content": "ok"}, finish="stop"))

    provider.propose_turn(
        _request(
            ProviderMessage("user", "hi"),
            provider_name="openai",
            model_name="gpt-4o-mini",
            raw_model="openai/gpt-4o-mini",
            reasoning_effort="high",
        )
    )

    wire_request = _wire_requests(loopback_server)[0]
    assert "reasoning_effort" not in wire_request
    assert "thinking" not in wire_request
    assert "extra_body" not in wire_request


def test_an_unreported_or_unrecognized_finish_reason_never_becomes_completed(loopback_server: OpenAILoopbackServer) -> None:
    provider = _provider(_entries(loopback_server))

    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="some-future-reason"))
    unrecognized = provider.propose_turn(_request(ProviderMessage("user", "hi")))
    assert unrecognized.done_reason == "unknown"
    assert unrecognized.finish_reason_reported is True
    assert unrecognized.metadata is not None
    assert unrecognized.metadata["request_id"] == "chatcmpl-loopback"

    loopback_server.replies.append(openai_response(message={"content": "done"}, finish=None))  # type: ignore[arg-type]
    unreported = provider.propose_turn(_request(ProviderMessage("user", "hi")))
    assert unreported.done_reason == "unknown"
    assert unreported.finish_reason_reported is False
    assert unreported.metadata is not None
    assert unreported.metadata["finish_reason_reported"] is False

    # A provider-reported error terminal is a typed failure, never a completed turn either.
    loopback_server.replies.append(openai_response(message={"content": "boom"}, finish="error"))
    with pytest.raises(ProviderExecutionError) as excinfo:
        provider.propose_turn(_request(ProviderMessage("user", "hi")))

    assert excinfo.value.kind == "provider_error"
    assert excinfo.value.retryable is False
    assert excinfo.value.details is not None
    assert excinfo.value.details["source"] == "finish_reason"
    assert excinfo.value.details["finish_reason_raw"] == "error"


def test_usage_none_and_zero_are_preserved_as_distinct_facts(loopback_server: OpenAILoopbackServer) -> None:
    """``None`` is "not reported"; ``0`` is an observed zero. Neither may become the other."""
    provider = _provider(_entries(loopback_server))

    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop", usage=None))
    assert provider.propose_turn(_request(ProviderMessage("user", "hi"))).usage is None

    loopback_server.replies.append(
        openai_response(
            message={"content": "done"},
            finish="stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0}},
        )
    )
    observed_zero = provider.propose_turn(_request(ProviderMessage("user", "hi"))).usage
    assert observed_zero is not None
    assert (observed_zero.input_tokens, observed_zero.output_tokens, observed_zero.cache_read_tokens) == (0, 0, 0)
    assert observed_zero.uncached_input_tokens == 0

    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop", usage={"prompt_tokens": 0}))
    partial = provider.propose_turn(_request(ProviderMessage("user", "hi"))).usage
    assert partial is not None
    assert partial.input_tokens == 0
    assert partial.output_tokens is None
    assert partial.cache_read_tokens is None
    assert partial.uncached_input_tokens is None


def test_an_observed_zero_is_priced_zero_while_an_unpriced_row_stays_unknown(loopback_server: OpenAILoopbackServer) -> None:
    """The catalog distinguishes "cost 0" from "cannot price": a row with no rates stays ``None``."""
    zeros = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0}}

    priced_provider = _provider(_entries(loopback_server, provider="zai"), name="zai")
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop", usage=zeros))
    priced = priced_provider.propose_turn(
        _request(ProviderMessage("user", "hi"), provider_name="zai", model_name="glm-4.5", raw_model="zai/glm-4.5")
    ).usage
    assert priced is not None
    assert priced.cost_usd == 0.0

    unpriced_provider = _provider(_entries(loopback_server))
    loopback_server.replies.append(openai_response(message={"content": "done"}, finish="stop", usage=zeros))
    unpriced = unpriced_provider.propose_turn(_request(ProviderMessage("user", "hi"))).usage
    assert unpriced is not None
    assert unpriced.input_tokens == 0
    assert unpriced.cost_usd is None
