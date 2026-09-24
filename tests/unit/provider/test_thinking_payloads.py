"""Per-wire request fields for the thinking rules: what the adapters actually send.

Every case asserts the request the wire receives (the payload dict, the SDK
config, or the Anthropic body), never the config or the data row: the row is the
input, the request field is the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from voidcode.provider.anthropic_native import AnthropicMessagesProvider, AnthropicMessagesTransport
from voidcode.provider.config import AnthropicProviderConfig, GoogleProviderConfig, OpenAICompatibleProviderConfig
from voidcode.provider.google_native import GoogleGenAIProvider
from voidcode.provider.model_catalog import ProviderModelMetadata, static_catalog_metadata
from voidcode.provider.openai_native import OpenAIChatCompletionsProvider
from voidcode.provider.protocol import ProviderAssembledContext, ProviderContextSegment, ProviderTurnRequest
from voidcode.tools.contracts import ToolDefinition


@dataclass(frozen=True, slots=True)
class _Context:
    prompt: str = "answer"
    segments: tuple[ProviderContextSegment, ...] = ()
    tool_results: tuple[object, ...] = ()
    continuity_state: object | None = None
    metadata: dict[str, object] = None  # type: ignore[assignment]


def _request(provider: str, model: str, *, effort: str | None, tools: bool = False) -> ProviderTurnRequest:
    metadata: ProviderModelMetadata | None = static_catalog_metadata(provider, model)
    return ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, _Context(segments=(ProviderContextSegment(role="user", content="hi"),))),
        available_tools=(ToolDefinition(name="read", description="read", input_schema={"type": "object"}),) if tools else (),
        provider_name=provider,
        model_name=model,
        reasoning_effort=effort,
        model_metadata=metadata,
    )


class _AnthropicTransport:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def request(self, payload: dict[str, object], *, timeout_seconds: float) -> object:
        _ = timeout_seconds
        self.payloads.append(payload)
        return {"id": "m", "type": "message", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}


def _openai_payload(provider: str, model: str, *, effort: str | None, tools: bool = False) -> dict[str, object]:
    adapter = OpenAIChatCompletionsProvider(name=provider, config=OpenAICompatibleProviderConfig(api_key="k"))
    return adapter._payload(_request(provider, model, effort=effort, tools=tools), stream=False)


def _anthropic_payload(provider: str, model: str, *, effort: str | None) -> dict[str, object]:
    transport = _AnthropicTransport()
    adapter = AnthropicMessagesProvider(
        name=provider,
        config=AnthropicProviderConfig(api_key="k"),
        transport=cast(AnthropicMessagesTransport, transport),
    )
    adapter.propose_turn(_request(provider, model, effort=effort))
    return transport.payloads[0]


class _Models:
    def __init__(self) -> None:
        self.last_config: Any | None = None

    def generate_content(self, **kwargs: object) -> object:
        self.last_config = kwargs.get("config")
        return type("_Response", (), {"text": "ok", "function_calls": (), "usage_metadata": None})()


class _Client:
    def __init__(self) -> None:
        self.models = _Models()


def _google_thinking(provider: str, model: str, *, effort: str | None) -> Any:
    client = _Client()
    adapter = GoogleGenAIProvider(name=provider, config=GoogleProviderConfig(), client=client)
    adapter.propose_turn(_request(provider, model, effort=effort))
    return client.models.last_config.thinking_config


def test_zai_sends_its_binary_thinking_switch_and_never_reasoning_effort() -> None:
    enabled = _openai_payload("zai", "glm-5", effort="high")
    disabled = _openai_payload("zai", "glm-5", effort="off")

    assert enabled["extra_body"] == {"thinking": {"type": "enabled"}}
    assert disabled["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in enabled
    assert "reasoning_effort" not in disabled


def test_deepseek_sends_the_level_and_no_output_cap_by_default() -> None:
    payload = _openai_payload("deepseek", "deepseek-v4-pro", effort="high")

    # OMP sends no output cap unless the caller asks for one, and deepseek is not
    # the kimi family, so neither field name appears.
    assert payload["reasoning_effort"] == "high"
    assert "max_tokens" not in payload
    assert "max_completion_tokens" not in payload


def test_the_kimi_family_always_sends_its_cap_under_its_own_field_name() -> None:
    """The one always-send exception, and the field name that carries it:
    ``max-tokens-field "max_tokens"`` for moonshot (resolve.ts:405, :497)."""
    payload = _openai_payload("moonshot", "kimi-k2.6", effort="high")
    metadata = static_catalog_metadata("moonshot", "kimi-k2.6")
    assert metadata is not None and metadata.max_output_tokens is not None

    assert payload["max_tokens"] == metadata.max_output_tokens
    assert "max_completion_tokens" not in payload


def test_qwen_sends_a_plain_reasoning_effort_for_a_reasoning_model() -> None:
    payload = _openai_payload("qwen", "glm-5", effort="medium")

    assert payload["reasoning_effort"] == "medium"
    assert "extra_body" not in payload


def test_a_qwen_model_the_catalog_says_cannot_reason_gets_no_knob() -> None:
    # No provider-name deny list any more: the catalog's own row decides, so the
    # same provider sends the field for one model and withholds it for another.
    metadata = static_catalog_metadata("qwen", "qwen-max")
    assert metadata is not None and metadata.supports_reasoning is False

    payload = _openai_payload("qwen", "qwen-max", effort="medium")

    assert "reasoning_effort" not in payload


def test_moonshot_uses_the_thinking_format_and_the_k3_override() -> None:
    # The provider default is the zai format: a binary body switch.
    default = _openai_payload("moonshot", "kimi-k2.6", effort="high")
    assert default["extra_body"] == {"thinking": {"type": "enabled"}}
    # The k3 family overrides the format, so its rows carry an effort value again.
    k3 = _openai_payload("moonshot", "kimi-k3", effort="off")
    assert k3["reasoning_effort"] == "low"
    assert "extra_body" not in k3


def test_anthropic_uses_the_budget_table_for_thinking() -> None:
    payload = _anthropic_payload("anthropic", "claude-opus-4-5", effort="high")

    # ANTHROPIC_THINKING (stream.ts:1813-1820): high is 16384 (the pre-W3 voidcode
    # table said 8192).
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert payload["max_tokens"] >= 16384 + 4000


def test_anthropic_never_lets_thinking_shrink_the_cap() -> None:
    """The R2 regression: enabling thinking must not lower the cap below the
    model's own maximum (the bug sent 64000 for a 128000-cap model)."""
    off = _anthropic_payload("anthropic", "claude-opus-4-8", effort="off")
    high = _anthropic_payload("anthropic", "claude-opus-4-8", effort="high")
    metadata = static_catalog_metadata("anthropic", "claude-opus-4-8")
    assert metadata is not None and metadata.max_output_tokens is not None

    assert high["max_tokens"] == metadata.max_output_tokens
    assert high["max_tokens"] >= off["max_tokens"]


