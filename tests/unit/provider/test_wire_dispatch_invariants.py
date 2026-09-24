"""Every provider's dispatch must agree with what its own data promises.

The catalog row's ``api`` is the wire the runtime uses -- except for a provider
whose table row says ``wire_source="provider"``, where the row records upstream
truth this build does not serve yet and dispatch stays on the provider's own
wire. This test walks the whole shipped catalog and turns any drift between the
catalog, the provider table and the adapters into a failure instead of a review
question: a model whose ``api`` is a wire VoidCode implements must reach that
wire's adapter, and one whose ``api`` is a wire it does not implement must fail
typed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from voidcode.provider import model_catalog
from voidcode.provider.anthropic_native import AnthropicMessagesProvider
from voidcode.provider.google_native import GoogleGenAIProvider
from voidcode.provider.model_routing import RoutedTurnProvider
from voidcode.provider.openai_native import OpenAIChatCompletionsProvider
from voidcode.provider.protocol import ProviderExecutionError, ProviderTurnRequest, TurnProvider
from voidcode.provider.provider_table import PROVIDER_TABLE_BY_ID
from voidcode.provider.registry import ModelProviderRegistry

#: Upstream wire name -> the wire VoidCode serves it at.
_VOIDCODE_WIRE_BY_API: Mapping[str, str] = {
    "openai-completions": "openai-chat-completions",
    "anthropic-messages": "anthropic-messages",
    "google-generative-ai": "google-generative-ai",
}

#: The wire each concrete adapter speaks. A new adapter type must be added here
#: instead of silently passing.
_WIRE_BY_TURN_PROVIDER: Mapping[type[TurnProvider], str] = {
    OpenAIChatCompletionsProvider: "openai-chat-completions",
    AnthropicMessagesProvider: "anthropic-messages",
    GoogleGenAIProvider: "google-generative-ai",
}


@dataclass(frozen=True, slots=True)
class _Context:
    prompt: str = "hello"
    tool_results: tuple[object, ...] = ()
    continuity_state: object | None = None
    segments: tuple[object, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


def _request(model: str, provider: str) -> ProviderTurnRequest:
    return ProviderTurnRequest(assembled_context=_Context(), provider_name=provider, model_name=model)


def _dispatch_wire(turn: TurnProvider, provider_id: str, model: str) -> str | None:
    """The wire a turn for ``model`` would actually be sent over (``None`` == typed failure)."""
    if isinstance(turn, RoutedTurnProvider):
        _, route = turn.route_for(_request(model, provider_id))
        return None if route is None else route.wire
    return _WIRE_BY_TURN_PROVIDER[type(turn)]


def test_dispatch_wire_matches_the_data_for_every_catalog_model() -> None:
    registry = ModelProviderRegistry.with_defaults()
    catalog = model_catalog._load_static_catalog()
    mismatches: list[str] = []
    unsupported: list[tuple[str, str, TurnProvider]] = []

    for provider_id, models in sorted(catalog.items()):
        row = PROVIDER_TABLE_BY_ID[provider_id]
        turn = registry.resolve(provider_id).turn_provider()
        for model, metadata in sorted(models.items()):
            wire = _dispatch_wire(turn, provider_id, model)
            if row.wire_source == "provider":
                expected = _VOIDCODE_WIRE_BY_API[row.wire]
            else:
                expected = _VOIDCODE_WIRE_BY_API.get(metadata.api or "")
            if wire != expected:
                mismatches.append(
                    f"{provider_id}/{model}: dispatch={wire!r} promised={expected!r} (api={metadata.api!r}, wire_source={row.wire_source})"
                )
            if expected is None:
                unsupported.append((provider_id, model, turn))

    assert not mismatches, "dispatch disagrees with the catalog/table:\n" + "\n".join(mismatches)
    # A wire we do not implement must fail typed, never degrade to another wire.
    assert unsupported
    for provider_id, model, turn in unsupported:
        with pytest.raises(ProviderExecutionError) as failure:
            turn.stream_turn(_request(model, provider_id))
        assert failure.value.kind == "unsupported_feature"
        assert failure.value.retryable is False


def test_copilot_records_upstream_truth_but_dispatches_its_own_wire() -> None:
    """The one provider whose row's ``api`` is upstream truth this build does not
    serve: its catalog still says anthropic-messages / openai-responses, and
    dispatch stays chat-completions."""
    row = PROVIDER_TABLE_BY_ID["github-copilot"]
    catalog = model_catalog._load_static_catalog()["github-copilot"]
    turn = ModelProviderRegistry.with_defaults().resolve("github-copilot").turn_provider()

    assert row.wire_source == "provider"
    assert {metadata.api for metadata in catalog.values()} >= {"anthropic-messages", "openai-responses"}
    for model in catalog:
        assert _dispatch_wire(turn, "github-copilot", model) == "openai-chat-completions"
