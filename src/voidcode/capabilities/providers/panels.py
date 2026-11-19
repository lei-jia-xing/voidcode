"""Read-only provider, model and catalog panels."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.provider import ProviderExecutionError
from voidcode.sdk.services import HistoryServices, PluginServices
from voidcode.sdk.tools import ToolEffect
from voidcode.sdk.transcript import TranscriptDispatch
from voidcode.sdk.views import PluginViewDescriptor

from .dispatch import RegisteredProvider
from .native.model_catalog import static_catalog_model_ids
from .native.naming import canonical_provider_id, provider_label
from .native.provider_table import PROVIDER_TABLE, PROVIDER_TABLE_BY_ID, ProviderTableRow


@dataclass(frozen=True, slots=True)
class ConfiguredProvider:
    """One configured provider, joined with its shipped table row."""

    name: str
    label: str
    wire: str | None
    models: int

    def as_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "wire": self.wire,
            "models": self.models,
        }


@dataclass(frozen=True, slots=True)
class ProviderFacts:
    """Configured provider identities and catalog facts; no credential values retained."""

    configured: tuple[ConfiguredProvider, ...]
    unconfigured: tuple[ProviderTableRow, ...]

    @classmethod
    def from_config(cls, config: Mapping[str, object] | None) -> ProviderFacts:
        """Read the core-injected ``providers`` section: configured names plus their table rows."""
        raw = (config or {}).get("providers")
        entries = raw if isinstance(raw, Mapping) else {}
        configured = tuple(sorted((_configured_provider(str(name)) for name in entries), key=lambda item: item.name))
        names = {item.name for item in configured}
        return cls(configured=configured, unconfigured=tuple(row for row in PROVIDER_TABLE if row.id not in names))


def _configured_provider(name: str) -> ConfiguredProvider:
    canonical = canonical_provider_id(name)
    row = PROVIDER_TABLE_BY_ID.get(canonical)
    return ConfiguredProvider(
        name=canonical,
        label=provider_label(canonical),
        wire=None if row is None else row.wire,
        models=len(static_catalog_model_ids(canonical)),
    )


@dataclass(frozen=True, slots=True)
class LastDispatch:
    """The newest authenticated dispatch this session committed."""

    sequence: int
    provider: str
    model: str

    def as_payload(self) -> dict[str, object]:
        return {"sequence": self.sequence, "provider": self.provider, "model": self.model}


#: The stored event behind TranscriptDispatch (core EventType.PROVIDER_INTENT, unimportable from capabilities).
_DISPATCH_EVENT_TYPE = "core.provider_intent"


def last_dispatch(services: HistoryServices) -> LastDispatch | None:
    """Newest committed dispatch, including one whose response has not completed."""
    entry = services.latest_event(_DISPATCH_EVENT_TYPE)
    if entry is None or not isinstance(entry.content, TranscriptDispatch):
        return None
    return LastDispatch(entry.sequence, entry.content.provider_name, entry.content.model_name)


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def providers_panel(facts: ProviderFacts) -> tuple[str, dict[str, object]]:
    """Markdown plus data for the workspace provider configuration."""
    lines = ["## Configured providers", ""]
    if facts.configured:
        lines += [
            f"- **{item.label}** (`{item.name}`) — {item.wire or 'no shipped wire'}, {_counted(item.models, 'shipped model')}"
            for item in facts.configured
        ]
    else:
        lines.append("No provider is configured in this workspace.")
    lines += ["", "## Declared by the shipped table, not configured", ""]
    if facts.unconfigured:
        lines.append(", ".join(f"`{row.id}`" for row in facts.unconfigured))
    else:
        lines.append("Every provider the shipped table declares is configured in this workspace.")
    data: dict[str, object] = {
        "configured": [item.as_payload() for item in facts.configured],
        "unconfigured": [{"name": row.id, "label": row.label} for row in facts.unconfigured],
    }
    return "\n".join(lines), data


def model_panel(
    services: HistoryServices,
    facts: ProviderFacts,
    provider: str | None,
    *,
    available_models: tuple[str, ...] | None = None,
) -> tuple[str, dict[str, object]]:
    """Markdown and data for a provider catalog or the session's dispatch."""
    if provider is not None:
        name = canonical_provider_id(provider)
        models = static_catalog_model_ids(name) if available_models is None else available_models
        source = "shipped catalog" if available_models is None else "available"
        lines = [f"## {provider_label(name)} (`{name}`)", ""]
        if models:
            lines += [f"{_counted(len(models), f'{source} model')}:", ""]
            lines += [f"- `{model}`" for model in models]
        else:
            lines.append(f"No {source} models for `{name}`.")
        return "\n".join(lines), {"provider": name, "models": list(models)}

    dispatch = last_dispatch(services)
    lines = ["## This session", ""]
    if dispatch is None:
        lines.append("No provider dispatch recorded in this session yet.")
    else:
        lines.append(f"Last dispatch: `{dispatch.provider}/{dispatch.model}` (event #{dispatch.sequence}).")
    lines += ["", "## Configured providers", ""]
    if facts.configured:
        lines += [f"- **{item.label}** (`{item.name}`)" for item in facts.configured]
    else:
        lines.append("No provider is configured in this workspace.")
    data = {
        "last_dispatch": None if dispatch is None else dispatch.as_payload(),
        "configured": [{"name": item.name, "label": item.label} for item in facts.configured],
    }
    return "\n".join(lines), data


