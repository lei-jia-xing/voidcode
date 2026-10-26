"""In-process plugin runtime: direct factory loading and synchronous dispatch.

One `PluginRuntime` is one capability generation: it imports each configured plugin
package (`__init__.py:create_plugin`), registers the returned `Plugin` objects, and
dispatches tool, command, provider, hook, view, observer, and migration calls by direct
synchronous invocation.

Module identity: each plugin root is imported under a unique namespace derived from its
id plus this runtime's identity, so two generations can coexist (a candidate is built
before the old runtime closes) and a reloaded generation never reuses stale modules.
`close()` runs every `Plugin.close()`, stops generation-owned service processes, and
unimports the runtime's namespaces.

Cancellation is cooperative only: a plugin that never returns blocks its dispatch until
the core restarts. Real child processes keep their own timeout/cancel/partial-output
semantics via the core-owned `ProcessRegistry`.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import json
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, cast, get_args
from uuid import uuid4

import jsonschema
from pydantic import TypeAdapter

from voidcode.sdk.commands import CommandDescriptor, CommandInvocation, CommandResult
from voidcode.sdk.hooks import (
    ApprovalRequestedFact,
    ChildSpawnedFact,
    ContextContribution,
    ContextProjectedFact,
    FactPage,
    InvocationFinishedFact,
    ObservedFactKind,
    PluginFact,
    RunFinishedFact,
    RunLifecycle,
    RunRequestedFact,
    StopCause,
    ToolIntent,
    ToolOutcome,
    UsageFact,
    WakeupFiredFact,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.plugin import Plugin, PluginQueryCaller, StateSchema
from voidcode.sdk.provider import ProviderExecutionError, ProviderTokenUsage, ProviderTurnRequest, ProviderTurnResult
from voidcode.sdk.services import (
    EventView,
    PluginBudget,
    QueryUnavailable,
    SessionUsageReport,
)
from voidcode.sdk.tools import (
    ConfirmedStop,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    is_read_tier,
)
from voidcode.sdk.transcript import ContextHistory, TranscriptEntry, TranscriptPage
from voidcode.sdk.views import PluginViewDescriptor

from .artifacts import ArtifactStore
from .events import EventType
from .facts import fact_for_event
from .processes import ProcessRegistry
from .services import RuntimeServices
from .storage.store import (
    EventDraft,
    SessionRecord,
    SqliteStore,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
)
from .transcript import context_records, transcript_entry

RESERVED_COMMAND_NAMES: frozenset[str] = frozenset({"run", "resume", "show", "cancel", "close", "init", "help", "status", "sessions", "catalog"})
RESERVED_COMMAND_PREFIXES: tuple[str, ...] = ("core.", "voidcode.")
#: Names the core itself registers, which a plugin can never shadow.
RESERVED_TOOL_NAMES: frozenset[str] = frozenset({"core_reload"})
#: One plugin query answer stays a small JSON object: it is a read convenience, never a transport.
MAX_QUERY_RESULT_BYTES: Final[int] = 256 * 1024
#: The invocation-cancel watcher polls the caller's abort signal at this cadence.
_POLL_SECONDS: Final[float] = 0.05


@dataclass(frozen=True, slots=True)
class PluginSource:
    """One configured plugin a generation loads: its root and factory config."""

    root: Path
    plugin_id: str
    config: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            object.__setattr__(self, "root", Path(self.root))
        if not isinstance(self.plugin_id, str) or not self.plugin_id:
            raise ValueError("plugin source requires a non-empty plugin id")
        object.__setattr__(self, "config", own_json_object(self.config))


@dataclass
class LeaseContext:
    token: str
    owner: str
    session_id: str
    workspace: Path
    run_id: str
    revision_id: str
    invocation_id: str
    readonly: bool
    #: Provenance of the invocation that created this lease ("model", "client", "user", "plugin").
    actor: str = "model"
    #: Hook leases cannot start nested work.
    hook: bool = False
    #: The lease may stage internal session work (state, events, budget, continuations):
    #: set when the session stance is writable, or when the tool declares SESSION.
    internal_work: bool = False
    invocation_depth: int = 0
    staged_state: dict[tuple[str, str], tuple[Mapping[str, object], int, int]] = field(default_factory=dict)
    staged_events: list[EventDraft] = field(default_factory=list)
    staged_continuations: list[StagedContinuationDraft] = field(default_factory=list)
    staged_cancellations: list[StagedCancellation] = field(default_factory=list)
    staged_budget: PluginBudget | None | Literal["clear"] = None
    revoked: bool = False
    op_counter: int = 0

    @property
    def internal_restricted(self) -> bool:
        """Whether this lease refuses internal session work."""
        return self.readonly and not self.internal_work


@dataclass(frozen=True, slots=True)
class ToolInvocationOutcome:
    result: ToolResult
    staged_state: tuple[StagedStateUpdate, ...] = ()
    staged_events: tuple[EventDraft, ...] = ()
    staged_continuations: tuple[StagedContinuationDraft, ...] = ()
    staged_cancellations: tuple[StagedCancellation, ...] = ()
    staged_budget: PluginBudget | None | Literal["clear"] = None


#: One invocation's staged side effects: state, events, continuations, cancellations, budget.
type _Staged = tuple[
    tuple[StagedStateUpdate, ...],
    tuple[EventDraft, ...],
    tuple[StagedContinuationDraft, ...],
    tuple[StagedCancellation, ...],
    PluginBudget | None | Literal["clear"],
]


@dataclass(frozen=True, slots=True)
class CallbackChanges:
    staged_state: tuple[StagedStateUpdate, ...] = ()
    staged_events: tuple[EventDraft, ...] = ()
    staged_continuations: tuple[StagedContinuationDraft, ...] = ()
    staged_cancellations: tuple[StagedCancellation, ...] = ()
    staged_budgets: tuple[tuple[str, PluginBudget | Literal["clear"]], ...] = ()
    errors: tuple[dict[str, str], ...] = ()


def _check_arity(fn: Any, expected_arity: int, name: str) -> None:
    try:
        sig = inspect.signature(fn)
        sig.bind(*([None] * expected_arity))
    except Exception as exc:
        raise ValueError(f"{name} must accept {expected_arity} positional arguments: {exc}") from exc


class PluginRuntime:
    """One loaded plugin generation, dispatched synchronously in this process."""

    def __init__(
        self,
        sources: tuple[PluginSource, ...],
        *,
        workspace: Path,
        revision_id: str,
        store: SqliteStore | None = None,
        usage_callback: Callable[[str, int, int | None], SessionUsageReport] | None = None,
        budget_callback: Callable[[str, str], PluginBudget | None] | None = None,
        spawn_child_callback: Callable[[str, str, str, int, tuple[str, ...] | None, bool, bool], str] | None = None,
        join_child_callback: Callable[[str, str, float], Mapping[str, object]] | None = None,
        send_child_callback: Callable[[str, str, str, str, bool], Mapping[str, object]] | None = None,
        cancel_child_callback: Callable[[str, str, str], bool] | None = None,
        model_query_callback: Callable[[str, str, str | None, int | None, bool], str] | None = None,
        progress_callback: Callable[[LeaseContext, Mapping[str, object]], None] | None = None,
        tool_definition_callback: Callable[[str, str, str], tuple[Mapping[str, object] | None, str]] | None = None,
        artifacts_dir: Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace directory does not exist: {self.workspace}")
        if not revision_id:
            raise ValueError("revision_id must be a non-empty string")
        self.revision_id = revision_id
        self._store = store
        self.usage_callback = usage_callback
        self.budget_callback = budget_callback
        self.spawn_child_callback = spawn_child_callback
        self.join_child_callback = join_child_callback
        self.send_child_callback = send_child_callback
        self.cancel_child_callback = cancel_child_callback
        self.model_query_callback = model_query_callback
        self.progress_callback = progress_callback
        self.tool_definition_callback = tool_definition_callback
        self._artifacts = None if artifacts_dir is None else ArtifactStore(artifacts_dir)
        # Unique per runtime so two generations never share imported plugin modules.
        self._namespace = f"voidcode_runtime_{uuid4().hex}"
        self._modules: list[str] = []

        seen: set[str] = set()
        for source in sources:
            if source.plugin_id in seen:
                raise ValueError(f"Duplicate plugin id: {source.plugin_id}")
            seen.add(source.plugin_id)
            if not source.root.is_dir():
                raise ValueError(f"Plugin source is not a directory: {source.root}")

        self._sources = tuple(sources)
        self._lease_lock = threading.Lock()
        self._active_leases: dict[str, LeaseContext] = {}
        self._claimed_services: dict[str, set[tuple[str, str]]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._closed = False
        #: Every child process this runtime started; reaped on close.
        self.processes = ProcessRegistry()
        #: Where a background process spools its output when the caller names no log path.
        self.background_spool = Path(tempfile.mkdtemp(prefix="voidcode-process-"))

        self._tools: dict[str, tuple[Any, str]] = {}
        self._providers: dict[str, tuple[Any, str]] = {}
        self._commands: dict[str, tuple[Any, str]] = {}
        self._views: dict[str, tuple[Any, str]] = {}
        self._context_callbacks: dict[str, Callable[..., Any]] = {}
        self._post_tool_hooks: list[tuple[Any, tuple[str, ...], bool, str]] = []
        self._pre_tool_hooks: list[tuple[Any, tuple[str, ...], bool, str]] = []
        self._lifecycle_hooks: dict[str, tuple[Any, str]] = {}
        self._observers: list[tuple[Any, tuple[str, ...], str]] = []
        self._state_deactivators: dict[str, Any] = {}
        self._queries: dict[str, tuple[Any, str]] = {}

        self._plugins: list[Plugin] = []
        self._plugin_ids: list[str] = []
        self._plugin_states: dict[str, StateSchema] = {}
        try:
            for source in sources:
                self._load_plugin(source)

            self._build_catalog()
        except Exception:
            self.close()
            raise

    # ---- loading ----

    def _load_plugin(self, source: PluginSource) -> None:
        plugin_id = source.plugin_id
        root = source.root.resolve()
        entry_module = root / "__init__.py"
        if not entry_module.is_file():
            raise ValueError(f"Plugin {plugin_id} root is not a package (no __init__.py): {root}")
        # Flat unique module name per runtime + id: no synthetic parent package to fabricate,
        # and "-" is encoded so distinct ids can never collide after normalization.
        safe_id = plugin_id.replace("_", "__").replace("-", "_")
        mod_name = f"{self._namespace}__{safe_id}"
        spec = importlib.util.spec_from_file_location(
            mod_name,
            str(entry_module),
            submodule_search_locations=[str(root)],
        )
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            raise ValueError(f"Failed to create module spec for plugin {plugin_id}: {entry_module}")
        loader = spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        self._modules.append(mod_name)
        try:
            # Compile from current source bytes: pyc validation is second-granularity,
            # so a same-second source rewrite would otherwise serve stale code.
            source_bytes = loader.get_data(str(entry_module))
            code = loader.source_to_code(source_bytes, str(entry_module))
            exec(code, mod.__dict__)
        finally:
            for name in list(sys.modules):
                if name.startswith(f"{mod_name}.") and name not in self._modules:
                    self._modules.append(name)

        factory = getattr(mod, "create_plugin", None)
        if factory is None or not callable(factory):
            raise ValueError(f"Plugin {plugin_id} does not export create_plugin")
        try:
            plugin_instance = factory(json_wire_object(source.config))
        except Exception as exc:
            raise ValueError(f"Plugin {plugin_id} factory failed: {exc}") from exc
        if not isinstance(plugin_instance, Plugin):
            raise ValueError(f"Plugin {plugin_id} factory returned {type(plugin_instance).__name__}, expected Plugin")
        # Recorded before registration validation: a duplicate/reserved name must still close.
        self._plugins.append(plugin_instance)
        self._plugin_ids.append(plugin_id)
        self._plugin_states[plugin_id] = plugin_instance.state

        seen_tools: set[str] = set()
        for tool in plugin_instance.tools:
            name = tool.definition.name
            if name in RESERVED_TOOL_NAMES:
                raise ValueError(f"Tool '{name}' in plugin {plugin_id} is a reserved tool name")
            if name in seen_tools or name in self._tools:
                raise ValueError(f"Plugin {plugin_id} registers duplicate tool name: {name}")
            seen_tools.add(name)
            self._tools[name] = (tool, plugin_id)

        seen_commands: set[str] = set()
        for command in plugin_instance.commands:
            name = command.definition.name
            if name in RESERVED_COMMAND_NAMES or any(name.startswith(prefix) for prefix in RESERVED_COMMAND_PREFIXES):
                raise ValueError(f"Command '{name}' in plugin {plugin_id} is a reserved command name or prefix")
            if name in seen_commands or name in self._commands:
                raise ValueError(f"Plugin {plugin_id} registers duplicate command name: {name}")
            seen_commands.add(name)
            self._commands[name] = (command, plugin_id)

        seen_providers: set[str] = set()
        for provider in plugin_instance.providers:
            name = provider.name
            if name in seen_providers or name in self._providers:
                raise ValueError(f"Plugin {plugin_id} registers duplicate provider name: {name}")
            seen_providers.add(name)
            self._providers[name] = (provider, plugin_id)

        seen_views: set[str] = set()
        for view in plugin_instance.views:
            name = view.name
            if name in seen_views or name in self._views:
                raise ValueError(f"Plugin {plugin_id} registers duplicate view name: {name}")
            seen_views.add(name)
            self._views[name] = (view, plugin_id)

        if plugin_instance.context is not None:
            _check_arity(plugin_instance.context, 2, f"Plugin {plugin_id} context callback")
            self._context_callbacks[plugin_id] = plugin_instance.context
        if plugin_instance.observer is not None:
            _check_arity(plugin_instance.observer, 3, f"Plugin {plugin_id} observer callback")
            self._observers.append((plugin_instance.observer, plugin_instance.observed_facts, plugin_id))
        if plugin_instance.post_tool is not None:
            _check_arity(plugin_instance.post_tool, 3, f"Plugin {plugin_id} post_tool callback")
            self._post_tool_hooks.append((plugin_instance.post_tool, plugin_instance.post_tool_match, plugin_instance.post_tool_mutating, plugin_id))
        for kind, attribute in (("session_start", "session_start"), ("session_end", "session_end")):
            callback = getattr(plugin_instance, attribute)
            if callback is not None:
                _check_arity(callback, 3, f"Plugin {plugin_id} {kind} callback")
                self._lifecycle_hooks[f"{plugin_id}\0{kind}"] = (callback, plugin_id)
        if plugin_instance.pre_tool is not None:
            _check_arity(plugin_instance.pre_tool, 3, f"Plugin {plugin_id} pre_tool callback")
            self._pre_tool_hooks.append((plugin_instance.pre_tool, plugin_instance.pre_tool_match, plugin_instance.pre_tool_mutating, plugin_id))
        if plugin_instance.prepare_deactivate is not None:
            _check_arity(plugin_instance.prepare_deactivate, 4, f"Plugin {plugin_id} prepare_deactivate callback")
            self._state_deactivators[plugin_id] = plugin_instance.prepare_deactivate
        for query in plugin_instance.queries:
            if query.name in self._queries:
                raise ValueError(f"Duplicate query name registered across plugins: {query.name}")
            _check_arity(query.handler, 2, f"Plugin {plugin_id} query {query.name} handler")
            self._queries[query.name] = (query.handler, plugin_id)

    def _build_catalog(self) -> None:
        self._tool_defs = tuple(tool.definition for tool, _ in self._tools.values())
        self._tool_map = {definition.name: definition for definition in self._tool_defs}
        self._provider_names = tuple(self._providers)
        self._tool_owners = {name: plugin_id for name, (_, plugin_id) in self._tools.items()}
        self._validators = {}
        for name, definition in self._tool_map.items():
            if definition.input_schema:
                schema_json = json_wire_object(definition.input_schema)
                try:
                    jsonschema.Draft202012Validator.check_schema(schema_json)
                except jsonschema.exceptions.SchemaError as err:
                    plugin_id = self._tool_owners.get(name, "unknown")
                    raise ValueError(f"Plugin {plugin_id} tool {name} has an invalid input schema: {err.message}") from err
                self._validators[name] = jsonschema.Draft202012Validator(schema_json)
        self._command_descs = tuple(
            CommandDescriptor(
                name=command.definition.name,
                owner=plugin_id,
                description=command.definition.description,
                argument_schema=command.definition.argument_schema,
                effects=tuple(sorted(command.definition.effects, key=lambda effect: effect.value)),
                busy_gate=command.definition.busy_gate,
                client_syntax=command.definition.client_syntax,
            )
            for command, plugin_id in self._commands.values()
        )
        self._command_map = {desc.name: desc for desc in self._command_descs}
        self._command_map_view: Mapping[str, CommandDescriptor] = MappingProxyType(self._command_map)
        self._command_validators = {}
        for desc in self._command_descs:
            if desc.argument_schema:
                schema_json = json_wire_object(desc.argument_schema)
                try:
                    jsonschema.Draft202012Validator.check_schema(schema_json)
                except jsonschema.exceptions.SchemaError as err:
                    raise ValueError(f"Plugin {desc.owner} command {desc.name} has an invalid argument schema: {err.message}") from err
                self._command_validators[desc.name] = jsonschema.Draft202012Validator(schema_json)
            else:
                self._command_validators[desc.name] = None
        self._view_names = tuple(self._views)
        self._view_owners = {name: plugin_id for name, (_, plugin_id) in self._views.items()}
        self._tool_map_view: Mapping[str, ToolDefinition] = MappingProxyType(self._tool_map)
        self._validators_view: Mapping[str, Any] = MappingProxyType(self._validators)
        self._command_validators_view: Mapping[str, Any] = MappingProxyType(self._command_validators)
        self._context_owners = tuple(self._context_callbacks)
        self._observer_owners: dict[str, tuple[str, ...]] = {
            fact_type: tuple(pid for _, obs_facts, pid in self._observers if fact_type in obs_facts) for fact_type in get_args(ObservedFactKind)
        }

    # ---- catalog accessors ----

    @property
    def tools(self) -> tuple[ToolDefinition, ...]:
        return self._tool_defs

    @property
    def providers(self) -> tuple[str, ...]:
        return self._provider_names

    @property
    def commands(self) -> tuple[CommandDescriptor, ...]:
        return self._command_descs

    @property
    def tool_definitions(self) -> Mapping[str, ToolDefinition]:
        return self._tool_map_view

    @property
    def tool_validators(self) -> Mapping[str, Any]:
        return self._validators_view

    @property
    def command_descriptors(self) -> Mapping[str, CommandDescriptor]:
        return self._command_map_view

    @property
    def command_validators(self) -> Mapping[str, Any]:
        return self._command_validators_view

    @property
    def views(self) -> tuple[str, ...]:
        return self._view_names

    def owner(self, tool_name: str) -> str:
        if tool_name not in self._tool_owners:
            raise KeyError(f"Tool {tool_name} not found in catalog")
        return self._tool_owners[tool_name]

    def view_owner(self, view_name: str) -> str:
        if view_name not in self._view_owners:
            raise KeyError(f"View {view_name} not found in catalog")
        return self._view_owners[view_name]

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        """The plugin ids this runtime actually loaded, in admission order."""
        return tuple(self._plugin_ids)

    @property
    def observer_owners(self) -> Mapping[str, tuple[str, ...]]:
        """Fact kind -> owners that registered it in this generation."""
        return dict(self._observer_owners)

    @property
    def lifecycle_hooks(self) -> tuple[tuple[str, str], ...]:
        """Declared lifecycle hooks: (owner, kind) pairs, kinds session_start/session_end."""
        return tuple((plugin_id, kind) for key, (_, plugin_id) in self._lifecycle_hooks.items() for kind in (key.split("\0", 1)[1],))

    @property
    def pre_tool_hooks(self) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
        """Declared pre-tool gates: (owner, tool match patterns, mutating)."""
        return tuple((plugin_id, match, mutating) for _, match, mutating, plugin_id in self._pre_tool_hooks)

    @property
    def post_tool_hooks(self) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
        """Declared post-tool hooks: (owner, tool match patterns, mutating)."""
        return tuple((plugin_id, match, mutating) for _, match, mutating, plugin_id in self._post_tool_hooks)

    def state_metadata(self) -> Mapping[str, StateSchema]:
        """The owner-state contract each loaded plugin declared at runtime."""
        return dict(self._plugin_states)

    def state_schema_version(self, owner: str) -> int:
        """The declared state schema version of one loaded owner (1 when it declares none)."""
        state = self._plugin_states.get(owner)
        return 1 if state is None else state.version

    # ---- core-facing helpers ----

    @property
    def store(self) -> SqliteStore | None:
        return self._store

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def lease_lock(self) -> threading.Lock:
        return self._lease_lock

    def require_store(self) -> SqliteStore:
        if self._store is None:
            raise RuntimeError("Store not attached to PluginRuntime")
        return self._store

    def require_artifacts(self) -> ArtifactStore:
        if self._artifacts is None:
            raise RuntimeError("artifact store is not attached to this runtime")
        return self._artifacts

    def _open_lease(
        self,
        *,
        owner: str,
        session_id: str,
        run_id: str,
        invocation_id: str,
        readonly: bool,
        actor: str,
        hook: bool = False,
        internal_work: bool = False,
        invocation_depth: int = 0,
    ) -> LeaseContext:
        lease = LeaseContext(
            token=uuid4().hex,
            owner=owner,
            session_id=session_id,
            workspace=self.workspace,
            run_id=run_id,
            revision_id=self.revision_id,
            invocation_id=invocation_id,
            readonly=readonly,
            actor=actor,
            hook=hook,
            internal_work=internal_work,
            invocation_depth=invocation_depth,
        )
        with self._lease_lock:
            self._active_leases[lease.token] = lease
        return lease

    def _revoke_lease(self, lease: LeaseContext) -> None:
        with self._lease_lock:
            lease.revoked = True
            self._active_leases.pop(lease.token, None)
            self._cancel_events.pop(lease.token, None)
            self._claimed_services.pop(lease.token, None)
        if self._artifacts is not None:
            self._artifacts.discard_owner(lease.token)

    def _staged_changes(self, lease: LeaseContext) -> _Staged:
        owner_schema_ver = self.state_schema_version(lease.owner)
        return (
            tuple(
                StagedStateUpdate(
                    owner=lease.owner,
                    scope=sc,
                    scope_key=sk,
                    payload=st,
                    expected_version=baseline,
                    schema_version=owner_schema_ver,
                    revision_id=self.revision_id,
                )
                for (sc, sk), (st, _local, baseline) in lease.staged_state.items()
            ),
            tuple(lease.staged_events),
            tuple(lease.staged_continuations),
            tuple(lease.staged_cancellations),
            lease.staged_budget,
        )

    def cancel_event_for(self, lease: LeaseContext) -> threading.Event:
        """The cancellation event bound to one lease's invocation."""
        with self._lease_lock:
            event = self._cancel_events.get(lease.token)
            if event is None:
                event = threading.Event()
                self._cancel_events[lease.token] = event
            return event

    def claim_service(self, lease: LeaseContext, process_id: str) -> None:
        """Bind one live *service* id to this invocation so cancellation stops its tree."""
        with self._lease_lock:
            self._claimed_services.setdefault(lease.token, set()).add((process_id, lease.session_id))

    def _watch_invocation_cancel(self, lease: LeaseContext, abort_signal: Any, cancel_event: threading.Event, stop: threading.Event) -> None:
        """Mirror the caller's abort signal into this invocation's cancel event, then stop its trees.

        The abort signal may flip at any point during the direct call: polling at 20 Hz keeps
        the cancel path alive without touching plugin code. Claimed service trees are stopped
        at the moment of cancellation (not after the tool returns), and `run_process` sees the
        event through `cancel_event_for`. The watcher exits when the invocation ends.
        """
        while not stop.wait(_POLL_SECONDS):
            if abort_signal is not None and getattr(abort_signal, "cancelled", False):
                cancel_event.set()
                self._cancel_claimed_services(lease)
                return

    def _cancel_claimed_services(self, lease: LeaseContext) -> None:
        with self._lease_lock:
            claims = tuple(self._claimed_services.pop(lease.token, ()))
        for process_id, session_id in claims:
            # A live invocation's active services die with the cancellation: this is the same
            # cancellation a prompt plugin performs itself, just owned by the runtime so a reused
            # service cannot outlive the invocation that was using it.
            try:
                self.processes.cancel(process_id, session_id=session_id)
            except ValueError:
                pass

    def store_window_projection(self, lease: LeaseContext, covered_from_sequence: int, covered_to_sequence: int, replacement: str) -> None:
        store = self.require_store()
        if not replacement.strip():
            raise ValueError("a window projection must carry non-empty replacement content")
        events = store.events(lease.session_id)
        if not events:
            raise ValueError("session has no events to project")
        last_sequence = events[-1].sequence
        if covered_from_sequence != events[0].sequence:
            raise ValueError("a window projection must start at the session's first event")
        if covered_to_sequence < covered_from_sequence or covered_to_sequence > last_sequence:
            raise ValueError(f"covered_to_sequence must be within 1..{last_sequence}")
        covered = [event for event in events if event.sequence <= covered_to_sequence]
        # Integrity: the replaced prefix must be batch-complete, so every assistant tool call it
        # covers is answered inside it and the surviving tail still starts at a message boundary.
        records = context_records(covered)
        if not records[-1].safe_boundary:
            raise ValueError("a window projection must cover whole native tool batches")
        covered_bytes = sum(record.payload_bytes for record in records)
        replacement_bytes = len(replacement.encode("utf-8"))
        if replacement_bytes >= covered_bytes:
            raise ValueError("a window projection must be smaller than the window it replaces")
        lease.staged_events.append(
            EventDraft(
                EventType.WINDOW_PROJECTED,
                "kernel",
                "plugin",
                {
                    "owner": lease.owner,
                    "covered_from_sequence": covered_from_sequence,
                    "covered_to_sequence": covered_to_sequence,
                    "replacement": replacement,
                    "covered_bytes": covered_bytes,
                    "dropped_bytes": covered_bytes - replacement_bytes,
                },
            )
        )

    def fact_page(self, session_id: str, after: int, limit: int) -> FactPage:
        store = self.require_store()
        source = store.events(session_id, after_sequence=after, limit=limit + 1)
        page = source[:limit]
        facts: list[PluginFact] = []
        for event in page:
            context = ()
            dispatch = event.payload.get("dispatch_sequence")
            if event.event_type in (EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED) and type(dispatch) is int:
                context = store.events(session_id, after_sequence=dispatch - 1, limit=1)
            decoded = fact_for_event(event, context)
            if decoded is not None:
                kind, payload = decoded
                facts.append(self._typed_fact(kind, payload, event.session_id, event.run_id, event.revision_id, str(payload.get("logical_turn", ""))))
        return FactPage(tuple(facts), page[-1].sequence if page else after, len(source) > limit)

    def context_history(self, session_id: str, limit: int) -> ContextHistory:
        source = self.require_store().events(session_id, after_sequence=0, limit=limit + 1)
        return ContextHistory(context_records(source[:limit]), len(source) <= limit)

    def transcript_page(self, session_id: str, after: int, limit: int) -> TranscriptPage:
        source = self.require_store().events(session_id, after_sequence=after, limit=limit + 1)
        entries: list[TranscriptEntry] = []
        budget = 64 * 1024
        consumed = 0
        cursor = after
        for event in source[:limit]:
            size = len(json.dumps(json_wire_object(event.payload), ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if consumed and size > budget:
                break
            entry = transcript_entry(event)
            if entry is not None:
                entries.append(entry)
            budget -= size
            consumed += 1
            cursor = event.sequence
        return TranscriptPage(tuple(entries), cursor, len(source) > consumed, consumed)

    def event_views(self, session_id: str, after: int, limit: int, include_payload: bool) -> tuple[EventView, ...]:
        """One bounded page of a session's events; the payload budget is shared by the page."""
        from voidcode.sdk.services import EventView as View

        store = self.require_store()
        views: list[EventView] = []
        budget = 64 * 1024
        for event in store.events(session_id, after_sequence=after, limit=limit):
            payload_wire: dict[str, object] | None = None
            truncated = False
            if include_payload:
                serialized = json.dumps(json_wire_object(event.payload), ensure_ascii=False, allow_nan=False)
                if len(serialized.encode("utf-8")) > budget:
                    truncated = True
                    payload_wire = {"truncated": True, "original_bytes": len(serialized.encode("utf-8"))}
                else:
                    budget -= len(serialized.encode("utf-8"))
                    payload_wire = json_wire_object(event.payload)
            views.append(
                View(
                    sequence=event.sequence,
                    event_type=event.event_type,
                    run_id=event.run_id,
                    revision_id=event.revision_id,
                    source=event.source,
                    actor=event.actor,
                    payload=payload_wire,
                    payload_truncated=truncated,
                )
            )
        return tuple(views)

    def is_descendant(self, child: SessionRecord, caller_id: str) -> bool:
        """Whether the caller sits on the child's own parent chain (never a peer)."""
        store = self.require_store()
        current: SessionRecord | None = child
        while current is not None and current.parent_session_id is not None:
            if current.parent_session_id == caller_id:
                return True
            try:
                current = store.session(current.parent_session_id)
            except KeyError:
                return False
        return False

    def execute_query(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        session_id: str,
        workspace: Path,
    ) -> Mapping[str, Any] | None:
        """Run one named plugin query in this runtime; no core authority is involved."""
        entry = self._queries.get(name)
        if entry is None:
            raise QueryUnavailable(f"no admitted plugin answers the query {name!r}")
        handler, _owner = entry
        result = handler(dict(arguments), PluginQueryCaller(session_id=session_id, workspace=workspace))
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise ValueError(f"query {name!r} must answer with a JSON object or None")
        owned = own_json_object(result)
        if len(json.dumps(json_wire_object(owned), allow_nan=False)) > MAX_QUERY_RESULT_BYTES:
            raise ValueError(f"query {name!r} answer exceeded {MAX_QUERY_RESULT_BYTES} bytes")
        return owned

    # ---- dispatch (direct synchronous calls; a hung plugin blocks) ----

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        return_outcome: bool = False,
        readonly: bool = False,
    ) -> ToolResult | ToolInvocationOutcome:
        """Invoke a registered tool by direct call."""
        if context.workspace.resolve() != self.workspace.resolve() or context.revision_id != self.revision_id:
            raise ValueError(
                f"Context mismatch: expected workspace {self.workspace} revision {self.revision_id}, "
                f"got {context.workspace} revision {context.revision_id}"
            )
        tool_entry = self._tools.get(call.tool_name)
        if tool_entry is None:
            raise ValueError(f"Unknown tool: {call.tool_name}")
        tool_instance, tool_owner = tool_entry
        tool_definition = self._tool_map.get(call.tool_name)
        if tool_definition is None:
            raise ValueError(f"Unknown tool: {call.tool_name}")
        validator = self._validators.get(call.tool_name)
        if validator is not None:
            try:
                validator.validate(json_wire_object(call.arguments))
            except jsonschema.ValidationError as val_err:
                return ToolFailure(
                    tool_name=call.tool_name,
                    error=f"Input validation error: {val_err.message}",
                    output=TextOutput(text=f"Validation error: {val_err.message}"),
                    execution=ConfirmedStop(cancellation_signalled=False),
                )

        # A tool lease is restricted when the session's effective stance is restrictive, even if
        # the tool's own effects are read-tier or spawn-tier. Internal SESSION staging stays
        # available under a read-only stance: it mutates core-owned session data, not the world.
        is_ro = readonly or (is_read_tier(tool_definition.effects) and ToolEffect.EXECUTE not in tool_definition.effects)
        lease = self._open_lease(
            owner=tool_owner,
            session_id=context.session_id,
            run_id=context.run_id,
            invocation_id=context.invocation_id,
            readonly=is_ro,
            actor=context.actor,
            internal_work=(not readonly) or ToolEffect.SESSION in tool_definition.effects,
            invocation_depth=context.invocation_depth,
        )
        services = RuntimeServices(self, lease)
        cancel_event = self.cancel_event_for(lease)
        stop_watcher = threading.Event()
        watcher = threading.Thread(
            target=self._watch_invocation_cancel,
            args=(lease, context.abort_signal, cancel_event, stop_watcher),
            name=f"runtime-cancel-{lease.token[:8]}",
            daemon=True,
        )
        watcher.start()
        progress = self.progress_callback
        if context.emit_tool_progress is None and progress is not None:
            context = replace(context, emit_tool_progress=lambda payload: progress(lease, own_json_object(payload)))
        tool_threw = False
        try:
            result = tool_instance.invoke(call, context=context, services=services)
        except Exception as exc:
            tool_threw = True
            result = ToolFailure(
                tool_name=call.tool_name,
                error=f"Tool {call.tool_name} of {tool_owner} failed: {exc}",
                output=TextOutput(text=f"Tool {call.tool_name} failed: {exc}"),
                execution=ConfirmedStop(cancellation_signalled=False),
            )
        finally:
            stop_watcher.set()
            watcher.join(timeout=5)
        cancelled = cancel_event.is_set() or bool(context.abort_signal and context.abort_signal.cancelled)
        if cancelled:
            # Cooperative cancellation discards everything this invocation staged; claimed
            # service trees were already stopped by the watcher.
            self._cancel_claimed_services(lease)
            staged = ToolInvocationOutcome(result=result)
        elif tool_threw:
            staged = ToolInvocationOutcome(result=result)
        else:
            staged_state, staged_events, staged_conts, staged_cancels, staged_budget = self._staged_changes(lease)
            staged = ToolInvocationOutcome(
                result=result,
                staged_state=staged_state,
                staged_events=staged_events,
                staged_continuations=staged_conts,
                staged_cancellations=staged_cancels,
                staged_budget=staged_budget,
            )
        self._revoke_lease(lease)
        if return_outcome:
            return staged
        return result

    def propose_turn(self, request: ProviderTurnRequest, *, provider: str) -> ProviderTurnResult:
        """Request a model turn from a registered provider by direct call."""
        prov_entry = self._providers.get(provider)
        if prov_entry is None:
            raise ProviderExecutionError(
                kind="not_configured",
                provider_name=provider,
                model_name=request.model_name or "unknown",
                message=f"Provider '{provider}' not configured",
            )
        provider_instance, _provider_id = prov_entry
        try:
            return provider_instance.propose_turn(request)
        except ProviderExecutionError:
            raise
        except Exception as exc:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider,
                model_name=request.model_name or "unknown",
                message=f"Provider {provider} turn failed: {exc}",
            ) from exc

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object],
        *,
        context: ToolContext,
        readonly: bool = False,
    ) -> tuple[
        CommandResult,
        tuple[StagedStateUpdate, ...],
        tuple[EventDraft, ...],
        tuple[StagedContinuationDraft, ...],
        tuple[StagedCancellation, ...],
        PluginBudget | None | Literal["clear"],
    ]:
        """Invoke a registered command by direct call."""
        cmd_entry = self._commands.get(name)
        if cmd_entry is None:
            raise ValueError(f"Unknown command: {name}")
        cmd_instance, cmd_owner = cmd_entry
        cmd_definition = cmd_instance.definition
        args = own_json_object(arguments)

        cmd_validator = self._command_validators.get(name)
        if cmd_validator is not None:
            try:
                cmd_validator.validate(json_wire_object(args))
            except jsonschema.ValidationError as val_err:
                return (CommandResult(status="error", error=f"Input validation error: {val_err.message}"), (), (), (), (), None)

        lease = self._open_lease(
            owner=cmd_owner,
            session_id=session_id,
            run_id=context.run_id,
            invocation_id=context.invocation_id,
            readonly=readonly or (is_read_tier(cmd_definition.effects) and ToolEffect.EXECUTE not in cmd_definition.effects),
            actor=context.actor,
            internal_work=(not readonly) or ToolEffect.SESSION in cmd_definition.effects,
        )
        services = RuntimeServices(self, lease)
        invocation = CommandInvocation(context=context, name=name, arguments=args)
        cancel_event = self.cancel_event_for(lease)
        stop_watcher = threading.Event()
        watcher = threading.Thread(
            target=self._watch_invocation_cancel,
            args=(lease, context.abort_signal, cancel_event, stop_watcher),
            name=f"runtime-cancel-{lease.token[:8]}",
            daemon=True,
        )
        watcher.start()
        cmd_threw = False
        try:
            result = cmd_instance.invoke(invocation, services)
        except Exception as exc:
            cmd_threw = True
            result = CommandResult(status="error", error=f"Command {name} of {cmd_owner} failed: {exc}")
        finally:
            stop_watcher.set()
            watcher.join(timeout=5)
        cancelled = cancel_event.is_set() or bool(context.abort_signal and context.abort_signal.cancelled)
        if cancelled:
            self._cancel_claimed_services(lease)
            staged: _Staged = ((), (), (), (), None)
        elif cmd_threw:
            staged = ((), (), (), (), None)
        else:
            staged = self._staged_changes(lease)
        self._revoke_lease(lease)
        return (result, *staged)

    def render_view(
        self,
        session_id: str,
        name: str,
        *,
        run_id: str = "",
    ) -> PluginViewDescriptor:
        """Render a declarative plugin view for a session by direct call."""
        view_entry = self._views.get(name)
        if view_entry is None:
            raise ValueError(f"Unknown view: {name}")
        view_instance, view_owner = view_entry
        lease = self._open_lease(
            owner=view_owner,
            session_id=session_id,
            run_id=run_id,
            invocation_id=uuid4().hex,
            readonly=True,
            actor="plugin",
        )
        services = RuntimeServices(self, lease)
        try:
            return view_instance.render(session_id, services)
        except Exception as exc:
            raise RuntimeError(f"View {name} of {view_owner} failed: {exc}") from exc
        finally:
            self._revoke_lease(lease)

    def run_lifecycle(
        self,
        owner: str,
        kind: str,
        *,
        session_id: str,
        run_id: str,
        logical_turn: str = "",
        status: str = "",
        error: str | None = None,
        readonly: bool = True,
    ) -> str | None:
        """Notify one owner's lifecycle hook; returns its note (never a decision)."""
        entry = self._lifecycle_hooks.get(f"{owner}\0{kind}")
        if entry is None:
            return None
        callback, _pid = entry
        lifecycle = RunLifecycle(
            phase="start" if kind == "session_start" else "end",
            session_id=session_id,
            run_id=run_id,
            logical_turn=logical_turn,
            status=cast("Literal['completed', 'failed', 'interrupted', '']", status),
            error=error if isinstance(error, str) and error else None,
        )
        lease = self._open_lease(
            owner=owner, session_id=session_id, run_id=run_id, invocation_id=uuid4().hex, readonly=readonly, actor="plugin", hook=True
        )
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=lease.invocation_id,
            revision_id=self.revision_id,
            actor="plugin",
            logical_turn=logical_turn,
        )
        services = RuntimeServices(self, lease)
        try:
            note = callback(lifecycle, context, services)
        except Exception as exc:
            raise RuntimeError(f"Lifecycle hook failed: {type(exc).__name__}: {exc}") from exc
        finally:
            self._revoke_lease(lease)
        if note is not None and not isinstance(note, str):
            raise ValueError(f"{kind} hook of {owner} must return str or None")
        return note

    def pre_tool(
        self,
        owner: str,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        tool_call_id: str | None,
        arguments: Mapping[str, object],
        readonly: bool,
        logical_turn: str = "",
    ) -> str | None:
        """Ask one owner's pre-tool gate whether this call may run.

        Returns a denial reason, or None to allow. The gate observes the intent: it cannot
        rewrite the call, and a failure on this path is the caller's to treat as a refusal.
        """
        lease = self._open_lease(
            owner=owner, session_id=session_id, run_id=run_id, invocation_id=uuid4().hex, readonly=readonly, actor="plugin", hook=True
        )
        intent = ToolIntent(tool_name=tool_name, tool_call_id=tool_call_id, arguments=json_wire_object(arguments))
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=lease.invocation_id,
            revision_id=self.revision_id,
            actor="plugin",
            logical_turn=logical_turn,
        )
        services = RuntimeServices(self, lease)
        try:
            for callback, _, _, plugin_id in self._pre_tool_hooks:
                if plugin_id != owner:
                    continue
                try:
                    reason = callback(intent, context, services)
                except Exception as exc:
                    raise RuntimeError(f"Gate failed: {type(exc).__name__}: {exc}") from exc
                if reason is not None:
                    if not isinstance(reason, str) or not reason.strip():
                        raise ValueError(f"pre_tool gate of {plugin_id} must return a non-empty reason or None")
                    return reason
            return None
        finally:
            self._revoke_lease(lease)

    def post_tool(
        self,
        owner: str,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        tool_call_id: str | None,
        status: str,
        text: str,
        arguments: Mapping[str, object],
        readonly: bool,
        logical_turn: str = "",
    ) -> str | None:
        """Run one owner's post-tool hook inside the governed invocation.

        The hook observes a settled result under its own lease: it cannot start nested
        governed work, and its failure is the caller's to record, never a rewrite of the
        tool verdict this invocation already decided.
        """
        lease = self._open_lease(
            owner=owner, session_id=session_id, run_id=run_id, invocation_id=uuid4().hex, readonly=readonly, actor="plugin", hook=True
        )
        outcome = ToolOutcome(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            status=cast("Literal['ok', 'error', 'cancelled']", status),
            text=text,
            arguments=json_wire_object(arguments),
        )
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=lease.invocation_id,
            revision_id=self.revision_id,
            actor="plugin",
            logical_turn=logical_turn,
        )
        services = RuntimeServices(self, lease)
        try:
            note: str | None = None
            for callback, _, _, plugin_id in self._post_tool_hooks:
                if plugin_id != owner:
                    continue
                try:
                    returned = callback(outcome, context, services)
                except Exception as exc:
                    raise RuntimeError(f"Hook failed: {type(exc).__name__}: {exc}") from exc
                if returned is not None:
                    if not isinstance(returned, str):
                        raise ValueError(f"post_tool hook of {plugin_id} must return str or None")
                    note = returned
            return note
        finally:
            self._revoke_lease(lease)

    def contribute_context(
        self,
        session_id: str,
        *,
        run_id: str = "",
        logical_turn: str = "",
    ) -> tuple[dict[str, object], ...]:
        contributions: list[dict[str, object]] = []
        for plugin_id, callback in self._context_callbacks.items():
            lease = self._open_lease(owner=plugin_id, session_id=session_id, run_id=run_id, invocation_id=uuid4().hex, readonly=True, actor="plugin")
            context = ToolContext(
                workspace=self.workspace,
                session_id=session_id,
                run_id=run_id,
                invocation_id=lease.invocation_id,
                revision_id=self.revision_id,
                actor="plugin",
                logical_turn=logical_turn,
            )
            services = RuntimeServices(self, lease)
            try:
                contribution = callback(context, services)
            except Exception as exc:
                raise RuntimeError(f"Context provider {plugin_id} failed: {type(exc).__name__}: {exc}") from exc
            finally:
                self._revoke_lease(lease)
            if isinstance(contribution, ContextContribution):
                contributions.append({"type": "text", "content": contribution.content, "owner": plugin_id, "role": contribution.role})
        return tuple(contributions)

    def notify_fact(
        self,
        fact_type: str,
        fact: Mapping[str, object],
        *,
        owners: Sequence[str] | None = None,
        logical_turn: str = "",
    ) -> CallbackChanges:
        """Notify subscribers of one fact; `owners` restricts delivery to those captured subscribers."""
        subscribed_owners = self._observer_owners.get(fact_type, ())
        if owners is not None:
            requested = frozenset(owners)
            subscribed_owners = tuple(owner for owner in subscribed_owners if owner in requested)
        if not subscribed_owners:
            return CallbackChanges()

        all_state_updates: list[StagedStateUpdate] = []
        all_events: list[EventDraft] = []
        all_conts: list[StagedContinuationDraft] = []
        all_cancels: list[StagedCancellation] = []
        all_budgets: list[tuple[str, PluginBudget | Literal["clear"]]] = []
        all_errors: list[dict[str, str]] = []

        session_id = _required_fact_field(fact, "session_id")
        run_id = _required_fact_field(fact, "run_id")
        revision_id = _required_fact_field(fact, "revision_id")
        typed = self._typed_fact(fact_type, fact, session_id, run_id, revision_id, str(fact.get("logical_turn", logical_turn)))

        for owner in subscribed_owners:
            lease = self._open_lease(owner=owner, session_id=session_id, run_id=run_id, invocation_id=uuid4().hex, readonly=False, actor="plugin")
            context = ToolContext(
                workspace=self.workspace,
                session_id=session_id,
                run_id=run_id,
                invocation_id=lease.invocation_id,
                revision_id=revision_id,
                actor="plugin",
                logical_turn=str(fact.get("logical_turn", logical_turn)),
            )
            services = RuntimeServices(self, lease)
            try:
                for obs, observed_kinds, plugin_id in self._observers:
                    if plugin_id != owner or fact_type not in observed_kinds:
                        continue
                    try:
                        obs(typed, context, services)
                    except Exception as exc:
                        # A raised observer callback invalidates every proposal it staged:
                        # truth (bill/fact) stays, its partial side effects do not.
                        all_errors.append({"owner": plugin_id, "error": f"{type(exc).__name__}: {exc}"})
                        break
                else:
                    owner_schema_ver = self.state_schema_version(lease.owner)
                    all_state_updates.extend(
                        StagedStateUpdate(
                            owner=lease.owner,
                            scope=sc,
                            scope_key=sk,
                            payload=st,
                            expected_version=baseline,
                            schema_version=owner_schema_ver,
                            revision_id=self.revision_id,
                        )
                        for (sc, sk), (st, _local, baseline) in lease.staged_state.items()
                    )
                    all_events.extend(lease.staged_events)
                    all_conts.extend(lease.staged_continuations)
                    all_cancels.extend(lease.staged_cancellations)
                    if lease.staged_budget is not None:
                        all_budgets.append((lease.owner, lease.staged_budget))
                    continue
            finally:
                self._revoke_lease(lease)

        return CallbackChanges(
            staged_state=tuple(all_state_updates),
            staged_events=tuple(all_events),
            staged_continuations=tuple(all_conts),
            staged_cancellations=tuple(all_cancels),
            staged_budgets=tuple(all_budgets),
            errors=tuple(all_errors),
        )

    @staticmethod
    def _typed_fact(fact_type: str, fact: Mapping[str, object], session_id: str, run_id: str, revision_id: str, logical_turn: str) -> PluginFact:
        if fact_type == "usage":
            usage_payload = fact.get("usage")
            usage_obj = None
            if isinstance(usage_payload, Mapping):
                usage_obj = ProviderTokenUsage(
                    input_tokens=usage_payload.get("input_tokens"),
                    output_tokens=usage_payload.get("output_tokens"),
                    cache_read_tokens=usage_payload.get("cache_read_tokens"),
                    cache_write_tokens=usage_payload.get("cache_write_tokens"),
                    uncached_input_tokens=usage_payload.get("uncached_input_tokens"),
                    cost_usd=_optional_number(usage_payload, "cost_usd"),
                )
            return UsageFact(
                session_id=session_id,
                run_id=run_id,
                revision_id=revision_id,
                logical_turn=logical_turn,
                event_sequence=_required_int(fact, "event_sequence"),
                dispatch_sequence=_required_int(fact, "dispatch_sequence"),
                provider_name=_required_text(fact, "provider_name"),
                model_name=_required_text(fact, "model_name"),
                usage=usage_obj,
                elapsed_seconds=_optional_number(fact, "elapsed_seconds"),
                cost_usd=_optional_number(fact, "cost_usd"),
            )
        if fact_type == "invocation_finished":
            call_id = fact.get("tool_call_id")
            if call_id is not None and not isinstance(call_id, str):
                raise ValueError("'tool_call_id' must be a string or null")
            raw_status = fact.get("status")
            if raw_status not in ("ok", "error", "cancelled"):
                raise ValueError(f"Unknown invocation status: {raw_status!r}")
            error = fact.get("error")
            if error is not None and not isinstance(error, str):
                raise ValueError("'error' must be a string or null")
            raw_effects = fact.get("effects", ())
            if not isinstance(raw_effects, (list, tuple)) or not all(isinstance(item, str) for item in raw_effects):
                raise ValueError("'effects' must be a list of strings")
            return TypeAdapter(InvocationFinishedFact).validate_python(json_wire_object(fact))
        extra_types = {
            "run_requested": RunRequestedFact,
            "approval_requested": ApprovalRequestedFact,
            "child_spawned": ChildSpawnedFact,
            "context_projected": ContextProjectedFact,
            "wakeup_fired": WakeupFiredFact,
        }
        if fact_type in extra_types:
            return TypeAdapter(extra_types[fact_type]).validate_python(json_wire_object(fact))
        if fact_type == "run_finished":
            stop_cause_payload = fact.get("stop_cause")
            if stop_cause_payload is not None and not isinstance(stop_cause_payload, str):
                raise ValueError("'stop_cause' must be a string or null")
            status_payload = _required_text(fact, "status")
            if status_payload not in ("completed", "failed", "interrupted"):
                raise ValueError(f"Unknown run status: {status_payload!r}")
            return RunFinishedFact(
                session_id=session_id,
                run_id=run_id,
                revision_id=revision_id,
                logical_turn=logical_turn,
                event_sequence=_required_int(fact, "event_sequence"),
                status=status_payload,
                stop_cause=cast(StopCause | None, stop_cause_payload),
                elapsed_seconds=_optional_number(fact, "elapsed_seconds"),
            )
        raise ValueError(f"Unknown fact_type: {fact_type}")

    def prepare_deactivate_owner_state(
        self,
        owner: str,
        payload: Mapping[str, object],
        *,
        scope: str,
        scope_key: str,
        tip_sequence: int | None,
    ) -> dict[str, Any]:
        """Run one owner's pure deactivation transform in the runtime that still holds its schema."""
        if scope not in ("session", "workspace"):
            raise RuntimeError("scope must be 'session' or 'workspace'")
        if scope == "session" and (isinstance(tip_sequence, bool) or not isinstance(tip_sequence, int) or tip_sequence < 0):
            raise RuntimeError("session-scope deactivation requires the owning session's event tip as a non-negative integer")
        if scope == "workspace" and tip_sequence is not None:
            raise RuntimeError("workspace-scope deactivation has no single session event tip")
        transform = self._state_deactivators.get(owner)
        if transform is None:
            raise RuntimeError(f"plugin {owner} declares no prepare_deactivate transform")
        try:
            prepared = transform(payload, scope, scope_key, tip_sequence)
        except Exception as exc:
            raise RuntimeError(f"state deactivation of {owner} ({scope}:{scope_key}) failed: {exc}") from exc
        if not isinstance(prepared, Mapping):
            raise RuntimeError("prepare_deactivate must return a JSON object")
        return json_wire_object(prepared)

    def stop_background_processes(self) -> None:
        """Owner shutdown: supervised background processes end with the core that started them.

        A generation swap is not an owner shutdown: a background process started by one
        generation keeps running across a reload, which is exactly why the caller persists
        its identity.
        """
        self.processes.stop_background()

    def _forget_module(self, name: str) -> None:
        sys.modules.pop(name, None)

    def close(self) -> None:
        """Retire this generation: stop its services, run close callbacks, unimport its modules."""
        if self._closed:
            return
        self._closed = True
        with self._lease_lock:
            for lease in self._active_leases.values():
                lease.revoked = True
            self._active_leases.clear()
            self._claimed_services.clear()
            self._cancel_events.clear()
        # Child and service processes started by this generation stop with it;
        # supervised background jobs are owned separately and survive a swap.
        try:
            self.processes.reap_all()
        finally:
            self.processes.stop_services()
        # Run close callbacks for loaded plugins, propagating first error after unimport.
        # A hung callback blocks retire until core restart — there is no kill isolation.
        first_error: BaseException | None = None
        for plugin in self._plugins:
            if plugin.close is not None and callable(plugin.close):
                try:
                    plugin.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        # Unimport generation namespaces so reloaded generations avoid stale modules.
        try:
            for name in self._modules:
                self._forget_module(name)
            self._modules.clear()
        finally:
            if first_error is not None:
                raise first_error

    def __enter__(self) -> PluginRuntime:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def _required_fact_field(fact: Mapping[str, object], field_name: str) -> str:
    """Read one required observer-fact identity field, failing visibly when absent."""
    value = fact.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"observer fact requires a non-empty {field_name!r}")
    return value


def _required_text(params: Mapping[str, Any], key: str) -> str:
    """Read a required non-empty text parameter, failing visibly instead of defaulting."""
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' parameter required as non-empty string")
    return value


def _optional_text(params: Mapping[str, Any], key: str, default: str = "") -> str:
    """Read an optional text parameter; an empty string is an explicit 'no value'."""
    value = params.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"'{key}' parameter must be a string")
    return value


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    """Read a required integer field without coercing strings or booleans."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{key}' must be an integer")
    return value


def _optional_number(payload: Mapping[str, Any], key: str) -> float | None:
    """Read an optional numeric field without coercing strings or booleans."""
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{key}' must be a number or null")
    return float(value)
