"""The official ``providers`` plugin: every provider policy artifact.

Owns the provider table, thinking/pricing/API-routing rules, model catalog
datasets, tokenizer routing, the native wire adapters, provider dispatch, and the
read-only ``providers``/``model`` information panels. The core selects a
registered provider name and never learns which vendor policy answered it.
"""

from collections.abc import Mapping
from contextlib import ExitStack

from voidcode.sdk.plugin import Plugin

from .dispatch import build_registered_providers
from .panels import ModelCommand, ModelView, ProviderFacts, ProvidersCommand, ProvidersView


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    providers = build_registered_providers(dict(config or {}))
    facts = ProviderFacts.from_config(config)

    def close() -> None:
        with ExitStack() as stack:
            for provider in providers:
                stack.callback(provider.close)

    return Plugin(
        commands=(
            ProvidersCommand(facts),
            ModelCommand(facts),
        ),
        providers=providers,
        views=(ProvidersView(facts), ModelView(facts)),
        close=close,
    )
