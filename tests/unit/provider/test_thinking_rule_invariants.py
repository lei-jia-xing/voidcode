"""Every thinking row's `mode` must name the knob its wire's adapter actually emits.

This is the drift guard for the class of bug W3 shipped: an `effort` row on an
Anthropic-wire model made the adapter write `thinking.budget_tokens` under an
effort label. The knob each adapter can emit is fixed by the wire, so the check
is per catalog model: resolve the wire the runtime dispatches (the W2 rule), then
require the row's mode to be one that wire's adapter can express -- and, for the
budget modes, that the row actually carries the table the adapter reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from voidcode.provider.anthropic_native import AnthropicMessagesProvider
from voidcode.provider.google_native import GoogleGenAIProvider
from voidcode.provider.model_catalog import _load_static_catalog
from voidcode.provider.model_routing import RoutedTurnProvider
from voidcode.provider.openai_native import OpenAIChatCompletionsProvider
from voidcode.provider.protocol import ProviderTurnRequest, TurnProvider
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.thinking_rules import thinking_rule_for

#: Which thinking modes each wire's adapter can express.
_MODES_BY_WIRE: Mapping[str, frozenset[str]] = {
    # reasoning_effort, or the vendor's binary body switch.
    "openai-chat-completions": frozenset({"effort", "binary"}),
    # thinking.budget_tokens only.
    "anthropic-messages": frozenset({"budget"}),
    # thinking_level or thinking_budget.
    "google-generative-ai": frozenset({"google-level", "budget"}),
}


#: The wire each concrete adapter speaks.
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


def _dispatch_wire(turn: TurnProvider, provider_id: str, model_id: str) -> str | None:
    """The wire the adapter would actually send this model over."""
    if isinstance(turn, RoutedTurnProvider):
        request = ProviderTurnRequest(assembled_context=_Context(), provider_name=provider_id, model_name=model_id)  # type: ignore[arg-type]
        _, route = turn.route_for(request)
        return None if route is None else route.wire
    return _WIRE_BY_TURN_PROVIDER[type(turn)]


def test_every_models_mode_is_expressible_on_the_wire_it_dispatches() -> None:
    registry = ModelProviderRegistry.with_defaults()
    mismatches: list[str] = []
    # An emptied or truncated artifact would leave every loop below with nothing to
    # walk: assert the precondition so a wipe fails instead of passing vacuously.
    catalog = _load_static_catalog()
    assert catalog, "the shipped catalog is empty"
    assert all(models for models in catalog.values()), "a provider in the shipped catalog has no models"
    total = sum(len(models) for models in catalog.values())
    assert total > len(catalog), f"the shipped catalog has {total} models across {len(catalog)} providers"

    for provider_id in sorted(registry.providers):
        turn = registry.resolve(provider_id).turn_provider()
        for model_id in sorted(_catalog_models(provider_id)):
            wire = _dispatch_wire(turn, provider_id, model_id)
            if wire is None:
                continue  # a wire voidcode does not implement never builds a request
            rule = thinking_rule_for(provider_id, model_id)
            allowed = _MODES_BY_WIRE[wire]
            if rule.mode not in allowed:
                mismatches.append(f"{provider_id}/{model_id}: mode={rule.mode!r} on wire {wire!r}")
            if rule.mode == "budget" and wire == "anthropic-messages" and not rule.budgets:
                mismatches.append(f"{provider_id}/{model_id}: budget mode without a budget table on the anthropic wire")
            if rule.mode == "binary" and rule.disable_mode != "zai-thinking-disabled":
                mismatches.append(f"{provider_id}/{model_id}: binary mode without the zai format spelling")

    assert not mismatches, "thinking mode does not match the emitted knob:\n" + "\n".join(mismatches)


def _catalog_models(provider_id: str) -> tuple[str, ...]:
    return tuple(_load_static_catalog().get(provider_id, {}))
