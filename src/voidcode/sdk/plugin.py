"""Registration returned by a trusted plugin's ``create_plugin(config)`` factory."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from .commands import Command, CommandDefinition
from .hooks import ContextContribution, PluginFact
from .invocation import ToolContext
from .json_values import own_json_object
from .manifest import PluginCommandDeclaration, PluginManifest, PluginToolDeclaration
from .provider import TurnProvider
from .services import PluginServices
from .tools import Tool, ToolDefinition, ToolReplayPolicy
from .views import PluginView


@dataclass(frozen=True, slots=True)
class Plugin:
    tools: tuple[Tool, ...] = ()
    commands: tuple[Command, ...] = ()
    providers: tuple[TurnProvider, ...] = ()
    views: tuple[PluginView, ...] = ()
    context: Callable[[ToolContext, PluginServices], ContextContribution | None] | None = None
    observer: Callable[[PluginFact, ToolContext, PluginServices], None] | None = None
    close: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class HostPlugin:
    """Kernel-admitted artifact and explicitly granted invocation capabilities."""

    root: Path
    manifest: PluginManifest
    config: Mapping[str, object] = field(default_factory=dict, repr=False)
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", own_json_object(self.config))


def tool_definition(declaration: PluginToolDeclaration) -> ToolDefinition:
    """Use the admitted declaration for both the catalog and the actual handler."""
    return ToolDefinition(
        name=declaration.name,
        description=declaration.description,
        input_schema=declaration.input_schema,
        effects=frozenset(declaration.effects),
        replay_policy=cast(ToolReplayPolicy | None, declaration.replay_policy),
    )


def command_definition(declaration: PluginCommandDeclaration) -> CommandDefinition:
    """Use the admitted declaration for both the catalog and the actual handler."""
    return CommandDefinition(
        name=declaration.name,
        description=declaration.description,
        argument_schema=declaration.argument_schema,
        effects=frozenset(declaration.effects),
        capabilities=frozenset(declaration.capabilities),
        busy_gate=declaration.busy_gate,
        client_capabilities=frozenset(declaration.client_capabilities),
    )