class ProvidersCommand:
    """Read-only ledger of the workspace provider configuration."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="providers",
        description="List configured providers with their shipped table row, catalog size.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
    )

    def __init__(self, facts: ProviderFacts) -> None:
        self._facts = facts

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = (invocation, services)
        output, data = providers_panel(self._facts)
        return CommandResult(status="ok", output=output, data=data)


class ModelCommand:
    """Read-only model and provider-catalog lookup."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="model",
        description="Show the session model or list a provider's catalog models.",
        argument_schema={"type": "object", "properties": {"provider": {"type": "string"}}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ, ToolEffect.NETWORK}),
    )

    def __init__(self, facts: ProviderFacts, providers: Sequence[RegisteredProvider]) -> None:
        self._facts = facts
        self._providers = {provider.name: provider for provider in providers}

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw = invocation.arguments.get("provider")
        if raw is not None and (not isinstance(raw, str) or not raw.strip()):
            return CommandResult(status="error", error="provider must be a non-empty provider id")
        available_models = None
        if isinstance(raw, str):
            provider = self._providers.get(canonical_provider_id(raw))
            if provider is not None and not static_catalog_model_ids(provider.name):
                try:
                    available_models = provider.list_models()
                except ProviderExecutionError as exc:
                    return CommandResult(status="error", error=exc.message)
        output, data = model_panel(services, self._facts, raw if isinstance(raw, str) else None, available_models=available_models)
        return CommandResult(status="ok", output=output, data=data)


class ProvidersView:
    """Declarative panel for the ``providers`` command's ledger."""

    name: str = "providers"

    def __init__(self, facts: ProviderFacts) -> None:
        self._facts = facts

    def render(
        self,
        session_id: str,
        services: PluginServices,
    ) -> PluginViewDescriptor:
        _ = (session_id, services)
        output, _data = providers_panel(self._facts)
        return PluginViewDescriptor(owner="providers", name=self.name, title="Providers", view_type="markdown", data={"content": output})


class ModelView:
    """Declarative panel for the ``model`` command's session report."""

    name: str = "model"

    def __init__(self, facts: ProviderFacts) -> None:
        self._facts = facts

    def render(
        self,
        session_id: str,
        services: PluginServices,
    ) -> PluginViewDescriptor:
        _ = session_id
        output, _data = model_panel(services, self._facts, None)
        return PluginViewDescriptor(owner="providers", name=self.name, title="Model", view_type="markdown", data={"content": output})


__all__ = [
    "ConfiguredProvider",
    "LastDispatch",
    "ModelCommand",
    "ModelView",
    "ProviderFacts",
    "ProvidersCommand",
    "ProvidersView",
    "last_dispatch",
    "model_panel",
    "providers_panel",
]
