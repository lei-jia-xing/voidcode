"""The ``providers`` and ``model`` information panels.

Both panels report facts this plugin already owns: the provider entries the core injects
into :func:`create_plugin`, the shipped provider table, the shipped model catalog, and the
provider/model pairs the calling session actually dispatched with (its committed
``kernel.provider_intent`` events). Model *selection* is configuration plus hot reload by
design, so neither panel writes state, events, or invocations; a panel that cannot read a
dispatch payload says so instead of inventing a model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect
from voidcode.sdk.views import PluginViewDescriptor

from .native.model_catalog import static_catalog_model_ids
from .native.naming import canonical_provider_id, provider_label
from .native.provider_table import PROVIDER_TABLE, PROVIDER_TABLE_BY_ID, ProviderTableRow

#: The core fact one dispatch commits before it reaches the wire: its provider and model.
DISPATCH_EVENT = "kernel.provider_intent"

#: The largest page the core serves per event read (``services.read_events`` bound).
READ_PAGE = 200


@dataclass(frozen=True, slots=True)
class ConfiguredProvider:
    """One configured provider, joined with its shipped table row."""

    name: str
    label: str
    wire: str | None
    #: Presence only: the credential value never leaves the core injection.
    credential: bool
    models: int

    def as_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "wire": self.wire,
            "credential": self.credential,
            "models": self.models,
        }


@dataclass(frozen=True, slots=True)
class ProviderFacts:
    """Provider facts a panel reports; no configured value is retained here, only its presence."""

    configured: tuple[ConfiguredProvider, ...]
    unconfigured: tuple[ProviderTableRow, ...]

    @classmethod
    def from_config(cls, config: Mapping[str, object] | None) -> ProviderFacts:
        """Read the core-injected ``providers`` section: configured names plus their table rows."""
        raw = (config or {}).get("providers")
        entries = raw if isinstance(raw, Mapping) else {}
        configured = tuple(sorted((_configured_provider(str(name), entry) for name, entry in entries.items()), key=lambda item: item.name))
        names = {item.name for item in configured}
        return cls(configured=configured, unconfigured=tuple(row for row in PROVIDER_TABLE if row.id not in names))


def _configured_provider(name: str, entry: object) -> ConfiguredProvider:
    canonical = canonical_provider_id(name)
    fields = entry if isinstance(entry, Mapping) else {}
    row = PROVIDER_TABLE_BY_ID.get(canonical)
    return ConfiguredProvider(
        name=canonical,
        label=provider_label(canonical),
        wire=None if row is None else row.wire,
        credential=bool(fields.get("api_key")),
        models=len(static_catalog_model_ids(canonical)),
    )


@dataclass(frozen=True, slots=True)
class LastDispatch:
    """The newest dispatch this session committed; provider/model are None when unreadable."""

    sequence: int
    provider: str | None
    model: str | None

    def as_payload(self) -> dict[str, object]:
        return {"sequence": self.sequence, "provider": self.provider, "model": self.model}


def last_dispatch(services: PluginServices) -> LastDispatch | None:
    """The newest ``kernel.provider_intent`` of this session, ``None`` when it committed none.

    Event reads page forward, so the scan stays payload-free — event types and sequences are
    enough to find the newest intent — and only that one event is re-read with its payload.
    """
    newest = 0
    after = 0
    while True:
        page = services.read_events(after_sequence=after, limit=READ_PAGE, include_payload=False)
        if not page:
            break
        newest = max((event.sequence for event in page if event.event_type == DISPATCH_EVENT), default=newest)
        after = page[-1].sequence
        if len(page) < READ_PAGE:
            break
    if newest == 0:
        return None
    for event in services.read_events(after_sequence=newest - 1, limit=1, include_payload=True):
        if event.sequence != newest or event.payload_truncated or not isinstance(event.payload, Mapping):
            break
        provider = event.payload.get("provider")
        model = event.payload.get("model")
        if isinstance(provider, str) and isinstance(model, str):
            return LastDispatch(newest, provider, model)
    return LastDispatch(newest, None, None)


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def providers_panel(facts: ProviderFacts) -> tuple[str, dict[str, object]]:
    """Markdown plus data for the workspace provider configuration."""
    lines = ["## Configured providers", ""]
    if facts.configured:
        lines += [
            f"- **{item.label}** (`{item.name}`) — {item.wire or 'no shipped wire'}, {_counted(item.models, 'shipped model')}, "
            f"{'credential configured' if item.credential else 'no credential'}"
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


def model_panel(services: PluginServices, facts: ProviderFacts, provider: str | None) -> tuple[str, dict[str, object]]:
    """Markdown plus data for one provider's shipped catalog, or for this session's dispatch."""
    if provider is not None:
        name = canonical_provider_id(provider)
        models = static_catalog_model_ids(name)
        lines = [f"## {provider_label(name)} (`{name}`)", ""]
        if models:
            lines += [f"{_counted(len(models), 'shipped catalog model')}:", ""]
            lines += [f"- `{model}`" for model in models]
        else:
            lines.append(f"No shipped catalog models for `{name}`.")
        return "\n".join(lines), {"provider": name, "models": list(models)}

    dispatch = last_dispatch(services)
    lines = ["## This session", ""]
    if dispatch is None:
        lines.append("No provider dispatch recorded in this session yet.")
    elif dispatch.provider is None or dispatch.model is None:
        lines.append(f"Event #{dispatch.sequence} recorded a dispatch whose payload was not readable.")
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
        description="List configured providers with their shipped table row, catalog size and credential presence.",
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
    """Read-only report of the dispatching provider/model and a provider's shipped models."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="model",
        description="Show the provider/model this session last dispatched with, or one provider's shipped catalog models.",
        argument_schema={"type": "object", "properties": {"provider": {"type": "string"}}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
    )

    def __init__(self, facts: ProviderFacts) -> None:
        self._facts = facts

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw = invocation.arguments.get("provider")
        if raw is not None and (not isinstance(raw, str) or not raw.strip()):
            return CommandResult(status="error", error="provider must be a non-empty provider id")
        output, data = model_panel(services, self._facts, raw if isinstance(raw, str) else None)
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
        *,
        client_capabilities: frozenset[str] = frozenset(),
    ) -> PluginViewDescriptor:
        _ = (session_id, services, client_capabilities)
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
        *,
        client_capabilities: frozenset[str] = frozenset(),
    ) -> PluginViewDescriptor:
        _ = (session_id, client_capabilities)
        output, _data = model_panel(services, self._facts, None)
        return PluginViewDescriptor(owner="providers", name=self.name, title="Model", view_type="markdown", data={"content": output})


__all__ = [
    "DISPATCH_EVENT",
    "READ_PAGE",
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
