from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Literal, cast

from .protocol import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
    StreamableTurnProvider,
    TurnProvider,
)

# The wires VoidCode implements. A gateway that serves one of its models over
# anything else says so in its routing table (`overrides[model] = None`) instead
# of downgrading the model to a wire that would hit a different endpoint.
type RoutedWire = Literal["openai-chat-completions", "anthropic-messages", "google-generative-ai"]


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """The wire one model speaks, and the base URL it is spoken at."""

    wire: RoutedWire
    # ``None`` => the sub-provider's own default for that wire.
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class WireRouting:
    """Per-model wire routing for one gateway.

    Routing is a property of the model, not of the provider name: mixed-API
    gateways (OpenCode Zen/Go) speak several wires behind one host. The table is
    sparse -- a model with no entry uses ``default``, which keeps aliases and
    user-mounted gateways on the documented default -- while a ``None`` entry
    names a model whose upstream wire VoidCode does not implement.
    """

    default: ModelRoute
    overrides: Mapping[str, ModelRoute | None]


def _routed_model(model_map: Mapping[str, str], model_name: str) -> str:
    """Resolve one provider ``model_map`` alias exactly as the wire adapters do."""
    mapped = model_map.get(model_name)
    return mapped if mapped else model_name


@dataclass(frozen=True, slots=True)
class RoutedTurnProvider:
    """``TurnProvider`` dispatching each request to the wire its model speaks.

    ``build`` is the owning adapter's wire factory (it knows the endpoint, the
    credential and the tool-feedback rules of that gateway); ``RoutedTurnProvider``
    knows only the table. Sub-providers are built lazily per routed model and
    cached, so every wire keeps one SDK client and connection pool for the
    process. Only the first-use race can build a losing sub-provider, which is
    dropped exactly like the adapters' owned transports.

    ``model_map`` aliases resolve here, before dispatch, so a routed model always
    reaches its wire under the name the table routed: only the chat wire applies
    the map on its own.
    """

    name: str
    routing: WireRouting
    build: Callable[[str, ModelRoute], TurnProvider]
    model_map: Mapping[str, str] = field(default_factory=dict)
    _providers: dict[str, TurnProvider] = field(default_factory=dict, compare=False, repr=False)

    def route_for(self, request: ProviderTurnRequest) -> tuple[str, ModelRoute | None]:
        """Return the routed model name and its route (``None`` == unimplemented wire)."""
        model = _routed_model(self.model_map, request.model_name or "")
        return model, self.routing.overrides.get(model, self.routing.default)

    def _dispatch(self, request: ProviderTurnRequest) -> tuple[TurnProvider, ProviderTurnRequest]:
        model, route = self.route_for(request)
        if route is None:
            # Never guessed at: an unimplemented upstream wire would send the
            # request to an endpoint that does not serve this model.
            raise ProviderExecutionError(
                kind="unsupported_feature",
                provider_name=request.provider_name or self.name,
                model_name=model,
                message=(
                    f"provider {request.provider_name or self.name} serves model {model} over an upstream wire "
                    "VoidCode does not implement; use another model"
                ),
                retryable=False,
                fallback_allowed=True,
            )
        provider = self._providers.get(model)
        if provider is None:
            provider = self.build(model, route)
            self._providers[model] = provider
        if request.model_name and model != request.model_name:
            # The routed model is the identity the sub-provider was built for, and
            # only the chat wire applies ``model_map`` itself: every other wire
            # would otherwise send the alias upstream.
            request = replace(request, model_name=model)
        return provider, request

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        provider, routed = self._dispatch(request)
        return provider.propose_turn(routed)

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        provider, routed = self._dispatch(request)
        # Wire factories only produce streamable sub-providers; the cast mirrors
        # ``graph/provider_graph.py``'s handling of the same seam.
        streamable = cast(StreamableTurnProvider, provider)
        return streamable.stream_turn(routed)
