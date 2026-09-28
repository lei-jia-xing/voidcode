"""Wire routing: the ``api_routes`` matcher and the gateways' per-model dispatch.

The shipped route data and the generated catalog row decide which wire a model
speaks; these tests pin the matcher semantics and that the resolved wire selects
the adapter VoidCode implements (and fails typed on the ones it does not).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import pytest

from voidcode.provider import api_routes, model_match
from voidcode.provider.anthropic_native import AnthropicMessagesProvider
from voidcode.provider.api_routes import ApiRoute
from voidcode.provider.google_native import GoogleGenAIProvider
from voidcode.provider.model_catalog import ProviderModelMetadata
from voidcode.provider.model_routing import RoutedTurnProvider
from voidcode.provider.openai_native import OpenAIChatCompletionsProvider
from voidcode.provider.opencode import OpenCodeZenModelProvider
from voidcode.provider.opencode_go import OpenCodeGoModelProvider
from voidcode.provider.protocol import ProviderExecutionError, ProviderTurnRequest


@dataclass(frozen=True, slots=True)
class _Context:
    prompt: str = "hello"
    tool_results: tuple[object, ...] = ()
    continuity_state: object | None = None
    segments: tuple[object, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


def _request(model: str, provider: str, metadata: ProviderModelMetadata | None = None) -> ProviderTurnRequest:
    return ProviderTurnRequest(
        assembled_context=_Context(),
        provider_name=provider,
        model_name=model,
        model_metadata=metadata,
    )


def _route(value: str, matcher: str, *, strip_prefix: bool = False) -> ApiRoute:
    return ApiRoute(provider="test", matcher=matcher, value=value, api="anthropic-messages", strip_prefix=strip_prefix)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("matcher", "value", "model", "expected"),
    [
        ("exact", "gpt-5.4", "gpt-5.4", True),
        ("exact", "gpt-5.4", "gpt-5.4-pro", False),
        ("prefix", "claude-", "claude-opus-5", True),
        ("prefix", "claude-", "grok-4.6", False),
        ("substring", "spark", "muse-spark-1.3", True),
        ("substring", "spark", "glm-5.3", False),
        # token and glob match the lowercased id.
        ("token", "codex", "GPT-5.3-CODEX", True),
        ("token", "code", "gpt-5.3-codex", False),
        ("glob", "claude-*", "claude-opus-5", True),
        ("glob", "claude-*-free", "claude-opus-5-free", True),
        ("glob", "claude-*-free", "claude-opus-5", False),
        # exact/prefix/substring compare the raw id, case included.
        ("prefix", "claude-", "Claude-opus-5", False),
    ],
)
def test_matcher_semantics(matcher: str, value: str, model: str, expected: bool) -> None:
    assert model_match.matches(matcher, value, model) is expected  # type: ignore[arg-type]


def test_first_match_wins_and_strip_prefix_rewrites_the_request_model() -> None:
    routes = (
        ApiRoute(provider="test", matcher="prefix", value="anthropic/", api="anthropic-messages", strip_prefix=True),
        ApiRoute(provider="test", matcher="prefix", value="anthropic/claude-", api="openai-completions"),
    )

    match = api_routes._match_route(routes, "anthropic/claude-opus-5")

    assert match is not None
    # The first declaration wins even though the second one is more specific.
    assert match.api == "anthropic-messages"
    assert match.request_model_id == "claude-opus-5"


def test_go_gateway_sends_minimax_m3_over_chat_completions() -> None:
    """The one real divergence the OMP table settled: Go's ``minimax-m3`` is
    chat-completions there, not Anthropic, despite the upstream npm hint."""
    routed = cast(RoutedTurnProvider, OpenCodeGoModelProvider().turn_provider())

    provider, _ = routed._dispatch(_request("minimax-m3", "opencode-go"))

    assert isinstance(provider, OpenAIChatCompletionsProvider)


def test_gateway_dispatch_picks_the_adapter_its_catalog_row_names() -> None:
    routed = cast(RoutedTurnProvider, OpenCodeZenModelProvider().turn_provider())

    assert isinstance(routed._dispatch(_request("claude-opus-5", "opencode-zen"))[0], AnthropicMessagesProvider)
    assert isinstance(routed._dispatch(_request("gemini-3-flash", "opencode-zen"))[0], GoogleGenAIProvider)
    assert isinstance(routed._dispatch(_request("glm-5.1", "opencode-zen"))[0], OpenAIChatCompletionsProvider)


def test_request_metadata_wire_wins_over_the_shipped_row() -> None:
    routed = cast(RoutedTurnProvider, OpenCodeZenModelProvider().turn_provider())
    metadata = ProviderModelMetadata(api="anthropic-messages")

    provider, _ = routed._dispatch(_request("a-model-not-in-the-catalog", "opencode-zen", metadata))

    assert isinstance(provider, AnthropicMessagesProvider)


def test_an_unpinned_model_falls_back_to_the_provider_wire() -> None:
    routed = cast(RoutedTurnProvider, OpenCodeZenModelProvider().turn_provider())

    provider, _ = routed._dispatch(_request("some-future-model", "opencode-zen"))

    assert isinstance(provider, OpenAIChatCompletionsProvider)


@pytest.mark.parametrize(
    ("provider_id", "model"),
    [("opencode-zen", "gpt-5.6-luna"), ("opencode-zen", "muse-spark-1.3"), ("opencode-go", "grok-4.6")],
)
def test_a_wire_voidcode_does_not_implement_fails_typed(provider_id: str, model: str) -> None:
    adapter = OpenCodeZenModelProvider() if provider_id == "opencode-zen" else OpenCodeGoModelProvider()

    with pytest.raises(ProviderExecutionError) as failure:
        adapter.turn_provider().stream_turn(_request(model, provider_id))  # type: ignore[attr-defined]

    assert failure.value.kind == "unsupported_feature"
    assert failure.value.retryable is False
    assert failure.value.fallback_allowed is True
