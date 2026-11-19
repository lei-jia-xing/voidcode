"""Shared provider adapters and the optional provider information plugin.

Catalog/protocol data, SDK wire adapters and registered providers are shared by
core defaults and plugin admission. The factory adds read-only information views.
"""

from collections.abc import Mapping
from contextlib import ExitStack

from voidcode.sdk.plugin import Plugin


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    # Deferred: importing dispatch pulls every native wire + vendor SDK (~2.6s);
    # config-only callers (resolve_providers, catalog reads) must not pay it.
    from .dispatch import build_registered_providers
    from .panels import ModelCommand, ModelView, ProviderFacts, ProvidersCommand, ProvidersView

    providers = build_registered_providers(dict(config or {}))
    facts = ProviderFacts.from_config(config)

    def close() -> None:
        with ExitStack() as stack:
            for provider in providers:
                stack.callback(provider.close)

    return Plugin(
        commands=(
            ProvidersCommand(facts),
            ModelCommand(facts, providers),
        ),
        providers=providers,
        views=(ProvidersView(facts), ModelView(facts)),
        close=close,
    )
