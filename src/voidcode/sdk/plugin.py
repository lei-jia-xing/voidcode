"""Registration returned by a trusted plugin's ``create_plugin(config)`` factory.

A plugin is a Python package directory whose ``__init__.py`` exports
``create_plugin(config) -> Plugin``. This registration is the single truth about
what the plugin registers; there is no manifest to drift from.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, get_args

import jsonschema

from .commands import Command
from .hooks import ContextContribution, ObservedFactKind, PluginFact, RunLifecycle, ToolIntent, ToolOutcome
from .invocation import ToolContext
from .json_values import json_wire_object, own_json_object
from .provider import TurnProvider
from .services import PluginServices
from .tools import Tool
from .views import PluginView

#: The fact kinds a plugin observer may subscribe to; the single source is ``ObservedFactKind``.
_VALID_OBSERVED_FACTS: Final[frozenset[str]] = frozenset(get_args(ObservedFactKind))


def _validated_schema(schema: Mapping[str, object] | None, *, field_name: str) -> Mapping[str, object] | None:
    if schema is None:
        return None
    if not isinstance(schema, Mapping):
        raise ValueError(f"{field_name} must be an object")
    owned = own_json_object(schema)
    try:
        jsonschema.Draft202012Validator.check_schema(json_wire_object(owned))
    except jsonschema.exceptions.SchemaError as err:
        raise ValueError(f"{field_name} is not a valid JSON schema: {err.message}") from err
    return owned


def _validated_match(match: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    patterns = tuple(match)
    if not patterns or any(not isinstance(pattern, str) or not pattern for pattern in patterns):
        raise ValueError(f"{field_name} must list at least one non-empty tool name or '*'")
    return patterns


@dataclass(frozen=True, slots=True)
class StateSchema:
    """The owner-state contract a plugin's ``migrate_state``/``prepare_deactivate`` operators assume."""

    version: int = 1
    schema: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 1:
            raise ValueError("plugin state schema version must be an integer >= 1")
        object.__setattr__(self, "schema", _validated_schema(self.schema, field_name="plugin state schema"))


@dataclass(frozen=True, slots=True)
class PluginQueryCaller:
    """The lease facts the runtime injects into a query, so a provider never trusts caller claims."""

    session_id: str
    workspace: Path


@dataclass(frozen=True, slots=True)
class PluginQuery:
    """One named, read-only question another plugin in this runtime may ask.

    A query is a runtime-local convenience between trusted plugins, not a capability: it runs inside
    the asking plugin's invocation, returns one bounded JSON object (or ``None`` for "nothing to
    answer"), and gets no writable ``PluginServices`` authority. The scoped read-only convention
    is allowed (a query may read the files its plugin could already read); state
    mutations, filesystem writes, process starts, and any other external effects are forbidden.
    This is a code-review contract, not an OS sandbox — and only some providers are purely
    in-memory. It carries no kernel authority.
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
    #: Fact kinds this plugin's observer wants delivered.
    observed_facts: tuple[ObservedFactKind, ...] = ()
    #: Lifecycle notifications: run once at the start and once at the end of each run.
    session_start: Callable[[RunLifecycle, ToolContext, PluginServices], str | None] | None = None
    session_end: Callable[[RunLifecycle, ToolContext, PluginServices], str | None] | None = None
    #: Pre-tool gate: runs before the invocation is admitted. Returning a reason DENIES the
    #: call, and so does a gate failure (fail-closed): a gate can only refuse, never widen.
    pre_tool: Callable[[ToolIntent, ToolContext, PluginServices], str | None] | None = None
    #: Tool names this gate matches; ignored when ``pre_tool`` is absent.
    pre_tool_match: tuple[str, ...] = ("*",)
    #: Whether the gate may mutate the workspace while deciding.
    pre_tool_mutating: bool = False
    #: Post-tool hook: runs inside the governed invocation after a successful tool result,
    #: before that result becomes durable. Returns an optional note appended to the result.
    post_tool: Callable[[ToolOutcome, ToolContext, PluginServices], str | None] | None = None
    #: Tool names this hook matches; ignored when ``post_tool`` is absent.
    post_tool_match: tuple[str, ...] = ("*",)
    #: Whether the hook may mutate the workspace while observing.
    post_tool_mutating: bool = False
    #: Owner-state schema the migration/deactivation operators below assume.
    state: StateSchema = StateSchema()
    #: Pure old-version -> new-version state transform, run before a capability switch.
    migrate_state: Callable[[Mapping[str, object], int, int], Mapping[str, object]] | None = None
    #: Pure deactivation transform, run in the current runtime when this owner is disabled.
    prepare_deactivate: Callable[[Mapping[str, object], str, str, int | None], Mapping[str, object]] | None = None
    close: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        for name in ("tools", "commands", "providers", "views", "queries"):
            if not isinstance(getattr(self, name), tuple):
                object.__setattr__(self, name, tuple(getattr(self, name)))
        facts = tuple(self.observed_facts)
        for fact in facts:
            if fact not in _VALID_OBSERVED_FACTS:
                raise ValueError(f"invalid observed fact kind: {fact!r}")
        object.__setattr__(self, "observed_facts", facts)
        object.__setattr__(self, "pre_tool_match", _validated_match(self.pre_tool_match, field_name="pre_tool_match"))
        object.__setattr__(self, "post_tool_match", _validated_match(self.post_tool_match, field_name="post_tool_match"))
        for name in ("pre_tool_mutating", "post_tool_mutating"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"plugin {name} must be a boolean")
        if not isinstance(self.state, StateSchema):
            raise ValueError("plugin state must be a StateSchema")
        if self.observed_facts and self.observer is None:
            raise ValueError("plugin declares observed_facts but provides no observer callback")
        if self.observer is not None and not self.observed_facts:
            raise ValueError("plugin provides an observer callback but declares no observed_facts")
