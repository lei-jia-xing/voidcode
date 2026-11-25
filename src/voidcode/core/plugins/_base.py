"""Shared state and core-facing accessors for one in-process plugin generation.

`PluginRuntime.__init__` initializes this state; the private capability mixins
inherit these declarations so every concern is checked against one contract.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from voidcode.sdk.commands import Command, CommandDescriptor
    from voidcode.sdk.hooks import ContextContribution, ObservedFactKind, PluginFact, RunLifecycle, ToolIntent, ToolOutcome
    from voidcode.sdk.invocation import ToolContext
    from voidcode.sdk.plugin import Plugin, PluginQueryCaller, StateSchema, StateScope
    from voidcode.sdk.provider import ProviderAbortSignal, TurnProvider
    from voidcode.sdk.resources import ResourcePage, ResourceReadCaller, ResourceReaderServices, ResourceReadRequest
    from voidcode.sdk.services import (
        BackgroundContext,
        CancelSubtreeReceipt,
        ChildOutcome,
        ChildSendReceipt,
        ChildSessionSnapshot,
        DescendantNode,
        PluginBudget,
        PluginServices,
        SessionUsageReport,
        StructuredModelResult,
    )
    from voidcode.sdk.tools import Tool, ToolDefinition
    from voidcode.sdk.views import PluginView

    from ..artifacts import ArtifactStore
    from ..child_contracts import ChildCompletionReceipt, ChildSpawnRequest, ChildWaitDelivery
    from ..processes import ProcessHost, ProcessRegistry
    from ..sandbox import SandboxBackend
    from ..storage.store import ContinuationRecord, SqliteStore
    from ._contracts import LeaseContext, PluginSource

RESERVED_COMMAND_NAMES: frozenset[str] = frozenset({"run", "resume", "show", "cancel", "close", "init", "help", "status", "sessions", "catalog"})
RESERVED_COMMAND_PREFIXES: tuple[str, ...] = ("core.", "voidcode.")
#: Names the core itself registers, which a plugin can never shadow.
RESERVED_TOOL_NAMES: frozenset[str] = frozenset({"core_reload"})
#: One plugin query answer stays a small JSON object: it is a read convenience, never a transport.
MAX_QUERY_RESULT_BYTES: Final[int] = 256 * 1024


class PluginRuntimeState:
    """Declarations for the runtime fields plus the narrow core-facing accessors."""

    workspace: Path
    revision_id: str
    process_host: ProcessHost
    processes: ProcessRegistry
    sandbox_backend: SandboxBackend | None
    usage_callback: Callable[[str, int, int | None], SessionUsageReport] | None
    budget_callback: Callable[[str, str], PluginBudget | None] | None
    spawn_child_callback: Callable[[ChildSpawnRequest], str] | None
    find_child_callback: Callable[[str, str, str], str | None] | None
    join_child_callback: Callable[[str, str, float], ChildOutcome] | None
    wait_children_callback: Callable[[str, str, tuple[str, ...], Callable[[], bool]], tuple[ChildWaitDelivery, ...]] | None
    acknowledge_child_completion_callback: Callable[[str, str, str, str], ChildCompletionReceipt | None] | None
    send_child_callback: Callable[[str, str, str, str, bool, Callable[[], bool]], ChildSendReceipt] | None
    cancel_child_callback: Callable[[str, str, str], bool] | None
    list_descendants_callback: Callable[[str], tuple[DescendantNode, ...]] | None
    read_child_snapshot_callback: Callable[[str, str], ChildSessionSnapshot | None] | None
    cancel_subtree_callback: Callable[[str, str, str | None, str], CancelSubtreeReceipt] | None
    cancel_background_action_callback: Callable[[str, str, str, str], bool] | None
    model_query_callback: Callable[[str, str, str | None, int | None, bool, str | None, ProviderAbortSignal | None], str] | None
    model_query_structured_callback: (
        Callable[[str, str, Mapping[str, object], str | None, int | None, bool, str | None, ProviderAbortSignal | None], StructuredModelResult] | None
    )
    progress_callback: Callable[[LeaseContext, Mapping[str, object]], None] | None
    tool_definition_callback: Callable[[str, str, str], tuple[Mapping[str, object] | None, str]] | None
    session_read_only_callback: Callable[[str], bool] | None
    background_action_notify_callback: Callable[[], None] | None
    background_claim_live_callback: Callable[[ContinuationRecord], bool] | None

    _store: SqliteStore | None
    _artifacts: ArtifactStore | None
    _namespace: str
    _modules: list[str]
    _sources: tuple[PluginSource, ...]
    _lease_lock: threading.Lock
    _active_leases: dict[str, LeaseContext]
    _claimed_services: dict[str, set[tuple[str, str]]]
    _cancel_events: dict[str, threading.Event]
    _closed: bool

    _tools: dict[str, tuple[Tool, str]]
    _providers: dict[str, tuple[TurnProvider, str]]
    _commands: dict[str, tuple[Command, str]]
    _views: dict[str, tuple[PluginView, str]]
    _context_callbacks: dict[str, Callable[[ToolContext, PluginServices], ContextContribution | None]]
    _post_tool_hooks: list[tuple[Callable[[ToolOutcome, ToolContext, PluginServices], str | None], tuple[str, ...], bool, str]]
    _pre_tool_hooks: list[tuple[Callable[[ToolIntent, ToolContext, PluginServices], str | None], tuple[str, ...], bool, str]]
    _lifecycle_hooks: dict[str, tuple[Callable[[RunLifecycle, ToolContext, PluginServices], str | None], str]]
    _observers: list[tuple[Callable[[PluginFact, ToolContext, PluginServices], None], tuple[ObservedFactKind, ...], str]]
    _background_actions: dict[str, Callable[[BackgroundContext], None]]
    _state_deactivators: dict[str, Callable[[Mapping[str, object], StateScope, str, int | None], Mapping[str, object]]]
    _queries: dict[str, tuple[Callable[[Mapping[str, object], PluginQueryCaller], Mapping[str, object] | None], str]]
    _resource_readers: dict[str, tuple[Callable[[ResourceReadRequest, ResourceReadCaller, ResourceReaderServices], ResourcePage | None], str]]
    _plugins: list[Plugin]
    _plugin_ids: list[str]
    _plugin_states: dict[str, StateSchema]

    _tool_defs: tuple[ToolDefinition, ...]
    _tool_map: dict[str, ToolDefinition]
    _provider_names: tuple[str, ...]
    _tool_owners: dict[str, str]
    _validators: dict[str, Any]
    _command_descs: tuple[CommandDescriptor, ...]
    _command_map: dict[str, CommandDescriptor]
    _command_map_view: Mapping[str, CommandDescriptor]
    _command_validators: dict[str, Any]
    _command_prep_validators: dict[str, Any]
    _view_names: tuple[str, ...]
    _view_owners: dict[str, str]
    _tool_map_view: Mapping[str, ToolDefinition]
    _validators_view: Mapping[str, Any]
    _command_validators_view: Mapping[str, Any]
    _context_owners: tuple[str, ...]
    _observer_owners: dict[ObservedFactKind, tuple[str, ...]]

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
