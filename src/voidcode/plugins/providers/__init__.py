"""The official ``providers`` plugin: every provider policy artifact.

Owns the provider table, thinking/pricing/API-routing rules, model catalog
datasets, tokenizer routing, the native wire adapters, provider dispatch, and the
read-only ``providers``/``model`` information panels. The kernel selects a
registered provider name and never learns which vendor policy answered it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, command_definition

from .dispatch import build_registered_providers
from .panels import ModelCommand, ModelView, ProviderFacts, ProvidersCommand, ProvidersView


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    providers = build_registered_providers(dict(config or {}))
    # The admitted manifest is the one declaration of both panels: the host validates a
    # registration against it, so the two can never drift.
    manifest = parse_plugin_manifest(json.loads((Path(__file__).parent / "voidcode-plugin.json").read_text(encoding="utf-8")))
    definitions = {declaration.name: command_definition(declaration) for declaration in manifest.commands}
    facts = ProviderFacts.from_config(config)

    def close() -> None:
        for provider in providers:
            provider.close()

    return Plugin(
        tools=(),
        commands=(
            ProvidersCommand(definitions["providers"], facts),
            ModelCommand(definitions["model"], facts),
        ),
        providers=providers,
        views=(ProvidersView(facts), ModelView(facts)),
        close=close,
    )