def test_anthropic_keeps_a_small_cap_at_the_models_own_maximum() -> None:
    """``ensureMaxTokensForThinking`` (anthropic.ts:3602-3625): the cap is always
    ``min(desired, modelMaxTokens)`` -- 64000 is the unknown-model fallback, never
    a ceiling over a known larger model."""
    transport = _AnthropicTransport()
    adapter = AnthropicMessagesProvider(
        name="anthropic",
        config=AnthropicProviderConfig(api_key="k"),
        transport=cast(AnthropicMessagesTransport, transport),
    )
    request = ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, _Context(segments=(ProviderContextSegment(role="user", content="hi"),))),
        provider_name="anthropic",
        model_name="claude-opus-4-5",
        reasoning_effort="high",
        model_metadata=ProviderModelMetadata(
            max_output_tokens=8000,
            supports_reasoning=True,
            supports_reasoning_effort=True,
            supported_effort_levels=("low", "medium", "high"),
        ),
    )

    adapter.propose_turn(request)

    # The model's own maximum (8000) is the ceiling, so the budget cannot push the
    # cap past it -- and 64000 is never used as a cap here.
    assert transport.payloads[0]["max_tokens"] == 8000


def test_anthropic_omits_the_thinking_block_for_off() -> None:
    payload = _anthropic_payload("anthropic", "claude-opus-4-5", effort="off")

    assert "thinking" not in payload


def test_google_budget_and_level_models_read_their_own_knob() -> None:
    budget = _google_thinking("google", "gemini-2.5-pro", effort="high")
    level = _google_thinking("google", "gemini-3.5-flash", effort="high")

    assert budget.thinking_budget == 16384  # GOOGLE_THINKING (stream.ts:1822-1829)
    assert level.thinking_level.name == "HIGH"


def test_openai_sends_the_effort_and_no_output_cap() -> None:
    payload = _openai_payload("openai", "gpt-5.4", effort="xhigh")

    assert payload["reasoning_effort"] == "xhigh"
    assert "max_completion_tokens" not in payload
    assert "max_tokens" not in payload


def test_a_model_that_cannot_reason_is_never_handed_the_knob() -> None:
    metadata = static_catalog_metadata("openai", "gpt-4o")
    assert metadata is not None and metadata.supports_reasoning is False

    payload = _openai_payload("openai", "gpt-4o", effort="high")

    assert "reasoning_effort" not in payload
    assert "extra_body" not in payload


def test_an_unknown_model_still_gets_the_effort_it_asked_for() -> None:
    """No catalog row means no verdict, not a deny: the request is forwarded as asked."""
    payload = _openai_payload("openai", "a-model-not-in-the-catalog", effort="high")

    assert payload["reasoning_effort"] == "high"


def test_the_minimum_output_floor_never_exceeds_the_models_maximum() -> None:
    """A model whose own maximum is below the 1024 floor must keep its maximum."""
    transport = _AnthropicTransport()
    adapter = AnthropicMessagesProvider(
        name="anthropic",
        config=AnthropicProviderConfig(api_key="k"),
        transport=cast(AnthropicMessagesTransport, transport),
    )
    request = ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, _Context(segments=(ProviderContextSegment(role="user", content="hi"),))),
        provider_name="anthropic",
        model_name="claude-opus-4-5",
        reasoning_effort="high",
        model_metadata=ProviderModelMetadata(
            max_output_tokens=1000,
            supports_reasoning=True,
            supports_reasoning_effort=True,
            supported_effort_levels=("low", "medium", "high"),
        ),
    )

    adapter.propose_turn(request)

    assert transport.payloads[0]["max_tokens"] == 1000
