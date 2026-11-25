"""In-process plugin runtime: direct factory loading and synchronous dispatch.

`PluginRuntime` is the stable facade for one loaded generation. Loading,
catalog admission, leases, dispatch, fact delivery, lifecycle hooks, and state
seams live in private mixins; only the facade and the invocation contracts
callers already depend on are exported.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import uuid4

from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.services import (
    CancelSubtreeReceipt,
    ChildOutcome,
    ChildSendReceipt,
    ChildSessionSnapshot,
    DescendantNode,
    PluginBudget,
    SessionUsageReport,
    StructuredModelResult,
)

from ..artifacts import ArtifactStore
from ..child_contracts import ChildCompletionReceipt, ChildSpawnRequest, ChildWaitDelivery
from ..processes import ProcessHost, ProcessRegistry
from ..sandbox import SandboxBackend
from ..storage.store import SqliteStore, UnknownWorkspace
from ._base import MAX_QUERY_RESULT_BYTES, RESERVED_COMMAND_NAMES, RESERVED_COMMAND_PREFIXES, RESERVED_TOOL_NAMES
from ._contracts import (
    CallbackChanges,
    CommandInvocationOutcome,
    FrozenStaged,
    LeaseContext,
    LeaseIdentity,
    LeaseStaging,
    PluginSource,
    ToolInvocationOutcome,
    ToolSuspension,
)
from ._lifecycle import PluginLifecycle

__all__ = (
    "CallbackChanges",
    "CommandInvocationOutcome",
    "FrozenStaged",
    "LeaseContext",
    "LeaseIdentity",
    "LeaseStaging",
    "MAX_QUERY_RESULT_BYTES",
    "PluginRuntime",
    "PluginSource",
    "RESERVED_COMMAND_NAMES",
    "RESERVED_COMMAND_PREFIXES",
    "RESERVED_TOOL_NAMES",
    "ToolInvocationOutcome",
    "ToolSuspension",
)


class PluginRuntime(PluginLifecycle):
    """One loaded plugin generation, dispatched synchronously; shares its Core's ProcessHost."""

    def __init__(
        self,
        sources: tuple[PluginSource, ...],
        *,
        workspace: Path,
        revision_id: str,
        store: SqliteStore | None = None,
        usage_callback: Callable[[str, int, int | None], SessionUsageReport] | None = None,
        budget_callback: Callable[[str, str], PluginBudget | None] | None = None,
        spawn_child_callback: Callable[[ChildSpawnRequest], str] | None = None,
        find_child_callback: Callable[[str, str, str], str | None] | None = None,
        join_child_callback: Callable[[str, str, float], ChildOutcome] | None = None,
        wait_children_callback: Callable[[str, str, tuple[str, ...], Callable[[], bool]], tuple[ChildWaitDelivery, ...]] | None = None,
        acknowledge_child_completion_callback: Callable[[str, str, str, str], ChildCompletionReceipt | None] | None = None,
        send_child_callback: Callable[[str, str, str, str, bool, Callable[[], bool]], ChildSendReceipt] | None = None,
        cancel_child_callback: Callable[[str, str, str], bool] | None = None,
        list_descendants_callback: Callable[[str], tuple[DescendantNode, ...]] | None = None,
        read_child_snapshot_callback: Callable[[str, str], ChildSessionSnapshot | None] | None = None,
        cancel_subtree_callback: Callable[[str, str, str | None, str], CancelSubtreeReceipt] | None = None,
        cancel_background_action_callback: Callable[[str, str, str, str], bool] | None = None,
        model_query_callback: Callable[[str, str, str | None, int | None, bool, str | None, ProviderAbortSignal | None], str] | None = None,
        model_query_structured_callback: (
            Callable[
                [str, str, Mapping[str, object], str | None, int | None, bool, str | None, ProviderAbortSignal | None],
                StructuredModelResult,
            ]
            | None
        ) = None,
        progress_callback: Callable[[LeaseContext, Mapping[str, object]], None] | None = None,
        tool_definition_callback: Callable[[str, str, str], tuple[Mapping[str, object] | None, str]] | None = None,
        session_read_only_callback: Callable[[str], bool] | None = None,
        artifacts_dir: Path | None = None,
        sandbox_backend: SandboxBackend | None = None,
        process_host: ProcessHost | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise UnknownWorkspace(f"Workspace directory does not exist: {self.workspace}", self.workspace)
        if not revision_id:
            raise ValueError("revision_id must be a non-empty string")
        self.revision_id = revision_id
        self._store = store
        self.usage_callback = usage_callback
        self.budget_callback = budget_callback
        self.spawn_child_callback = spawn_child_callback
        self.find_child_callback = find_child_callback
        self.join_child_callback = join_child_callback

        self.wait_children_callback = wait_children_callback
        self.acknowledge_child_completion_callback = acknowledge_child_completion_callback
        self.send_child_callback = send_child_callback
        self.cancel_child_callback = cancel_child_callback
        self.list_descendants_callback = list_descendants_callback
        self.read_child_snapshot_callback = read_child_snapshot_callback
        self.cancel_subtree_callback = cancel_subtree_callback
        self.cancel_background_action_callback = cancel_background_action_callback
        self.model_query_callback = model_query_callback
        self.model_query_structured_callback = model_query_structured_callback
        self.progress_callback = progress_callback
        self.tool_definition_callback = tool_definition_callback
        self.session_read_only_callback = session_read_only_callback
        #: Core-owned background wiring: the claim loop's wakeup and the claim liveness check.
        self.background_action_notify_callback: Callable[[], None] | None = None
        self.background_claim_live_callback = None
        self._artifacts = None if artifacts_dir is None else ArtifactStore(artifacts_dir)
        self._namespace = f"voidcode_runtime_{uuid4().hex}"
        self._modules = []

        seen: set[str] = set()
        for source in sources:
            if source.plugin_id in seen:
                raise ValueError(f"Duplicate plugin id: {source.plugin_id}")
            seen.add(source.plugin_id)
            if not source.root.is_dir():
                raise ValueError(f"Plugin source is not a directory: {source.root}")

        self._sources = tuple(sources)
        self._lease_lock = threading.Lock()
        self._active_leases = {}
        self._claimed_services = {}
        self._cancel_events = {}
        self._closed = False
        #: Core-lifetime background supervisor; reload candidates receive the same host.
        self.process_host = process_host if process_host is not None else ProcessHost()
        #: Every child process this generation started; reaped/stopped on close.
        self.processes = ProcessRegistry(self.process_host)
        #: Transparent exec backend for this generation; None means unconfined host exec.
        self.sandbox_backend: SandboxBackend | None = sandbox_backend

        self._tools = {}
        self._providers = {}
        self._commands = {}
        self._views = {}
        self._context_callbacks = {}
        self._post_tool_hooks = []
        self._pre_tool_hooks = []
        self._lifecycle_hooks = {}
        self._observers = []
        self._background_actions = {}
        self._state_deactivators = {}
        self._queries = {}
        self._resource_readers = {}

        self._plugins = []
        self._plugin_ids = []
        self._plugin_states = {}
        try:
            for source in sources:
                self._load_plugin(source)

            self._build_catalog()
        except Exception:
            self.close()
            raise
