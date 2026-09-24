from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace

from .api_routes import api_route_for
from .config import RoutedWire
from .model_catalog import ProviderModelMetadata, static_catalog_metadata
from .protocol import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
    StreamableTurnProvider,
)
from .provider_table import WireSource


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """The wire one model speaks, and the base URL it is spoken at."""

    wire: RoutedWire
    # ``None`` => the sub-provider's own default for that wire.
    base_url: str | None = None


#: Upstream wire name -> the route VoidCode serves it at. A name absent here is a
#: wire VoidCode does not implement: the request fails typed instead of being sent
#: down a wire that does not serve the model.
IMPLEMENTED_API_ROUTES: Mapping[str, ModelRoute] = {
    "openai-completions": ModelRoute(wire="openai-chat-completions"),
    "anthropic-messages": ModelRoute(wire="anthropic-messages"),
    "google-generative-ai": ModelRoute(wire="google-generative-ai"),
}


@dataclass(frozen=True, slots=True)
class WireRouting:
    """Which wire each model of one provider speaks, resolved from data.

    Routing is a property of the model, not of the provider name: mixed-wire
    gateways (OpenCode Zen/Go) speak several wires behind one host. The wire comes
    from the model's catalog row (``api``, emitted by the generator and consumed
    verbatim), else an ``api_routes`` pin, else the provider's own wire --
    ``default_api``. ``api_to_route`` is the gateway's own wire vocabulary: an
    upstream wire it does not serve is absent, so such a model fails typed rather
    than being guessed at.

    ``wire_source`` comes from the provider table. ``provider`` means the catalog
    row's ``api`` is upstream truth this build does not serve yet, so every model
    dispatches ``default_api``; ``model`` means the row decides.
    """

    provider: str
    api_to_route: Mapping[str, ModelRoute]
    default_api: str
    wire_source: WireSource = "model"

    def resolve(self, model: str, api: str | None) -> tuple[str, ModelRoute | None]:
        """The routed model id and its route (``None`` == unimplemented wire)."""
        if self.wire_source == "provider":
            return model, self.api_to_route.get(self.default_api)
        if api:
            return model, self.api_to_route.get(api)
        pinned = api_route_for(self.provider, model)
        if pinned is not None:
            return pinned.request_model_id or model, self.api_to_route.get(pinned.api)
        return model, self.api_to_route.get(self.default_api)


def _routed_model(model_map: Mapping[str, str], model_name: str) -> str:
    """Resolve one provider ``model_map`` alias exactly as the wire adapters do."""
    mapped = model_map.get(model_name)
    return mapped if mapped else model_name


def _catalog_api(provider: str, model: str, metadata: ProviderModelMetadata | None) -> str | None:
    """The model's wire: the metadata the request carries, else the shipped row.

    The shipped catalog is the runtime's own copy of the row the generator
    emitted, so dispatch does not depend on how (or whether) discovery hydrated
    the request's metadata.
    """
    if metadata is not None and metadata.api:
        return metadata.api
    shipped = static_catalog_metadata(provider, model)
    return None if shipped is None else shipped.api


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
    build: Callable[[str, ModelRoute], StreamableTurnProvider]
    model_map: Mapping[str, str] = field(default_factory=dict)
    _providers: dict[str, StreamableTurnProvider] = field(default_factory=dict, compare=False, repr=False)

    def route_for(self, request: ProviderTurnRequest) -> tuple[str, ModelRoute | None]:
        """Return the routed model name and its route (``None`` == unimplemented wire)."""
        model = _routed_model(self.model_map, request.model_name or "")
        return self.routing.resolve(model, _catalog_api(self.routing.provider, model, request.model_metadata))

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
        return provider.stream_turn(routed)
