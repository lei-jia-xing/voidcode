"""Registration returned by a trusted plugin's ``create_plugin(config)`` factory."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from .commands import Command, CommandDefinition
from .hooks import ContextContribution, PluginFact, RunLifecycle, ToolIntent, ToolOutcome
from .invocation import ToolContext
from .json_values import own_json_object
from .manifest import PluginCommandDeclaration, PluginManifest, PluginToolDeclaration
from .provider import TurnProvider
from .services import PluginServices
from .tools import Tool, ToolDefinition, ToolReplayPolicy
from .views import PluginView


@dataclass(frozen=True, slots=True)
class PluginQueryCaller:
    """The lease facts the host injects into a query, so a provider never trusts caller claims."""

    session_id: str
    workspace: Path


@dataclass(frozen=True, slots=True)
class PluginQuery:
    """One named, read-only question another plugin in this host may ask.

    A query is a host-local convenience between trusted plugins, not a capability: it runs inside
    the asking plugin's invocation, returns one bounded JSON object (or ``None`` for "nothing to
    answer"), and gets no writable ``PluginServices`` authority. Read-only access to already
    authorized data is allowed (a query may read the files its plugin could already read); state
    mutations, filesystem writes, process starts, and any other external effects are forbidden.
    This is a code-review contract, not an OS sandbox — and only some providers are purely
    in-memory. It is not manifest-declared and carries no kernel authority.
    """

    name: str
    handler: Callable[[Mapping[str, object], PluginQueryCaller], Mapping[str, object] | None]


@dataclass(frozen=True, slots=True)
class Plugin:
    tools: tuple[Tool, ...] = ()
    commands: tuple[Command, ...] = ()
    providers: tuple[TurnProvider, ...] = ()
    views: tuple[PluginView, ...] = ()
    queries: tuple[PluginQuery, ...] = ()
    context: Callable[[ToolContext, PluginServices], ContextContribution | None] | None = None
    observer: Callable[[PluginFact, ToolContext, PluginServices], None] | None = None
    #: Lifecycle notifications: run once at the start and once at the end of each run.
    session_start: Callable[[RunLifecycle, ToolContext, PluginServices], str | None] | None = None
    session_end: Callable[[RunLifecycle, ToolContext, PluginServices], str | None] | None = None
    #: Pre-tool gate: runs before the invocation is admitted. Returning a reason DENIES the
    #: call, and so does a gate failure (fail-closed): a gate can only refuse, never widen.
    pre_tool: Callable[[ToolIntent, ToolContext, PluginServices], str | None] | None = None
    #: Post-tool hook: runs inside the governed invocation after a successful tool result,
    #: before that result becomes durable. Returns an optional note appended to the result.
    post_tool: Callable[[ToolOutcome, ToolContext, PluginServices], str | None] | None = None
    #: Pure old-version -> new-version state transform, run before a capability switch.
    migrate_state: Callable[[Mapping[str, object], int, int], Mapping[str, object]] | None = None
    #: Pure deactivation transform, run in the current host when this owner is disabled.
    prepare_deactivate: Callable[[Mapping[str, object], str, str, int | None], Mapping[str, object]] | None = None
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
        path_argument_keys=tuple(declaration.path_argument_keys),
        supports_parallel=declaration.supports_parallel,
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
        direct_user=declaration.direct_user,
        client_syntax=declaration.client_syntax,
    )
