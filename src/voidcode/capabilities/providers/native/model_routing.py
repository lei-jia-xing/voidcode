from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from threading import Lock

from voidcode.sdk.provider import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
    StreamableTurnProvider,
)

from .config import RoutedWire
from .model_catalog import static_catalog_metadata

#: Upstream wire name -> the route VoidCode serves it at. A name absent here is a
#: wire VoidCode does not implement: the request fails typed instead of being sent
#: down a wire that does not serve the model.
IMPLEMENTED_API_ROUTES: Mapping[str, RoutedWire] = {
    "openai-completions": "openai-chat-completions",
    "anthropic-messages": "anthropic-messages",
    "google-generative-ai": "google-generative-ai",
}


@dataclass(frozen=True, slots=True)
class WireRouting:
    """Which wire each model of one provider speaks, resolved from data.

    Routing is a property of the model, not of the provider name: mixed-wire
    gateways (OpenCode Zen/Go) speak several wires behind one host. The wire comes
    from the model's catalog row (``api``, emitted by the generator and consumed
    verbatim), else the provider's own wire --
    ``default_api``. ``api_to_route`` is the gateway's own wire vocabulary: an
    upstream wire it does not serve is absent, so such a model fails typed rather
    than being guessed at.

    """

    provider: str
    api_to_route: Mapping[str, RoutedWire]
    default_api: str

    def resolve(self, api: str | None) -> RoutedWire | None:
        """Resolve a declared upstream protocol, using the provider default when absent."""
        return self.api_to_route.get(api or self.default_api)


@dataclass(frozen=True, slots=True)
class RoutedTurnProvider:
    """``TurnProvider`` dispatching each request to the wire its model speaks.

    ``build`` is the owning adapter's wire factory (it knows the endpoint, the
    credential and the tool-feedback rules of that gateway); ``RoutedTurnProvider``
    knows only the table. Sub-providers are built lazily per routed model and
    cached per model. Cache construction is serialized to avoid losing an owned
    SDK client and connection pool.

    ``model_map`` aliases resolve here, before dispatch, so a routed model always
    reaches its wire under the name the table routed: only the chat wire applies
    the map on its own.
    """

    name: str
    routing: WireRouting
    build: Callable[[str, RoutedWire], StreamableTurnProvider]
    model_map: Mapping[str, str] = field(default_factory=dict)
    _providers: dict[str, StreamableTurnProvider] = field(default_factory=dict, compare=False, repr=False)
    _lock: Lock = field(default_factory=Lock, compare=False, repr=False)

    def route_for(self, request: ProviderTurnRequest) -> tuple[str, RoutedWire | None]:
        """Return the routed model name and its route (``None`` == unimplemented wire)."""
        requested = request.model_name or ""
        model = self.model_map.get(requested) or requested
        metadata = static_catalog_metadata(self.routing.provider, model)
        return model, self.routing.resolve(None if metadata is None else metadata.api)

    def _dispatch(self, request: ProviderTurnRequest) -> tuple[StreamableTurnProvider, ProviderTurnRequest]:
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
        with self._lock:
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

    def close(self) -> None:
        with self._lock:
            providers = tuple(self._providers.values())
            self._providers.clear()
        with ExitStack() as stack:
            for provider in providers:
                closer = getattr(provider, "close", None)
                if callable(closer):
                    stack.callback(closer)

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        provider, routed = self._dispatch(request)
        return provider.propose_turn(routed)

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        provider, routed = self._dispatch(request)
        return provider.stream_turn(routed)
