"""Governed turns over immutable revisions and durable native tool batches."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any, Literal
from uuid import uuid4

import jsonschema

from ...sdk.client import (
    ApprovalPolicyRule,
    BudgetExhaustion,
    FollowupListReceipt,
    FollowupWithdrawReceipt,
    QueuedFollowup,
)
from ...sdk.codec import (
    decode_tool_call,
    encode_tool_call,
)
from ...sdk.command_help import grouped_help_entries
from ...sdk.commands import (
    CommandCatalog,
    CommandDescriptor,
    CommandInputSnapshot,
    CommandResult,
)
from ...sdk.hooks import ObservedFactKind, StopCause
from ...sdk.icons import Icon
from ...sdk.invocation import ToolContext
from ...sdk.json_values import json_wire_object
from ...sdk.observed_effects import MAX_OBSERVED_CHANGES
from ...sdk.plugin import StateScope
from ...sdk.provider import ImageAttachment, ProviderAbortSignal, ProviderMessage, ProviderReasoningEffort
from ...sdk.services import (
    ArtifactPage,
    CancelSubtreeReceipt,
    ChildOutcome,
    ChildSendReceipt,
    ContinuationResult,
    DescendantNode,
    PluginBudget,
    SessionUsageReport,
    StructuredModelResult,
)
from ...sdk.tools import (
    Change,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolResult,
)
from ...sdk.transcript import ContextHistory, TranscriptPage
from ...sdk.views import PluginViewDescriptor, ViewsSnapshot
from ..activation import Admission, ExecutionConfig
from ..activation_coordinator import ActivationCoordinator, ActivationFenced, ActivationSnapshot, PreparedActivation
from ..artifacts import ArtifactStore
from ..background_actions import BackgroundActionRegistry
from ..background_execution import BackgroundExecution
from ..cancellation import CURRENT, Cancellation, CancellationCoordinator, CancelTarget, RunBoundTarget
from ..child_contracts import ChildRunStatus, ChildSpawnRequest
from ..context_projection import ContextProjector
from ..delegation import (
    DelegationManager,
)
from ..events import EventType
from ..execution import _Cursor, _Execution, committed_event, wire_object
from ..fact_delivery import BirthAuthorization, FactDelivery
from ..output_projection import ResultProjector
from ..plugins import LeaseContext, PluginRuntime, PluginSource
from ..processes import ProcessHost
from ..sandbox import SandboxBackend
from ..storage.store import (
    DATABASE_FILENAME,
    Checkpoint,
    CheckpointKind,
    ContinuationRecord,
    ContinuationStatus,
    EventDraft,
    InvalidRevision,
    ObserverReceiptRecord,
    SequenceConflict,
    SessionRecord,
    SessionStatus,
    SqliteStore,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedConfirmation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
    StaleRevision,
    StoreConflict,
    StoredEvent,
    WakeupRecord,
    WakeupSkip,
    canonical_json,
)
from ..usage import UsageProjector
from .command_runtime import CommandPlan, CommandRuntime
from .commit_pipeline import CheckpointBinding, CommitPipeline, RootBudgetGrant
from .continuation_scheduler import ContinuationScheduler
from .followup import FollowupManager, FollowupSubmission
from .governance import (
    GovernanceSnapshot,
    ToolGovernance,
)
from .interactive_ask import (
    AskBinding,
    AskOutcome,
    InteractiveAskManager,
    is_ask_binding_expired,
    is_parked_intervening,
)
from .model_query import ModelQueryRuntime
from .run_coordinator import RunCoordinator
from .tool_execution import ToolExecutionManager, ToolExecutionSnapshot
from .turn_execution import CoreResult, TurnDriver

logger = logging.getLogger(__name__)

#: Bounded window for cooperative drivers to release dispatch slots before an activation
#: fence refuses. Long model turns and tools are already refused by the activation blockers.
_ACTIVATION_DRAIN_SECONDS = 2.0

_PARKED_INTERVENING_TYPES = frozenset(
    {
        EventType.COMMAND_COMPLETED,
        EventType.CONFIRMATION_REQUESTED,
        EventType.CONFIRMATION_DECIDED,
        EventType.CHILD_SPAWNED,
        EventType.CHILD_RESERVED,
        EventType.CHILD_FINISHED,
        EventType.FOLLOWUP_QUEUED,
        EventType.FOLLOWUP_WITHDRAWN,
        EventType.MODEL_QUERY,
        EventType.TOOL_DEFERRED,
        EventType.TOOL_PROGRESS,
        EventType.PROVIDER_PROGRESS,
        EventType.OBSERVER_ERROR,
        EventType.WINDOW_PROJECTED,
        EventType.PROVIDER_INTERRUPTED,
        EventType.ACTIVATION_REQUESTED,
        EventType.ACTIVATION_COMMITTED,
        EventType.ACTIVATION_FAILED,
    }
)


_CORE_COMMAND_SPECS: tuple[tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], bool], ...] = (
    (
        "run",
        "Run an agent turn with a user prompt",
        {
            "type": "object",
            "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 65536}},
            "required": ["prompt"],
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
        True,
    ),
    (
        "resume",
        "Resume a paused session (approval or interrupted)",
        {
            "type": "object",
            "properties": {
                "approval_request_id": {"type": "string", "minLength": 1},
                "decision": {"enum": ["allow", "deny"]},
            },
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
        True,
    ),
    (
        "show",
        "Show session state, checkpoint, and events",
        {"type": "object", "properties": {"events": {"type": "boolean"}}, "additionalProperties": False},
        (ToolEffect.READ,),
        False,
    ),
    ("status", "Show workspace status", {"type": "object", "additionalProperties": False}, (ToolEffect.READ,), False),
    ("help", "Show available commands and documentation", {"type": "object", "additionalProperties": False}, (ToolEffect.READ,), False),
)
#: Tools the core itself dispatches. A plugin can never shadow these names.
_CORE_TOOL_SPECS: tuple[tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], Icon], ...] = (
    (
        "core_reload",
        "Request that the configured plugins be re-admitted as a new capability revision. This only stages the "
        "request: the switch happens after the current tool batch finishes, and it needs user approval first.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string", "maxLength": 500}},
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE,),
        Icon(glyph="↻"),
    ),
)


#: One truthful, actionable message for a providerless core. Launch, commands, tools, and
#: sessions stay available; only a model turn or governed query fails with this text.
MODEL_CONFIGURATION_GUIDANCE = "no model configured or registered"

#: At most this many supervisor-observed changes join one tool result at merge time. The
#: helper already caps its own report at MAX_OBSERVED_CHANGES; this only bounds multi-child
#: accumulation within one invocation.
MAX_MERGED_OBSERVATIONS = MAX_OBSERVED_CHANGES
#: Reserved core command that activates the configured sources on explicit request.
RELOAD_COMMAND_NAME = "core.reload"
_RELOAD_COMMAND_SPEC: tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], bool] = (
    RELOAD_COMMAND_NAME,
    "Re-admit the configured plugin sources and switch this workspace to the new capability revision.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
    True,
)


def _core_command_descriptors(*, with_reload: bool) -> tuple[CommandDescriptor, ...]:
    """Reserved core command descriptors, captured together with the revision that admits them."""
    specs = (*_CORE_COMMAND_SPECS, _RELOAD_COMMAND_SPEC) if with_reload else _CORE_COMMAND_SPECS
    return tuple(
        CommandDescriptor(
            name=name,
            owner="core",
            description=description,
            argument_schema=dict(schema),
            effects=effects,
            busy_gate=busy_gate,
        )
        for name, description, schema, effects, busy_gate in specs
    )


def _core_tool_definitions() -> dict[str, ToolDefinition]:
    return {
        name: ToolDefinition(name=name, description=description, input_schema=dict(schema), effects=frozenset(effects), icon=icon)
        for name, description, schema, effects, icon in _CORE_TOOL_SPECS
    }


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """One immutable capability generation. Every dispatch reads this single pointer."""

    revision_id: str
    config: ExecutionConfig
    admission: Admission | None
    plugin_runtime: PluginRuntime
    native_tools: Mapping[str, ToolDefinition]
    native_validators: Mapping[str, Any]
    native_commands: Mapping[str, CommandDescriptor]
    tools: tuple[ToolDefinition, ...]
    commands: tuple[CommandDescriptor, ...]

    @staticmethod
    def build(
        *,
        revision_id: str,
        config: ExecutionConfig,
        admission: Admission | None,
        plugin_runtime: PluginRuntime,
        with_reload: bool,
    ) -> _Snapshot:
        native_tools = MappingProxyType(_core_tool_definitions()) if with_reload else MappingProxyType({})
        for name in plugin_runtime.tool_definitions:
            if name in native_tools:
                raise ValueError(f"plugin tool {name!r} collides with a reserved tool")
        native_validators_dict = {}
        for name, definition in native_tools.items():
            if definition.input_schema:
                schema_json = json_wire_object(definition.input_schema)
                jsonschema.Draft202012Validator.check_schema(schema_json)
                native_validators_dict[name] = jsonschema.Draft202012Validator(schema_json)
        native_validators = MappingProxyType(native_validators_dict)
        native_commands = MappingProxyType({desc.name: desc for desc in _core_command_descriptors(with_reload=with_reload)})
        return _Snapshot(
            revision_id=revision_id,
            config=config,
            admission=admission,
            plugin_runtime=plugin_runtime,
            native_tools=native_tools,
            native_validators=native_validators,
            native_commands=native_commands,
            tools=(*plugin_runtime.tools, *native_tools.values()),
            commands=(*native_commands.values(), *plugin_runtime.commands),
        )

    def definition_of(self, name: str) -> ToolDefinition | None:
        return self.native_tools.get(name) or self.plugin_runtime.tool_definitions.get(name)

    def validator_of(self, name: str) -> Any | None:
        return self.native_validators.get(name) or self.plugin_runtime.tool_validators.get(name)

    def command_of(self, name: str) -> CommandDescriptor | None:
        return self.native_commands.get(name) or self.plugin_runtime.command_descriptors.get(name)

    def owner_of(self, tool_name: str) -> str:
        return "core" if tool_name in self.native_tools else self.plugin_runtime.owner(tool_name)


_is_parked_intervening = is_parked_intervening


_AskBinding = AskBinding


class Core:
    def __init__(
        self,
        *,
        workspace: Path,
        state_dir: Path,
        config: ExecutionConfig,
        plugins: tuple[PluginSource, ...],
        revision_id: str,
        revision_payload: Mapping[str, object],
        auto_drain: bool = True,
        admission: Admission | None = None,
        admits: Callable[..., Admission] | None = None,
        sandbox_backend: SandboxBackend | None = None,
        process_host: ProcessHost | None = None,
        # Product-owned config filename (e.g. voidcode.json): the product layer
        # passes its own name in; the core never spells it, only guards it.
        config_filename: str = "voidcode.json",
    ) -> None:
        self.auto_drain = auto_drain
        self._config_filename = config_filename
        self.workspace = workspace.resolve(strict=True)
        self._abort = Cancellation()
        self._closed = False
        # One run lock per session: independent sessions (delegated children, cron firings, a
        # user turn beside a background run) execute concurrently in the same workspace. The
        # activation barrier does not depend on this lock; it uses the dispatch fence and the
        # in-transaction blocker check.
        self._session_locks: dict[str, Lock] = {}
        self._session_locks_guard = Lock()
        self._cancellations = CancellationCoordinator()
        # Background action drivers: claimed-but-unsettled action ids this process drives.
        # Cross-process double-driving is impossible: construction holds an exclusive
        # workspace flock (`another process owns this workspace`), so a claimed row this
        # process does not drive is necessarily stale, never a live foreign claim.
        self._background = BackgroundActionRegistry(self)
        self._background_execution = BackgroundExecution(self, self._background)
        # Commit clock for event streams: bumped under this condition after every
        # committed session-events write (see notify_commit); waiters re-read cursors.
        self._commit_cond = threading.Condition()
        self._commit_generation = 0
        # One pointer per capability generation, plus short-critical-section call accounting.
        # The activation fence rejects new dispatches and reports foreign in-flight calls as
        # busy instead of blocking any run, and no lock is ever held across an RPC.
        self._calls_lock = threading.Condition()
        self._active_calls: dict[str, str] = {}
        self._active_dispatch_threads: dict[int, int] = {}
        self._activating_thread: int | None = None
        # Set before the fence is attempted so long-lived drivers can release their slots;
        # cleared if the attempt is refused or after the switch commits.
        self._activation_requested = threading.Event()
        # ponytail: bounded in-process cache of per-revision observed_facts; a large
        # multi-revision workspace could grow it, then key it by revision only.
        self._observer_eligibility: dict[tuple[str, str], tuple[str, ...]] = {}
        self._state_dir = state_dir
        self._admits = admits
        # One host for this Core's lifetime: every runtime generation shares it, so background
        # jobs and their stable log paths survive reloads. Core.close() stops them. Materialize
        # the root now so sandbox protected-path setup always names an existing directory.
        self._process_host = process_host if process_host is not None else ProcessHost(spool_dir=self._process_spool_dir())
        _ = self._process_host.spool_dir
        self._delegation = DelegationManager(self)
        self._artifacts = ArtifactStore(state_dir / "artifacts")
        with ExitStack() as resources:
            # Lock the directory inode, independent of the selected database path.
            # Opening/locking it does not create or rewrite workspace files.
            lease = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY)
            resources.callback(os.close, lease)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another process owns this workspace") from exc
            self._lease_fd = lease
            state_dir.mkdir(parents=True, exist_ok=True)
            self.store = SqliteStore(state_dir / DATABASE_FILENAME)
            self._delegation.attach_store(self.store)
            self._activation = ActivationCoordinator(self)
            self._usage = UsageProjector(self.store)
            self._projection = ResultProjector(self._artifacts)
            self._context_projection = ContextProjector(self)
            self._command_runtime = CommandRuntime(self)
            self._fact_delivery = FactDelivery(self)
            self._model_query = ModelQueryRuntime(self)
            self._governance = ToolGovernance(self)
            self._interactive_ask = InteractiveAskManager(self)
            self._followup = FollowupManager(self)
            self._commit_pipeline = CommitPipeline(self)
            self._tool_execution = ToolExecutionManager(self)
            self._continuation_scheduler = ContinuationScheduler(self)
            self._turn_driver = TurnDriver(self)
            self._run_coordinator = RunCoordinator(self)
            resources.callback(self.store.close)
            runtime = self._build_runtime(plugins, revision_id=revision_id, sandbox_backend=sandbox_backend)
            # A runtime whose registry cannot be published is closed here: nothing may outlive a
            # failed construction, and the late-bound cleanup callback is registered only after
            # the generation that owns this runtime is complete.
            try:
                if admission is not None and admission.validate is not None:
                    admission.validate(runtime.providers)
                self.store.put_revision(revision_payload, revision_id=revision_id)
                if admission is not None and admission.revision_id != revision_id:
                    raise ValueError("admission does not describe the generation it was opened with")
                self._snapshot = _Snapshot.build(
                    revision_id=revision_id,
                    config=config,
                    admission=admission,
                    plugin_runtime=runtime,
                    with_reload=admits is not None,
                )
                self._assert_state_schema_compatible()
            except BaseException:
                runtime.close()
                raise
            # Late-bound so the callback closes whatever generation is current at shutdown.
            resources.callback(self._close_runtime)
            # Initialization work still owns the resources: a failure here unwinds the stack
            # (runtime, store, workspace lease) instead of leaking an already-detached core.
            self._background_started = False
            self._background_workers: list[threading.Thread] = []
            self._wakeup_workers: list[threading.Thread] = []
            self._wakeup_tick_lock = Lock()
            self._wakeup_tick_state: dict[str, object] = {"at_ms": None, "claimed": (), "skipped": (), "error": None}
            self._wakeup_tick_mode = threading.local()
            resources.callback(self._stop_background_work)
            # Startup holds the workspace lease, so a claimed wakeup run still marked
            # running was abandoned by the process that owned it. Settle it at its last
            # committed boundary before fact replay and before the ticker starts; never
            # replay a run-finished session.
            recovered = self.store.recover_claimed_wakeup_runs(
                str(self.workspace),
                revision_id=self.revision_id,
                fact_observers=self.plugin_runtime.observer_owners,
            )
            if recovered:
                recovered_summary = ", ".join(f"{sid}:{status}" for sid, status in recovered)
                logger.warning("startup recovered %d claimed wakeup run(s): %s", len(recovered), recovered_summary)
            # Deliver recovered run-finished facts (and every other unsettled fact) only after
            # recovery has appended its exactly-once interruption events.
            self._fact_delivery.replay_pending()
            if self.auto_drain:
                self._start_background_work()
            # Adopt the generation LAST: a failed cold start must leave the workspace unadopted, so
            # fixing the same authorized source and retrying works without deleting any database.
            self.store.record_workspace_revision(str(self.workspace), revision_id)
            self._resources = resources.pop_all()

    def _process_spool_dir(self) -> Path:
        """Stable runtime-owned spool ancestor for confined logs: state_dir/process-spool (0700)."""
        spool = self._state_dir / "process-spool"
        spool.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(spool, 0o700)
        except OSError:
            pass
        return spool

    def _assert_state_schema_compatible(self) -> None:
        """Refuse to reinterpret stored owner state under a different declared schema."""
        states = self._snapshot.plugin_runtime.state_metadata()
        scopes: list[tuple[StateScope, str]] = [("workspace", str(self.workspace))]
        scopes.extend(("session", record.session_id) for record in self.store.sessions(workspace=str(self.workspace)))
        for scope, scope_key in scopes:
            for row in self.store.plugin_state_rows(scope, scope_key):
                state = states.get(row.owner)
                if state is None or state.version == row.schema_version:
                    continue
                raise ValueError(f"plugin {row.owner} state schema mismatch: stored v{row.schema_version}, declared v{state.version}")

    def _start_background_work(self) -> None:
        """Start the continuation driver, the wakeup ticker, and the background loop, exactly once."""
        with self._session_locks_guard:
            if self._closed or self._background_started:
                return
            self._background_started = True
            self._background.wire(self._snapshot.plugin_runtime)
            for target, name in (
                (self._drive_pending_activations, "CoreActivationDriver"),
                (self._wakeup_loop, "CoreWakeupTicker"),
            ):
                worker = threading.Thread(target=target, name=name, daemon=True)
                worker.start()
                self._background_workers.append(worker)
            self._background_workers.append(self._background.start())

    def _wire_background_runtime(self, runtime: PluginRuntime) -> None:
        """Bind one runtime's background projections to this core's live driver registry."""
        self._background.wire(runtime)

    def _stop_background_work(self) -> None:
        """Join coordination workers before runtime/store callbacks close their resources."""
        self.cancel("core shutdown", cause="shutdown")
        self._background.park("core shutdown")
        with self._session_locks_guard:
            self._closed = True
            workers, self._background_workers = self._background_workers, []
            wakeup_workers, self._wakeup_workers = self._wakeup_workers, []
        self._background.notify()
        for worker in workers:
            worker.join()
        for worker in wakeup_workers:
            worker.join()

    @contextmanager
    def _session_lock(self, session_id: str) -> Generator[None]:
        """Serialize work of ONE session; different sessions never block each other."""
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = Lock()
                self._session_locks[session_id] = lock
        with lock:
            yield

    def _drain_session_locks(self) -> None:
        """Wait for every in-flight session run to finish; callers have already set `_closed`."""
        with self._session_locks_guard:
            locks = list(self._session_locks.values())
        for lock in locks:
            with lock:
                continue

    def _close_runtime(self) -> None:
        self._snapshot.plugin_runtime.close()

    @property
    def plugin_runtime(self) -> PluginRuntime:
        return self._snapshot.plugin_runtime

    @property
    def config(self) -> ExecutionConfig:
        return self._snapshot.config

    @property
    def admission(self) -> Admission | None:
        return self._snapshot.admission

    @property
    def revision_id(self) -> str:
        return self._snapshot.revision_id

    @property
    def policy_read_only(self) -> bool:
        return self.config.policy.read_only

    @property
    def approval_mode(self) -> str:
        return self.config.policy.approval_mode

    @property
    def commit_generation(self) -> int:
        """Current store-commit generation; event streams wait for it to advance."""
        with self._commit_cond:
            return self._commit_generation

    def notify_commit(self) -> None:
        """Record one committed session-events write and wake event-stream waiters.

        Call after every committed session-events write affecting sessions (all commit_staged /
        commit_activation / claim paths in engine and delegation). Level-triggered:
        waiters record a generation, wait for ANY change, then re-read their event
        cursor — no wakeup is ever lost (a check after a bump sees the new value),
        and spurious wakes or timeouts just re-read an unchanged cursor.
        """
        with self._commit_cond:
            self._commit_generation += 1
            self._commit_cond.notify_all()

    @property
    def observer_owners(self) -> Mapping[ObservedFactKind, tuple[str, ...]]:
        return self.plugin_runtime.observer_owners

    def notify_tool_waiters(self) -> None:
        self._background.notify_tool_waiters()

    def delivering_session(self, session_id: str) -> bool:
        return self._fact_delivery.delivering_session(session_id)

    def deliver_facts(
        self,
        execution: _Execution,
        events: Sequence[StoredEvent],
        *,
        tolerate_conflicts: bool = True,
    ) -> None:
        self._fact_delivery.deliver(execution, events, tolerate_conflicts=tolerate_conflicts)

    def wait_for_commit(self, after_generation: int, timeout: float) -> int:
        """Block until a commit lands past ``after_generation`` (or timeout); return current generation."""
        with self._commit_cond:
            self._commit_cond.wait_for(lambda: self._commit_generation != after_generation, timeout=timeout)
            return self._commit_generation

    @contextmanager
    def dispatch(self, description: str) -> Generator[_Snapshot]:
        """Capture the current generation and hold a call slot until the caller's result commits.

        The slot is what makes an activation safe: a switch is refused while any foreign
        dispatch is in flight, and refused-for-new once the fence is set, so no result can be
        committed under a generation other than the one that produced it.
        """
        call_id = uuid4().hex
        with self._calls_lock:
            if self._activating_thread is not None and self._activating_thread != threading.get_ident():
                raise ActivationFenced("capability activation is in progress; retry")
            snapshot = self._snapshot
            thread_id = threading.get_ident()
            self._active_calls[call_id] = description
            self._active_dispatch_threads[thread_id] = self._active_dispatch_threads.get(thread_id, 0) + 1
        try:
            yield snapshot
        finally:
            with self._calls_lock:
                self._active_calls.pop(call_id, None)
                dispatch_count = self._active_dispatch_threads.get(thread_id, 0)
                if dispatch_count <= 1:
                    self._active_dispatch_threads.pop(thread_id, None)
                else:
                    self._active_dispatch_threads[thread_id] = dispatch_count - 1
                self._calls_lock.notify_all()

    def assert_background_wait_unleased(self) -> None:
        """Refuse waits from any active plugin lease or Core dispatch on this thread."""
        thread_id = threading.get_ident()
        if PluginRuntime.current_thread_has_active_lease():
            raise RuntimeError("background tool wait is unavailable inside an active plugin callback")
        with self._calls_lock:
            if self._active_dispatch_threads.get(thread_id, 0) or self._activating_thread == thread_id:
                raise RuntimeError("background tool wait is unavailable inside an active Core dispatch")

    def __enter__(self) -> Core:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def cancel_session(
        self,
        session_id: str,
        reason: str = "user cancellation",
        *,
        cause: StopCause = "user_stop",
        target: CancelTarget = CURRENT,
    ) -> bool:
        """Signal the currently executing run and command invocations of one session."""
        return self._cancel_session_run(session_id, reason, cause=cause, target=target)

    def _cancel_session_run(self, session_id: str, reason: str, *, cause: StopCause, target: CancelTarget) -> bool:
        """Signal one session only when the live run matches ``target``.

        ``RUN_BOUND(None)`` is a REAL binding (a reserved, never-started run)
        and stays distinct from ``CURRENT``, which names only the session. The
        target's long-held session lock is never taken, so a blocked provider
        run stays signallable. Ordering is ownership, bound durable run, live
        target selection/signals, durable continuation cancel, delegation
        notify, then parked settle; a stale bound request touches no signal,
        including beside-running commands.
        """
        self.ensure_open()
        # Ownership first: a session id from another workspace sharing this
        # database may never be signalled or settled.
        self.session(session_id)
        if isinstance(target, RunBoundTarget):
            bound: str | None = target.run_id
            is_bound = True
            if (self.store.session(session_id).run_id or None) != bound:
                return False
        else:
            bound = None
            is_bound = False
        selection = self._cancellations.signal(session_id, target, reason=reason, cause=cause)
        if selection.blocked:
            return False
        self.store.cancel_pending_continuations(session_id, "core")
        self._delegation.notify_child_state_changed()
        # A bound call with no live abort for the bound run falls through to
        # the pinned parked settle; an unbound call settles whatever parked
        # work the session currently holds.
        settled = (
            self._settle_parked_cancel(session_id, reason, cause, expected_run_id=bound, is_bound=is_bound) if selection.abort is None else False
        )
        return bool(selection.signalled) or settled

    def _settle_parked_cancel(
        self, session_id: str, reason: str, cause: StopCause, *, expected_run_id: str | None = None, is_bound: bool = False
    ) -> bool:
        """Settle parked work (e.g. approval waits or interruptions) on cancellation."""
        session = self.session(session_id)
        if is_bound and (session.run_id or None) != expected_run_id:
            return False
        checkpoint = self.store.checkpoint(session_id)
        parked_run_id = self._parked_run_id(session, checkpoint)
        if parked_run_id is None:
            return False
        if is_bound and parked_run_id != expected_run_id:
            return False
        cursor = _Cursor()
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        execution = _Execution(session, cursor, list(self.store.events(session_id)), parked_run_id)
        parked_claim = (
            next((c for c in self.store.claimed_continuations(session_id, limit=100) if c.continuation_id == cursor.continuation_id), None)
            if cursor.continuation_id is not None
            else None
        )
        try:
            if cursor.batch_sequence is not None or cursor.deferred_sequence is not None:
                # The parked batch is never replayed: report its members as interrupted first.
                self._interrupt_batch(execution, pinned_run_id=expected_run_id if is_bound else None, enforce_pin=is_bound)
            if is_bound and execution.run_id != expected_run_id:
                return False
            lifecycle_events = self._governance.lifecycle_events(
                execution,
                self.snapshot,
                "session_end",
                status="interrupted",
                error=reason,
            )
            self._commit(
                execution,
                SessionStatus.INTERRUPTED,
                CheckpointKind.TERMINAL,
                EventDraft(
                    EventType.RUN_INTERRUPTED,
                    "core",
                    "core",
                    {
                        "reason": reason,
                        "stop_cause": cause,
                        "logical_turn": execution.cursor.logical_turn,
                        **self.measured_run_elapsed_fields(execution),
                    },
                ),
                *lifecycle_events,
                staged_cancellations=(
                    (StagedCancellation(owner=parked_claim.owner, continuation_id=parked_claim.continuation_id),) if parked_claim is not None else ()
                ),
                pinned_run_id=expected_run_id if is_bound else None,
                enforce_pin=is_bound,
            )
        except StoreConflict:
            # The parked session moved on under another writer; settling is moot.
            return False
        return True

    def _parked_run_id(self, session: SessionRecord, checkpoint: Checkpoint | None) -> str | None:
        """The run identity of parked work this session may safely settle, else None.

        Parked work is an approval wait or a recoverable interruption, and it must
        carry the session's own durable run identity with a non-terminal checkpoint
        of that same run. A terminal checkpoint (or a finished session) holds
        nothing to settle, but a session that still claims parked work without an
        authentic identity is a corrupt state and fails visibly here - before any
        write - instead of being reported as an ordinary no-work cancellation.
        """
        if session.status not in (SessionStatus.WAITING, SessionStatus.INTERRUPTED):
            return None
        if checkpoint is not None and checkpoint.kind is CheckpointKind.TERMINAL:
            return None
        run_id = session.run_id
        if not run_id or checkpoint is None or checkpoint.run_id != run_id:
            raise ValueError(f"session {session.session_id!r} reports parked {session.status.value} work without its authentic run identity")
        return run_id

    def admits_fresh_work(self, session: SessionRecord) -> bool:
        """Whether a session accepts a fresh user turn or auto-driven follow-up work.

        Idle, finished, and failed sessions are open, and so is a terminal
        interruption of the session's own run. Anything else still holds
        unfinished native work that only an explicit resume may re-enter.
        """
        if session.status in {SessionStatus.IDLE, SessionStatus.COMPLETED, SessionStatus.FAILED}:
            return True
        checkpoint = self.store.checkpoint(session.session_id)
        return (
            session.status is SessionStatus.INTERRUPTED
            and checkpoint is not None
            and checkpoint.kind is CheckpointKind.TERMINAL
            and checkpoint.run_id == session.run_id
        )

    def cancel(self, reason: str = "user cancellation", *, cause: StopCause = "user_stop") -> None:
        self._abort.set_cancelled(True, reason=reason, stop_cause=cause)
        for session_id in self._cancellations.session_ids():
            self.store.cancel_pending_continuations(session_id, "core")
        self._cancellations.signal_all(reason=reason, cause=cause)
        self._delegation.notify_child_state_changed()

    def close(self) -> None:
        self.cancel("core shutdown", cause="shutdown")
        with self._session_locks_guard:
            if self._closed:
                return
            self._closed = True
        self._background.signal_stop("core shutdown")
        self._delegation.close()
        # No new session work can start (run/resume check `_closed` under their lock), so
        # draining every session lock now waits only for work that was already admitted.
        self._drain_session_locks()
        self._background.quiesce("core shutdown")
        # The core's own shutdown is an owner shutdown: every background job, including ones
        # started by an earlier generation, ends here. A reload is not an owner shutdown and
        # never calls this; the shared host keeps those jobs alive.
        self._process_host.stop_background()
        self._resources.close()

    def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        """Read immutable workspace artifact bytes, never a caller-supplied path."""
        self.ensure_open()
        return self._projection.read(sha256, offset=offset, limit=limit)

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        max_steps: int | None = None,
        root_action_budget: int | None = None,
        images: tuple[ImageAttachment, ...] = (),
        interaction: Literal["interactive", "unattended"] | None = None,
    ) -> CoreResult:
        return self._run_coordinator.run(
            prompt,
            session_id=session_id,
            max_steps=max_steps,
            root_action_budget=root_action_budget,
            images=images,
            interaction=interaction,
        )

    def _run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        max_steps: int | None = None,
        root_action_budget: int | None = None,
        prompt_max_bytes: int,
        images: tuple[ImageAttachment, ...] = (),
        _root_id: str | None = None,
        _refuse_reserved_cancelled: bool = False,
        interaction: Literal["interactive", "unattended"] | None = None,
    ) -> CoreResult:
        return self._run_coordinator._run(
            prompt,
            session_id=session_id,
            max_steps=max_steps,
            root_action_budget=root_action_budget,
            prompt_max_bytes=prompt_max_bytes,
            images=images,
            _root_id=_root_id,
            _refuse_reserved_cancelled=_refuse_reserved_cancelled,
            interaction=interaction,
        )

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        decision: Literal["allow", "deny"] | None = None,
        max_steps: int | None = None,
        extend_root_actions: int | None = None,
        root_action_grant_id: str | None = None,
        interaction: Literal["interactive", "unattended"] | None = None,
        reason: str | None = None,
        grant_session_policy: bool = False,
    ) -> CoreResult:
        return self._run_coordinator.resume(
            session_id,
            approval_request_id=approval_request_id,
            decision=decision,
            max_steps=max_steps,
            extend_root_actions=extend_root_actions,
            root_action_grant_id=root_action_grant_id,
            interaction=interaction,
            reason=reason,
            grant_session_policy=grant_session_policy,
        )

    def answer_ask(
        self,
        session_id: str,
        request_id: str,
        values: Mapping[str, object],
        *,
        expected_revision: str,
    ) -> CoreResult:
        """Adopt one client's answer for the session's live ask and continue the run."""
        return self._interactive_ask.answer_ask(session_id, request_id, values, expected_revision=expected_revision)

    def cancel_ask(self, session_id: str, request_id: str, *, expected_revision: str) -> CoreResult:
        """Adopt one client's cancellation for the session's live ask and continue the run."""
        return self._interactive_ask.cancel_ask(session_id, request_id, expected_revision=expected_revision)

    def sweep_expired_asks(self) -> None:
        """Settle caller-declared ask expiries due now; one bad session never stops the sweep."""
        self._interactive_ask.sweep_expired_asks()

    def drain_deferred_requests(
        self,
        session_id: str,
        *,
        background_action_id: str | None = None,
        action_abort: threading.Event | None = None,
    ) -> None:
        """Run committed deferred requests after their caller's callback lease has returned."""
        with self._session_lock(session_id):
            while True:
                self.ensure_open()
                session = self.session(session_id)
                if not self.admits_fresh_work(session):
                    return
                events = list(self.store.events(session_id))
                completed_sequences = {
                    event.payload.get("batch_sequence")
                    for event in events
                    if event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED} and event.payload.get("deferred") is True
                }
                terminal_request_ids = {event.payload.get("request_id") for event in events if event.event_type == EventType.TOOL_DEFERRED_CANCELLED}
                request = next(
                    (
                        event
                        for event in events
                        if event.event_type == EventType.TOOL_DEFERRED
                        and event.sequence not in completed_sequences
                        and event.payload.get("request_id") not in terminal_request_ids
                        and (
                            event.payload.get("background_action_id") == background_action_id
                            if background_action_id is not None
                            else event.payload.get("background_action_id") is None
                        )
                    ),
                    None,
                )
                if request is None:
                    return
                raw_action_id = request.payload.get("background_action_id")
                request_action_id = raw_action_id if isinstance(raw_action_id, str) else None
                if action_abort is not None and action_abort.is_set():
                    if request_action_id is not None:
                        action_stop_cause = self._background.abort_cause(request_action_id)
                        status = "cancelled" if action_stop_cause == "owner_cancel" else "interrupted"
                        self.cancel_background_tool_requests(
                            session_id,
                            request_action_id,
                            status=status,
                            stop_cause=action_stop_cause,
                            reason="background action stopped before deferred dispatch",
                        )
                    return
                raw_root = request.payload.get("root_id")
                root_id = raw_root if isinstance(raw_root, str) and raw_root else ("" if request_action_id else uuid4().hex)
                raw_budget = request.payload.get("root_action_budget")
                root_budget = raw_budget if type(raw_budget) is int and raw_budget >= 0 else None
                raw_tools = request.payload.get("origin_visible_tools")
                origin_tools = (
                    tuple(raw_tools) if isinstance(raw_tools, (list, tuple)) and all(isinstance(name, str) and name for name in raw_tools) else None
                )
                raw_readonly = request.payload.get("origin_readonly")
                origin_readonly = raw_readonly if type(raw_readonly) is bool else None
                authority_missing = request_action_id is not None and (
                    not root_id or "root_action_budget" not in request.payload or origin_tools is None or origin_readonly is None
                )
                execution = _Execution(
                    session,
                    _Cursor(
                        root_id=root_id,
                        initial_root_budget=root_budget,
                        deferred_only=True,
                        deferred_action_id=request_action_id,
                        deferred_origin_tools=origin_tools,
                        deferred_origin_readonly=origin_readonly,
                        deferred_authority_missing=authority_missing,
                        logical_turn=uuid4().hex,
                    ),
                    events,
                    uuid4().hex,
                )
                if action_abort is None:
                    abort = Cancellation()
                else:
                    abort = Cancellation(
                        action_abort,
                        linked_reason="background action stopped",
                        linked_stop_cause_provider=lambda action_id=request_action_id or "": self._background.stop_cause(action_id),
                    )
                self._cancellations.register_run(session_id, execution.run_id, abort)
                try:
                    self._commit_pipeline.commit(execution, SessionStatus.RUNNING, CheckpointKind.READY)
                    result = self._turn_driver.drive(execution)
                finally:
                    self._cancellations.unregister_run(session_id)
                if result.status not in ("completed", "idle"):
                    return

    def command_catalog(self) -> CommandCatalog:
        """Command catalog captured together with the revision that admits it.

        Descriptor list and revision come from ONE immutable snapshot, so a client can
        bind a later action to exactly the generation it displayed.
        """
        self.ensure_open()
        snapshot = self._snapshot
        return CommandCatalog(revision_id=snapshot.revision_id, commands=snapshot.commands)

    def command_status(self, session: SessionRecord) -> CommandResult:
        """Reserved status command: session truth, usage, budgets, and event counts."""
        data = self.show(session.session_id, events=False)
        counts = self.store.event_type_counts(session.session_id)
        usage = self.usage(session.session_id)
        budgets = self.store.session_budgets(session.session_id)
        data["event_counts"] = counts
        data["usage"] = usage.as_payload()
        data["budgets"] = [budget.as_payload() for budget in budgets]
        model = self.config.model
        lines = [
            f"session: {session.session_id}",
            f"status: {session.status.value}",
            f"workspace: {self.workspace}",
            f"revision: {session.active_revision_id}",
            f"model: {model}" if model is not None else "model: not configured",
            f"run: {session.run_id or 'none'}",
            f"turn: {session.turn}, last event: {session.last_event_sequence}",
            f"turns: {usage.turn_count}, input tokens: {usage.input_tokens}, output tokens: {usage.output_tokens}, cost: {usage.total_cost_usd}",
        ]
        lines.extend("budget: " + ", ".join(f"{key}={value}" for key, value in budget.as_payload().items()) for budget in budgets)
        lines.extend(f"{event_type}: {count}" for event_type, count in sorted(counts.items()))
        return CommandResult(status="ok", output="\n".join(lines), data=data)

    def command_help(self) -> CommandResult:
        """Reserved help command: the attached revision's command catalog with usage."""
        catalog = self.command_catalog()
        lines = ["Available commands:"]
        for group, entries in grouped_help_entries(catalog.commands):
            lines.append("")
            lines.append(f"{group}:")
            for entry in entries:
                lines.append(f"  {entry.usage} — {entry.description}")
                lines.extend(f"    e.g. {example}" for example in entry.examples)
        return CommandResult(status="ok", output="\n".join(lines), data=catalog.as_payload())

    def owner_cancel_claimed_work(
        self,
        session_id: str,
        cancellations: Sequence[StagedCancellation],
        reason: str,
    ) -> None:
        """Settle work whose own claimed continuation an owner just cancelled."""
        self._continuation_scheduler.settle_cancellations(session_id, cancellations, reason=reason)

    def create_session(self, session_id: str | None = None) -> SessionRecord:
        """Create a new durable session in idle state bound to workspace and active revision."""
        self.ensure_open()
        sid = uuid4().hex if session_id is None else session_id
        return self._create_session_row(sid)

    def _create_session_row(self, session_id: str) -> SessionRecord:
        """Create a session on the workspace's recorded revision, retrying if it moves first."""
        for _attempt in range(3):
            recorded = self.store.workspace_current(str(self.workspace)) or self.revision_id
            try:
                return self.store.create_session(session_id, str(self.workspace), recorded)
            except StoreConflict:
                continue
        raise StoreConflict(f"could not create session {session_id!r}: the workspace revision kept moving")

    def session(self, session_id: str) -> SessionRecord:
        """Fetch session metadata by ID, verifying workspace."""
        self.ensure_open()
        s = self.store.session(session_id)
        if s.workspace != str(self.workspace):
            raise ValueError("session belongs to another workspace")
        return s

    def sessions(self, *, limit: int = 100) -> tuple[SessionRecord, ...]:
        """Fetch sessions for this workspace, ordered by updated_at descending, up to limit."""
        self.ensure_open()
        return self.store.sessions(workspace=str(self.workspace), limit=limit)

    def show(self, session_id: str, *, events: bool = False) -> dict[str, object]:
        """Return session metadata, active checkpoint, and optional events as JSON-serializable dict."""
        self.ensure_open()
        s = self.session(session_id)
        checkpoint = self.store.checkpoint(session_id)
        payload: dict[str, object] = {
            "session_id": s.session_id,
            "status": s.status.value,
            "run_id": s.run_id,
            "revision_id": s.active_revision_id,
            "turn": s.turn,
            "last_event_sequence": s.last_event_sequence,
            "checkpoint": None if checkpoint is None else {"kind": checkpoint.kind.value, "payload": json_wire_object(checkpoint.payload)},
        }
        if events:
            payload["events"] = [
                {
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "run_id": event.run_id,
                    "revision_id": event.revision_id,
                    "source": event.source,
                    "actor": event.actor,
                    "payload": json_wire_object(event.payload),
                }
                for event in self.store.events(session_id)
            ]
        return payload

    def events_after(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> tuple[StoredEvent, ...]:
        """Bounded tail read of stored events for continuous SSE streaming."""
        self.ensure_open()
        self.session(session_id)
        return self.store.events_after(session_id, after_sequence=after_sequence, limit=limit)

    def transcript_page(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        """One bounded transcript page; advance to next_sequence while has_more, even when empty."""
        self.ensure_open()
        self.session(session_id)
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in 1..1000")
        return self.plugin_runtime.transcript_page(session_id, after_sequence, limit)

    def context_history(self, session_id: str, *, limit: int = 100) -> ContextHistory:
        """One bounded context prefix from the session's first record."""
        self.ensure_open()
        self.session(session_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in 1..1000")
        return self.plugin_runtime.context_history(session_id, limit)

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        """Calculate truthful token usage metrics strictly from immutable core facts."""
        self.ensure_open()
        s = self.session(session_id)
        return self._get_session_usage(session_id, since_sequence, until_sequence, session=s)

    def _get_session_usage(
        self,
        session_id: str,
        since_sequence: int = 0,
        until_sequence: int | None = None,
        *,
        session: SessionRecord | None = None,
    ) -> SessionUsageReport:
        resolved = self.session(session_id) if session is None else session
        return self._usage.report(resolved, since_sequence=since_sequence, until_sequence=until_sequence)

    def _get_owner_budget(self, session_id: str, owner: str) -> PluginBudget | None:
        return self.store.owner_budget(session_id, owner)

    @contextmanager
    def command_abort(self, context: ToolContext) -> Generator[ToolContext]:
        # Commands may run alongside a turn and other commands: each owns its signal.
        self.ensure_open()
        abort = Cancellation()
        self._cancellations.register_command(context.session_id, context.invocation_id, abort)
        try:
            yield replace(context, abort_signal=abort)
        finally:
            self._cancellations.unregister_command(context.session_id, context.invocation_id)

    def command_input(
        self, session_id: str, name: str, *, expected_revision: str, arguments: Mapping[str, object] | None = None
    ) -> CommandInputSnapshot:
        """Render optional plugin-owned input; never execute the command to discover values."""
        self.ensure_open()
        session = self.session(session_id)
        with self.dispatch("command_input") as snapshot:
            if expected_revision != snapshot.revision_id:
                raise StaleRevision("displayed catalog revision is stale; refresh the catalog", expected_revision, snapshot.revision_id)
            context = ToolContext(
                workspace=self.workspace,
                session_id=session_id,
                run_id=session.run_id or "",
                invocation_id=uuid4().hex,
                revision_id=snapshot.revision_id,
                actor="user",
            )
            presentation = snapshot.plugin_runtime.prepare_command_input(name, context=context, arguments=arguments)
            return CommandInputSnapshot(revision_id=snapshot.revision_id, presentation=presentation)

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object],
        *,
        expected_revision: str,
    ) -> CommandResult:
        """Capture one command plan and delegate its governed invocation to the command runtime.

        This entrypoint keeps catalog resolution, session truth, invocation identity, and the
        reserved reload command; the runtime owns policy checks, dispatch, confirmation staging,
        and the atomic public commit. `expected_revision` is required and must be the revision
        the client displayed: a command advertised by one generation is never silently executed
        by another.
        """
        self.ensure_open()
        self._check_expected_revision(expected_revision)
        session = self.session(session_id)

        # Core reserved commands dispatch through the advertised typed contract.
        if name == RELOAD_COMMAND_NAME:
            return self._invoke_reload_command(session_id, arguments)

        descriptor = self._snapshot.command_of(name)
        if descriptor is None:
            raise ValueError(f"Unknown command: {name}")
        read_only = self.session_read_only(session_id)
        core_command = descriptor.owner == "core"
        root_action_id: str | None = None
        initial_root_budget: int | None = None
        origin_visible_tools: tuple[str, ...] | None = None
        if not core_command:
            root_action_id, initial_root_budget = self.capture_root_authorization(session)
            origin_visible_tools = self.visible_tool_names_for_session(session_id, self._snapshot)
        plan = CommandPlan(
            descriptor=descriptor,
            arguments=arguments,
            expected_revision=expected_revision,
            effects=frozenset(descriptor.effects),
            read_only=read_only,
            session=session,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
            context=ToolContext(
                workspace=self.workspace,
                session_id=session_id,
                run_id=session.run_id or uuid4().hex,
                invocation_id=uuid4().hex,
                revision_id=self.revision_id,
                actor="client",
                logical_turn=uuid4().hex,
                root_action_id=root_action_id,
                initial_root_budget=initial_root_budget,
                origin_visible_tools=origin_visible_tools,
                origin_readonly=None if core_command else read_only,
            ),
        )
        return self._command_runtime.invoke(plan)

    def confirm_action(
        self,
        session_id: str,
        request_id: str,
        decision: Literal["allow", "deny"],
        *,
        expected_revision: str,
    ) -> CommandResult:
        """Resolve a pending confirmation request by exact ID and state version."""
        self.ensure_open()
        self._check_expected_revision(expected_revision)
        return self._command_runtime.confirm(
            self.session(session_id),
            request_id,
            decision,
            expected_revision=expected_revision,
        )

    def plugin_views(
        self,
        session_id: str,
    ) -> ViewsSnapshot:
        """Render declarative views contributed by admitted plugins for the session.

        The whole set renders under ONE captured generation and carries that revision,
        so a late view from an older revision is never relabelled with the new one.
        """
        self.ensure_open()
        session = self.session(session_id)
        with self.dispatch("views") as snapshot:
            views: list[PluginViewDescriptor] = []
            for view_name in snapshot.plugin_runtime.views:
                try:
                    v = snapshot.plugin_runtime.render_view(session_id, view_name, run_id=session.run_id or "")
                    views.append(v)
                except Exception as exc:
                    owner = snapshot.plugin_runtime.view_owner(view_name)
                    views.append(
                        PluginViewDescriptor(
                            owner=owner,
                            name=view_name,
                            title=f"{view_name} (error)",
                            view_type="notice",
                            data={"message": f"View {view_name} failed: {type(exc).__name__}", "level": "error"},
                        )
                    )
            return ViewsSnapshot(revision_id=snapshot.revision_id, views=tuple(views))

    def drain_continuations(
        self,
        session_id: str,
        *,
        limit: int = 10,
        max_steps: int | None = None,
        continuation_id: str | None = None,
        _automatic: bool = False,
    ) -> tuple[ContinuationResult, ...]:
        """Claim and execute pending continuations as governed runs with plugin provenance."""
        return self._continuation_scheduler.drain(
            session_id,
            limit=limit,
            max_steps=max_steps,
            continuation_id=continuation_id,
            automatic=_automatic,
        )

    def ensure_open(self) -> None:
        """Refuse work on a closed core; also the explicit host-protocol open check."""
        if self._closed:
            raise RuntimeError("engine is closed")

    def check_session(self, session: SessionRecord) -> None:
        if session.workspace != str(self.workspace) or session.active_revision_id != self.revision_id:
            raise ValueError("session workspace or revision mismatch")

    def _check_expected_revision(self, expected_revision: str) -> None:
        """Bind a client action to the catalog generation it displayed."""
        if not isinstance(expected_revision, str) or not expected_revision.strip():
            raise InvalidRevision("expected_revision must be the revision the client displayed", expected_revision)
        if expected_revision != self.revision_id:
            raise StaleRevision(f"displayed catalog revision {expected_revision} is stale; refresh the catalog", expected_revision, self.revision_id)

    def _invoke_reload_command(self, session_id: str, arguments: Mapping[str, object]) -> CommandResult:
        """Reserved core command: activate the configured sources on explicit user request."""
        validator = jsonschema.Draft202012Validator(json_wire_object(_RELOAD_COMMAND_SPEC[2]))
        try:
            validator.validate(json_wire_object(arguments))
        except jsonschema.ValidationError as err:
            return CommandResult(status="error", error=f"Command arguments failed schema: {err.message}")
        if self._admits is None:
            return CommandResult(status="error", error="capability activation is unavailable")
        if self.config.policy.read_only:
            return CommandResult(status="error", error="read-only policy denies capability activation")
        session = self.session(session_id)
        if session.status not in {
            SessionStatus.IDLE,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.INTERRUPTED,
        }:
            return CommandResult(
                status="error",
                error=f"session {session_id} is busy ({session.status.value}); activation requires an idle session",
            )
        outcome = self.activate(session_id=session_id, source="client")
        if not outcome["activated"]:
            return CommandResult(status="error", error=str(outcome["error"]))
        return CommandResult(status="ok", data={key: value for key, value in outcome.items() if key != "error"})

    def activate(self, *, session_id: str, source: str, request_id: str = "", target_revision_id: str = "") -> dict[str, object]:
        """Run one capability activation for an already-safe boundary; refusals stay durable outcomes."""
        return self._activation.activate(
            session_id=session_id,
            source=source,
            request_id=request_id,
            target_revision_id=target_revision_id,
        )

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def config_filename(self) -> str:
        return self._config_filename

    @property
    def admits(self) -> Callable[..., Admission] | None:
        return self._admits

    @property
    def snapshot(self) -> _Snapshot:
        return self._snapshot

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def activation_requested(self) -> threading.Event:
        """Set while an activation is trying to drain cooperative drivers before its fence."""
        return self._activation_requested

    def begin_activation(self) -> None:
        """Enter the dispatch fence after bounded cooperation from long-lived drivers.

        Short dispatches (model turns, tools, commands) are waited out for the drain
        window so a switch is not refused by a momentarily busy callback; cooperative
        background drivers observe `activation_requested` and stop starting dispatches.
        A driver that never yields still refuses the activation after the window.
        """
        thread_id = threading.get_ident()
        deadline = time.monotonic() + _ACTIVATION_DRAIN_SECONDS
        while True:
            with self._calls_lock:
                if self._activating_thread is not None:
                    raise ActivationFenced("another capability activation is already in progress")
                if not self._active_calls:
                    self._activating_thread = thread_id
                    return
                busy = ", ".join(sorted(self._active_calls.values()))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ActivationFenced(f"capability activation is blocked by in-flight work: {busy}")
                self._calls_lock.wait(timeout=min(remaining, 0.05))

    def end_activation(self) -> None:
        self._clear_fence()

    def park_background_drivers(self, reason: str) -> None:
        self._background.park(reason)

    def flush_old_generation_facts(self, deferred_to_candidate: frozenset[str]) -> None:
        self._fact_delivery.replay_pending(require_complete=True, defer_to_candidate=deferred_to_candidate)

    def _build_runtime(self, sources: tuple[PluginSource, ...], *, revision_id: str, sandbox_backend: SandboxBackend | None) -> PluginRuntime:
        """Assemble one runtime generation with every host callback bound to this core."""
        return PluginRuntime(
            sources,
            workspace=self.workspace,
            revision_id=revision_id,
            store=self.store,
            usage_callback=self._get_session_usage,
            budget_callback=self._get_owner_budget,
            spawn_child_callback=self._spawn_child_callback,
            find_child_callback=self._delegation.find_keyed_child,
            join_child_callback=lambda parent, child, timeout: self.join_child(parent, child, timeout_seconds=timeout),
            wait_children_callback=lambda _owner, parent, children, guard: self._delegation.wait_children(parent, children, admission_guard=guard),
            acknowledge_child_completion_callback=lambda _owner, parent, child, run: self._delegation.acknowledge_child_completion(
                parent, child, run
            ),
            send_child_callback=lambda parent, child, prompt, label, notify, guard: self.send_child(
                parent,
                child,
                prompt,
                label=label,
                notify_on_completion=notify,
                admission_guard=guard,
            ),
            cancel_child_callback=self.cancel_child,
            list_descendants_callback=self.list_descendants,
            read_child_snapshot_callback=self._delegation.read_child_snapshot,
            cancel_subtree_callback=lambda bound, target, run, reason: self.cancel_descendant_subtree(
                bound, target, expected_run_id=run, reason=reason
            ),
            cancel_background_action_callback=self.cancel_background_action,
            model_query_callback=self.governed_model_query,
            model_query_structured_callback=self.governed_model_query_structured,
            progress_callback=self._report_progress,
            tool_definition_callback=self._governance.visible_tool_definition,
            session_read_only_callback=self.session_read_only,
            artifacts_dir=self._state_dir / "artifacts",
            sandbox_backend=sandbox_backend,
            process_host=self._process_host,
        )

    def build_candidate_runtime(self, admission: Admission, target_revision_id: str) -> PluginRuntime:
        """Assemble one candidate generation with every host callback bound to this core."""
        return self._build_runtime(
            admission.plugins,
            revision_id=target_revision_id,
            sandbox_backend=self._activation.candidate_sandbox_backend(admission),
        )

    def publish_activation(self, snapshot: ActivationSnapshot, prepared: PreparedActivation) -> None:
        """Swap the generation pointer, release the fence, and retire the old runtime."""
        admission = prepared.admission
        with self._calls_lock:
            self._snapshot = _Snapshot.build(
                revision_id=admission.revision_id,
                config=admission.config,
                admission=admission,
                plugin_runtime=prepared.plugin_runtime,
                with_reload=self._admits is not None,
            )
            self._activating_thread = None
            self._calls_lock.notify_all()
        # The revision is durable and published: nothing below may escape, and no
        # failure may leave `_snapshot` pointing at a runtime this method closes.
        try:
            self._wire_background_runtime(prepared.plugin_runtime)
            self._background.notify()
        except Exception:
            logger.exception("activation %s: background runtime wiring failed after publication", admission.revision_id)
        # Deliver facts deferred to owners the candidate now registers, under the new runtime.
        try:
            self._fact_delivery.replay_pending()
        except Exception:
            logger.exception("activation %s: deferred fact replay failed after publication", admission.revision_id)
        try:
            snapshot.plugin_runtime.close()
        except Exception:
            logger.exception("activation %s: old runtime close failed after publication", admission.revision_id)

    def activation_checkpoint(self, payload: Mapping[str, object] | None, /, *, source: str, request_id: str) -> Mapping[str, object]:
        """Carry the session's own cursor across the switch, plus a real resume intent where owed."""
        cursor = _Cursor()
        if payload is not None:
            cursor = _Cursor.model_validate(json_wire_object(payload))
        if source == "model":
            cursor.resume_intent = request_id
        return json_wire_object(cursor.model_dump())

    def _bound_target(self, execution: _Execution, call: ToolCall) -> str | None:
        """The pinned candidate an approval was bound to, if one exists for this exact native call."""
        encoded = encode_tool_call(call)
        target: str | None = None
        for event in execution.events:
            if event.event_type != EventType.APPROVAL_REQUESTED:
                continue
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            bound = payload.get("target_revision_id")
            bound_call = payload.get("call")
            if isinstance(bound, str) and isinstance(bound_call, Mapping) and json_wire_object(bound_call) == encoded:
                target = bound
        return target

    def _pending_activation_request(self, execution: _Execution) -> Mapping[str, object] | None:
        """The newest staged activation request of this run that no answer has consumed yet."""
        request: Mapping[str, object] | None = None
        answered: set[str] = set()
        for event in execution.events:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                continue
            if event.event_type == EventType.ACTIVATION_REQUESTED:
                request = payload
            elif event.event_type in {EventType.ACTIVATION_COMMITTED, EventType.ACTIVATION_FAILED}:
                answered.add(request_id)
        if request is None:
            return None
        return None if str(request.get("request_id", "")) in answered else request

    def _clear_fence(self) -> None:
        with self._calls_lock:
            self._activating_thread = None
            self._calls_lock.notify_all()

    def record_activation_refusal(
        self,
        session_id: str,
        request_id: str,
        source: str,
        error: str,
        blockers: Sequence[str],
    ) -> None:
        """Record why an activation did not happen, on the acting session, without consuming the request."""
        session = self.store.session(session_id)
        cursor = _Cursor()
        checkpoint = self.store.checkpoint(session_id)
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        execution = _Execution(session, cursor, list(self.store.events(session_id)), session.run_id or request_id)
        try:
            self._commit_pipeline.commit(
                execution,
                session.status,
                checkpoint.kind if checkpoint is not None else CheckpointKind.READY,
                EventDraft(
                    EventType.ACTIVATION_FAILED,
                    "core",
                    "core",
                    {
                        "request_id": request_id,
                        "source": source,
                        "error": error,
                        "blockers": list(blockers),
                    },
                ),
            )
        except StoreConflict, ValueError:
            # The refusal is a diagnostic, never a reason to lose the durable request.
            pass

    def _spawn_child_callback(self, request: ChildSpawnRequest) -> str:
        return self.spawn_child(
            request.parent_session_id,
            request.prompt,
            label=request.label,
            tool_allowlist=request.tool_allowlist,
            max_steps=request.max_steps,
            readonly=request.readonly,
            notify_on_completion=request.notify_on_completion,
            owner=request.owner,
            idempotency_key=request.idempotency_key,
            root_action_id=request.root_action_id,
            initial_root_budget=request.initial_root_budget,
            admission_guard=request.admission_guard,
            result_schema=request.result_schema,
        )

    def birth_record(self, session_id: str) -> BirthAuthorization | None:
        """Decode one child's durable birth into its captured root authorization."""
        birth = self._delegation._birth_record(session_id)
        if birth is None or "root_action_budget" not in birth:
            return None
        root_id = birth.get("root_id")
        budget = birth.get("root_action_budget")
        if not isinstance(root_id, str) or not root_id or "\0" in root_id:
            return None
        if budget is not None and (type(budget) is not int or budget < 0):
            return None
        return BirthAuthorization(root_id=root_id, root_action_budget=budget)

    def capture_root_authorization(self, session: SessionRecord) -> tuple[str, int | None]:
        """Capture the current invocation's root, or inherit an immutable child birth."""
        if session.parent_session_id is not None:
            birth = self._delegation._birth_record(session.session_id)
            if birth is None or "root_action_budget" not in birth:
                raise StoreConflict(f"child session {session.session_id!r} has no captured root authorization")
            root_id = birth.get("root_id")
            root_budget = birth.get("root_action_budget")
        elif session.status in (SessionStatus.RUNNING, SessionStatus.WAITING):
            checkpoint = self.store.checkpoint(session.session_id)
            payload = checkpoint.payload if checkpoint is not None and isinstance(checkpoint.payload, Mapping) else {}
            if "initial_root_budget" not in payload:
                raise StoreConflict(f"active session {session.session_id!r} has no captured root budget")
            root_id = payload.get("root_id")
            root_budget = payload.get("initial_root_budget")
        else:
            return uuid4().hex, None
        if not isinstance(root_id, str) or not root_id or "\0" in root_id:
            raise StoreConflict(f"session {session.session_id!r} has no valid captured root action identity")
        if root_budget is not None and (type(root_budget) is not int or root_budget < 0):
            raise StoreConflict(f"session {session.session_id!r} has an invalid captured root budget")
        return root_id, root_budget

    def spawn_child(
        self,
        parent_session_id: str,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        readonly: bool = False,
        notify_on_completion: bool = False,
        owner: str = "",
        idempotency_key: str | None = None,
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
        admission_guard: Callable[[], bool] | None = None,
        result_schema: Mapping[str, object] | None = None,
    ) -> str:
        if root_action_id is None:
            if initial_root_budget is not None:
                raise ValueError("a finite child budget requires its captured root action identity")
            if idempotency_key is not None:
                retry_id = self._delegation._keyed_child_id(parent_session_id, owner, idempotency_key)
                retry_birth = self._delegation._birth_record(retry_id)
                if retry_birth is not None:
                    if "root_action_budget" not in retry_birth:
                        raise StoreConflict(f"keyed child {retry_id!r} has no captured root budget")
                    candidate_root_id = retry_birth.get("root_id")
                    if not isinstance(candidate_root_id, str) or not candidate_root_id:
                        raise StoreConflict(f"keyed child {retry_id!r} has no captured root action identity")
                    root_action_id = candidate_root_id
                    candidate_budget = retry_birth.get("root_action_budget")
                    if candidate_budget is None:
                        initial_root_budget = None
                    elif type(candidate_budget) is int and candidate_budget >= 0:
                        initial_root_budget = candidate_budget
                    else:
                        raise StoreConflict(f"keyed child {retry_id!r} has an invalid captured root budget")
            if root_action_id is None:
                parent_sess = self.store.session(parent_session_id)
                if (
                    idempotency_key is not None
                    and parent_sess.parent_session_id is None
                    and parent_sess.status
                    not in (
                        SessionStatus.RUNNING,
                        SessionStatus.WAITING,
                    )
                ):
                    retry_id = self._delegation._keyed_child_id(parent_session_id, owner, idempotency_key)
                    root_action_id = hashlib.sha256(f"root:{retry_id}".encode()).hexdigest()
                    initial_root_budget = None
                else:
                    root_action_id, initial_root_budget = self.capture_root_authorization(parent_sess)
        return self._delegation.spawn_child(
            parent_session_id,
            prompt,
            label=label,
            tool_allowlist=tool_allowlist,
            max_steps=max_steps,
            readonly=readonly,
            notify_on_completion=notify_on_completion,
            owner=owner,
            idempotency_key=idempotency_key,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
            admission_guard=admission_guard,
            result_schema=result_schema,
        )

    def find_keyed_child(self, parent_session_id: str, owner: str, idempotency_key: str) -> str | None:
        """The child one parent spawned under one owner key; a read-only mapping lookup."""
        return self._delegation.find_keyed_child(parent_session_id, owner, idempotency_key)

    def join_child(self, parent_session_id: str, child_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        return self._delegation.join_child(parent_session_id, child_id, timeout_seconds=timeout_seconds)

    def send_child(
        self,
        parent_session_id: str,
        child_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
        admission_guard: Callable[[], bool] | None = None,
    ) -> ChildSendReceipt:
        return self._delegation.send_child(
            parent_session_id,
            child_id,
            prompt,
            label=label,
            notify_on_completion=notify_on_completion,
            admission_guard=admission_guard,
        )

    def cancel_child(self, parent_session_id: str, child_id: str, reason: str = "owner cancellation") -> bool:
        return self._delegation.cancel_child(parent_session_id, child_id, reason=reason)

    def list_descendants(self, session_id: str) -> tuple[DescendantNode, ...]:
        return self._delegation.list_descendants(session_id)

    def cancel_descendant_subtree(
        self, bound_session_id: str, target_session_id: str, *, expected_run_id: str | None, reason: str = "owner cancellation"
    ) -> CancelSubtreeReceipt:
        return self._delegation.cancel_descendant_subtree(bound_session_id, target_session_id, expected_run_id=expected_run_id, reason=reason)

    def wakeup_tick_status(self) -> dict[str, object]:
        """Last wakeup tick's bounded claim/skip/error report (never a delivery log)."""
        with self._wakeup_tick_lock:
            state = dict(self._wakeup_tick_state)
        claimed_val = state.get("claimed")
        skipped_val = state.get("skipped")
        return {
            "at_ms": state.get("at_ms"),
            "claimed": tuple(claimed_val) if isinstance(claimed_val, Sequence) else (),
            "skipped": tuple(skipped_val) if isinstance(skipped_val, Sequence) else (),
            "error": state.get("error"),
        }

    def _record_wakeup_tick(
        self,
        *,
        claimed: Sequence[str],
        skipped: Sequence[dict[str, str]],
        error: str | None,
    ) -> None:
        with self._wakeup_tick_lock:
            self._wakeup_tick_state = {
                "at_ms": int(time.time() * 1000),
                "claimed": tuple(claimed)[:32],
                "skipped": tuple(skipped)[:32],
                "error": None if error is None else error[:500],
            }

    @staticmethod
    def _skip_payload(skipped: Sequence[WakeupSkip]) -> list[dict[str, str]]:
        return [{"wakeup_id": item.wakeup_id, "session_id": item.session_id, "reason": item.reason} for item in skipped[:32]]

    def tick(self, now_ms: int | None = None) -> dict[str, object]:
        """Claim every currently due wakeup and drive the ones whose sessions can start.

        Explicit calls run each claimed turn synchronously. The core's own ticker
        marks its thread-local dispatch mode and launches each claimed turn on its
        own worker under that session's lock, so a long run never blocks the ticker
        or another due session.
        """
        self.ensure_open()
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if type(now) is not int or now < 0:
            raise ValueError("now_ms must be a non-negative integer")
        async_dispatch = bool(getattr(self._wakeup_tick_mode, "async_dispatch", False))
        claims, skipped = self.store.claim_due_wakeups(
            str(self.workspace),
            now,
            revision_id=self.revision_id,
            eligible_owners=self.plugin_runtime.observer_owners.get("wakeup_fired", ()),
        )
        skipped_payload = self._skip_payload(skipped)
        self._record_wakeup_tick(claimed=[wakeup.wakeup_id for wakeup, _session in claims], skipped=skipped_payload, error=None)
        if not claims:
            return {"fired": None, **({"skipped": skipped_payload} if skipped_payload else {})}
        self.notify_commit()
        if async_dispatch:
            for wakeup, session in claims:
                self._launch_claimed_wakeup(wakeup, session)
            first_wakeup, first_session = claims[0]
            return {
                "fired": first_wakeup.wakeup_id,
                "fired_ids": [wakeup.wakeup_id for wakeup, _session in claims],
                "session_id": first_session.session_id,
                "status": "dispatched",
                "wakeup_status": "claimed",
                **({"skipped": skipped_payload} if skipped_payload else {}),
            }
        results = [self._drive_claimed_wakeup(wakeup, session) for wakeup, session in claims]
        first = results[0]
        first["fired_ids"] = [wakeup.wakeup_id for wakeup, _session in claims]
        if len(results) > 1:
            first["results"] = results
        if skipped_payload:
            first["skipped"] = skipped_payload
        return first

    def _launch_claimed_wakeup(self, wakeup: WakeupRecord, session: SessionRecord) -> None:
        """Run one already-claimed wakeup turn on its own worker."""
        worker = threading.Thread(
            target=self._drive_claimed_wakeup_safely,
            args=(wakeup, session),
            name=f"CoreWakeup:{session.session_id[:16]}",
            daemon=True,
        )
        with self._session_locks_guard:
            live = [item for item in self._wakeup_workers if item.is_alive()]
            live.append(worker)
            self._wakeup_workers = live
        worker.start()

    def _drive_claimed_wakeup_safely(self, wakeup: WakeupRecord, session: SessionRecord) -> None:
        try:
            self._drive_claimed_wakeup(wakeup, session)
        except Exception as exc:
            message = f"wakeup {wakeup.wakeup_id} run failed: {type(exc).__name__}: {exc}"
            self._record_wakeup_tick(claimed=(), skipped=(), error=message)
            logger.warning("%s", message)

    def _drive_claimed_wakeup(self, wakeup: WakeupRecord, session: SessionRecord) -> dict[str, object]:
        """Drive one claimed wakeup under its session lock and report its terminal status."""
        wakeup_id = wakeup.wakeup_id
        root_id = wakeup.root_id
        run_id = wakeup.claim_run_id or session.run_id or ""
        wakeup_sequence = session.last_event_sequence
        logical_turn = uuid4().hex
        abort = Cancellation()
        self.register_run(session.session_id, run_id, abort)
        try:
            with self._session_lock(session.session_id):
                self.ensure_open()
                # Commands may commit beside this run without a session run lock. Re-read
                # the tip under the lock so the driver's in-memory prefix matches durable
                # order; the claim event itself stays addressed by its recorded sequence.
                current = self.store.session(session.session_id)
                if current.run_id != run_id:
                    raise StoreConflict(f"wakeup {wakeup_id!r} claim was superseded by run {current.run_id!r}")
                cursor = _Cursor(root_id=root_id, logical_turn=logical_turn)
                execution = _Execution(current, cursor, list(self.store.events(session.session_id)), run_id)
                self._fact_delivery.deliver(
                    execution,
                    (committed_event(execution, wakeup_sequence, EventType.WAKEUP_FIRED),),
                )
                result = self._turn_driver.drive(execution)
        finally:
            self.unregister_run(session.session_id)
        terminal = self._wakeup_terminal_status(result)
        if terminal is not None:
            # The run-finished commit already settled the claim atomically; this
            # idempotent call keeps the explicit tick contract when a driver
            # returns a terminal result without a new claim row.
            self.store.complete_wakeup(wakeup_id, terminal)
        return {
            "fired": wakeup_id,
            "session_id": session.session_id,
            "status": result.status,
            "wakeup_status": terminal if terminal is not None else "claimed",
        }

    @staticmethod
    def _wakeup_terminal_status(result: CoreResult) -> Literal["completed", "failed", "cancelled"] | None:
        """The wakeup settlement a run result implies, or None while the run is parked."""
        if result.status in ("completed", "idle"):
            return "completed"
        if result.status == "failed":
            return "failed"
        if result.status in (SessionStatus.WAITING, SessionStatus.RUNNING, "waiting", "running", "queued"):
            return None
        if result.stop_cause in ("user_stop", "owner_cancel"):
            return "cancelled"
        return "failed"

    def _wakeup_loop(self) -> None:
        """Core-owned tick: claim every due wakeup, launch each run, never replay slots."""
        self._wakeup_tick_mode.async_dispatch = True
        last_error: str | None = None
        try:
            while not self._closed:
                try:
                    outcome = self.tick()
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    self._record_wakeup_tick(claimed=(), skipped=(), error=message)
                    if message != last_error:
                        logger.warning("wakeup tick failed: %s", message)
                        last_error = message
                    time.sleep(1.0)
                    continue
                last_error = None
                if outcome.get("fired") is not None:
                    continue
                with self._session_locks_guard:
                    self._wakeup_workers = [item for item in self._wakeup_workers if item.is_alive()]
                time.sleep(1.0)
        finally:
            self._wakeup_tick_mode.async_dispatch = False

    # --- Background actions: owner drivers outside any model turn ---

    def drive_background_action(self, claimed: ContinuationRecord, abort: threading.Event) -> None:
        """Delegate one claimed action to the background executor."""
        self._background_execution.drive(claimed, abort)

    def _commit_tool_wait_events(
        self,
        session_id: str,
        expected_session: SessionRecord,
        events: Sequence[EventDraft],
        *,
        status: SessionStatus | str | None = None,
        checkpoint_kind: CheckpointKind | str | None = None,
        checkpoint_payload: Mapping[str, object] | None = None,
        run_id: str | None = None,
    ) -> None:
        target_status = SessionStatus(status) if isinstance(status, str) else (status or expected_session.status)
        target_checkpoint_kind = CheckpointKind(checkpoint_kind) if isinstance(checkpoint_kind, str) else checkpoint_kind
        self.store.commit_staged(
            session_id,
            expected_sequence=expected_session.last_event_sequence,
            expected_run_id=expected_session.run_id,
            run_id=run_id or expected_session.run_id or uuid4().hex,
            revision_id=self.revision_id,
            events=self._commit_pipeline.stamp_fact_owners(events),
            status=target_status,
            checkpoint_kind=target_checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
        )
        self.notify_commit()

    def cancel_background_tool_requests(
        self,
        session_id: str,
        action_id: str,
        *,
        status: Literal["cancelled", "interrupted", "core_lost", "budget_exhausted"],
        stop_cause: StopCause | None,
        reason: str,
    ) -> None:
        while True:
            expected_session = self.store.session(session_id)
            events = list(self.store.events(session_id))
            terminal_request_ids = self._background_execution.terminal_request_ids(events)
            requests = [
                event
                for event in events
                if event.event_type == EventType.TOOL_DEFERRED
                and event.payload.get("background_action_id") == action_id
                and event.payload.get("result_delivery") == "requester"
                and event.payload.get("request_id") not in terminal_request_ids
            ]
            if not requests:
                return
            terminal_events: list[EventDraft] = []
            request_intents: dict[int, bool] = {}
            for request in requests:
                request_id = request.payload.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    continue
                intent_exists = any(
                    event.event_type == EventType.TOOL_INTENT and event.payload.get("batch_sequence") == request.sequence for event in events
                )
                request_intents[request.sequence] = intent_exists
                if status == "cancelled" and intent_exists:
                    continue
                payload: dict[str, object] = {
                    "request_id": request_id,
                    "background_action_id": action_id,
                    "owner": request.payload.get("requester", ""),
                    "revision_id": request.payload.get("expected_revision", ""),
                    "tool_name": decode_tool_call(wire_object(request.payload["call"], "deferred tool call")).tool_name,
                    "status": status,
                    "reason": reason,
                }
                if stop_cause is not None:
                    payload["stop_cause"] = stop_cause
                if intent_exists and status in {"interrupted", "core_lost"}:
                    payload["execution"] = {
                        "cancellation_signalled": stop_cause in {"user_stop", "owner_cancel", "shutdown"},
                        "execution_stopped": False,
                        "side_effect_state": "unknown",
                    }
                terminal_events.append(EventDraft(EventType.TOOL_DEFERRED_CANCELLED, "core", "plugin", payload))
            if not terminal_events:
                return
            checkpoint = self.store.checkpoint(session_id)
            checkpoint_payload: Mapping[str, object] | None = None
            checkpoint_kind: CheckpointKind | None = None
            new_status: SessionStatus | None = None
            terminal_run_id: str | None = None
            if checkpoint is not None and checkpoint.kind is not CheckpointKind.TERMINAL and isinstance(checkpoint.payload, Mapping):
                cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
                current_request = next((request for request in requests if request.sequence == cursor.deferred_sequence), None)
                if (
                    current_request is not None
                    and current_request.payload.get("background_action_id") == action_id
                    and (not request_intents.get(current_request.sequence, False) or status == "core_lost")
                ):
                    approval_id: str | None = None
                    if cursor.approval_sequence is not None:
                        approval = next(
                            (
                                event
                                for event in events
                                if event.sequence == cursor.approval_sequence and event.event_type == EventType.APPROVAL_REQUESTED
                            ),
                            None,
                        )
                        raw_approval_id = None if approval is None else approval.payload.get("request_id")
                        approval_id = raw_approval_id if isinstance(raw_approval_id, str) else None
                    if approval_id is not None:
                        terminal_events.append(
                            EventDraft(
                                EventType.APPROVAL_DECIDED,
                                "core",
                                "core",
                                {"request_id": approval_id, "decision": "deny", "reason": reason},
                            )
                        )
                    run_payload: dict[str, object] = {
                        "reason": reason,
                        "logical_turn": cursor.logical_turn,
                        "deferred_request_id": current_request.payload.get("request_id"),
                        "background_action_id": action_id,
                    }
                    if stop_cause is not None:
                        run_payload["stop_cause"] = stop_cause
                    terminal_events.append(EventDraft(EventType.RUN_INTERRUPTED, "core", "core", run_payload))
                    terminal_run_id = expected_session.run_id or (checkpoint.run_id if checkpoint is not None else None) or uuid4().hex
                    execution = _Execution(expected_session, cursor, events, terminal_run_id)
                    terminal_events.extend(
                        self._governance.lifecycle_events(
                            execution,
                            self.snapshot,
                            "session_end",
                            status="interrupted",
                            error=reason,
                        )
                    )
                    cursor.batch_sequence = None
                    cursor.next_call = 0
                    cursor.intent_sequence = None
                    cursor.approval_sequence = None
                    cursor.deferred_sequence = None
                    cursor.deferred_depth = 0
                    cursor.deferred_action_id = None
                    cursor.deferred_origin_tools = None
                    cursor.deferred_origin_readonly = None
                    cursor.deferred_authority_missing = False
                    checkpoint_payload = cursor.model_dump(mode="json")
                    checkpoint_kind = CheckpointKind.TERMINAL
                    new_status = SessionStatus.INTERRUPTED
            try:
                self._commit_tool_wait_events(
                    session_id,
                    expected_session,
                    terminal_events,
                    status=new_status,
                    checkpoint_kind=checkpoint_kind,
                    checkpoint_payload=checkpoint_payload,
                    run_id=terminal_run_id,
                )
                return
            except SequenceConflict:
                continue

    def cancel_background_action(self, session_id: str, owner: str, action_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel one owner action and only its own queued or in-flight tool requests."""
        return self._background.cancel_action(session_id, owner, action_id, reason)

    def wait_background_actions(
        self, session_id: str, *, action_id: str | None = None, timeout_seconds: float = 300.0
    ) -> tuple[dict[str, object], ...]:
        """Wait until background actions settle and raise if any action failed."""
        return self._background.wait_actions(session_id, action_id=action_id, timeout_seconds=timeout_seconds)

    @property
    def default_model(self) -> str | None:
        return self.config.model

    @property
    def reasoning_effort(self) -> ProviderReasoningEffort | None:
        return self.config.reasoning_effort

    @property
    def abort_signal(self) -> Cancellation:
        return self._abort

    @property
    def cancellations(self) -> CancellationCoordinator:
        return self._cancellations

    @property
    def artifacts(self) -> ArtifactStore:
        return self._artifacts

    def check_expected_revision(self, expected_revision: str) -> None:
        self._check_expected_revision(expected_revision)

    def session_lock(self, session_id: str) -> AbstractContextManager[None]:
        return self._session_lock(session_id)

    def background_abort_event(self, action_id: str) -> threading.Event | None:
        return self._background.abort_event(action_id)

    def background_stop_cause(self, action_id: str) -> StopCause:
        return self._background.stop_cause(action_id)

    def child_captured_root_id(self, child_session_id: str) -> str:
        return self._delegation._captured_child_root_id(child_session_id)

    def captured_child_root_id(self, session_id: str) -> str:
        return self._delegation._captured_child_root_id(session_id)

    def raw_birth_record(self, session_id: str) -> Mapping[str, object] | None:
        return self._delegation._birth_record(session_id)

    def background_drain_pending(self, session_id: str) -> None:
        self._background_execution.drain_pending(session_id)

    def background_reconcile_lost(self, session_id: str) -> None:
        self._background_execution.reconcile_lost(session_id)

    def store_sessions(self, workspace: str) -> Sequence[SessionRecord]:
        return self.store.sessions(workspace=workspace)

    def store_session(self, session_id: str) -> SessionRecord:
        return self.store.session(session_id)

    def create_session_row(self, session_id: str) -> SessionRecord:
        return self._create_session_row(session_id)

    def store_checkpoint(self, session_id: str) -> Checkpoint | None:
        return self.store.checkpoint(session_id)

    def store_events(self, session_id: str, *, after_sequence: int | None = None) -> Sequence[StoredEvent]:
        seq = 0 if after_sequence is None else after_sequence
        return self.store.events(session_id, after_sequence=seq)

    def ensure_root_budget(self, root_id: str, budget: int | None) -> None:
        self.store.ensure_root_budget(root_id, budget)

    def root_budget_state(self, root_id: str) -> tuple[int, int] | None:
        return self.store.root_budget_state(root_id)

    def record_followup_event(
        self,
        session_id: str,
        event: EventDraft,
        *,
        run_id: str | None = None,
    ) -> None:
        self.store.append_session_event_at_tip(
            session_id,
            revision_id=self.revision_id,
            event=event,
            run_id=run_id,
        )
        self.notify_commit()

    def list_followups(self, session_id: str) -> FollowupListReceipt:
        """List pending user follow-ups for one session in FIFO order."""
        self.ensure_open()
        self.session(session_id)
        rows = self.store.pending_followups(session_id)
        return FollowupListReceipt(
            session_id=session_id,
            revision_id=self.revision_id,
            followups=tuple(
                QueuedFollowup(
                    continuation_id=row.continuation_id,
                    prompt=row.prompt,
                    revision_id=row.revision_id,
                    created_at=row.created_at,
                    max_steps=row.max_steps,
                    image_count=row.image_count,
                )
                for row in rows
            ),
        )

    def withdraw_followup(
        self,
        session_id: str,
        continuation_id: str,
        *,
        expected_revision: str,
    ) -> FollowupWithdrawReceipt:
        """Withdraw one pending user follow-up before claim, bound to the displayed revision."""
        self.ensure_open()
        self._check_expected_revision(expected_revision)
        if not isinstance(continuation_id, str) or not continuation_id.strip() or "\0" in continuation_id:
            raise ValueError("continuation_id must be nonempty text without NUL")
        self.session(session_id)
        record = self.store.withdraw_pending_followup(
            session_id,
            continuation_id,
            expected_revision=expected_revision,
            revision_id=self.revision_id,
        )
        self.notify_commit()
        self._delegation.notify_child_state_changed()
        return FollowupWithdrawReceipt(
            session_id=session_id,
            continuation_id=record.continuation_id,
            revision_id=self.revision_id,
            prompt=record.prompt,
            max_steps=record.max_steps,
            image_count=record.image_count,
            withdrawn=True,
        )

    def queue_followup(
        self,
        session: SessionRecord,
        prompt: str,
        max_steps: int | None,
        *,
        images: tuple[ImageAttachment, ...] = (),
    ) -> CoreResult:
        return self._followup.queue_followup(
            session,
            FollowupSubmission(prompt=prompt, max_steps=max_steps, images=images),
        )

    def execute_claimed_followup(self, session_id: str, claim: Any) -> CoreResult:
        return self._followup.execute_claimed_followup(session_id, claim)

    def claim_pending_followup(self, session_id: str) -> Any | None:
        return self._followup.claim_pending_followup(session_id)

    def recover_unstarted_child_drivers(self) -> list[tuple[str, str]]:
        return self._delegation._recover_unstarted_child_drivers()

    def recover_child_publications(self) -> list[tuple[str, str]]:
        return self._delegation._recover_child_publications()

    def first_child_message(self, session_id: str) -> ContinuationRecord | None:
        return self._delegation._first_child_message(session_id)

    def ask_binding(self, execution: _Execution, cursor: _Cursor) -> AskBinding:
        return self._interactive_ask.resolve_ask_binding(execution, cursor)

    def ask_binding_expired(self, binding: AskBinding) -> bool:
        return is_ask_binding_expired(binding)

    def adopt_ask_terminal(self, execution: _Execution, binding: AskBinding, outcome: AskOutcome) -> None:
        self._interactive_ask.adopt_ask_terminal(execution, binding, outcome)

    def definition_of(self, name: str) -> ToolDefinition | None:
        return self._snapshot.definition_of(name)

    def denial(self, call: ToolCall, definition: ToolDefinition, *, read_only: bool) -> str | None:
        return self._governance.denial(call, definition, read_only=read_only)

    def gate_denial(self, execution: _Execution, snapshot: Any, call: ToolCall, *, readonly: bool) -> str | None:
        return self._governance.gate_denial(execution, snapshot, call, readonly=readonly)

    def needs_approval(self, definition: ToolDefinition) -> bool:
        return self._governance.needs_approval(definition)

    def has_approval_policy(self, session_id: str, tool_name: str, arguments: Mapping[str, object]) -> bool:
        policies = self.store.approval_policies(session_id)
        for policy in policies:
            target_tool = policy.get("tool_name")
            if target_tool not in ("*", tool_name):
                continue
            pattern = policy.get("pattern")
            if pattern is None:
                return True
            pattern_str = str(pattern)
            path = arguments.get("path")
            if isinstance(path, str) and (path.startswith(pattern_str) or path == pattern_str):
                return True
            command = arguments.get("command")
            if isinstance(command, str) and (command.startswith(pattern_str) or command == pattern_str):
                return True
            for val in arguments.values():
                if isinstance(val, str) and (val.startswith(pattern_str) or val == pattern_str):
                    return True
        return False

    def list_approval_policies(self, session_id: str) -> list[ApprovalPolicyRule]:
        self.ensure_open()
        raw_list = self.store.approval_policies(session_id)
        res: list[ApprovalPolicyRule] = []
        for item in raw_list:
            raw_created = item.get("created_at")
            created_at = int(raw_created) if isinstance(raw_created, (int, float, str)) else 0
            res.append(
                ApprovalPolicyRule(
                    rule_id=str(item["rule_id"]),
                    tool_name=str(item["tool_name"]),
                    pattern=str(item["pattern"]) if item.get("pattern") is not None else None,
                    created_at=created_at,
                )
            )
        return res

    def revoke_approval_policy(self, session_id: str, rule_id: str) -> bool:
        self.ensure_open()
        with self._session_lock(session_id):
            revoked = self.store.revoke_approval_policy(session_id, rule_id, revision_id=self.revision_id)
            if revoked:
                self.notify_commit()
            return revoked

    def prepare_activation_target(self) -> tuple[str, Admission]:
        return self._activation.prepare_target()

    def pending_activation_request(self, execution: _Execution) -> Mapping[str, object] | None:
        return self._pending_activation_request(execution)

    def session_budgets(self, session_id: str) -> Sequence[Any]:
        return self.store.session_budgets(session_id)

    def merge_child_messages(self, execution: _Execution) -> None:
        self._delegation._merge_child_messages(execution)

    def lifecycle_events(
        self,
        execution: _Execution,
        snapshot: Any,
        kind: Literal["session_start", "session_end"],
        *,
        status: Literal["completed", "failed", "interrupted", ""] = "",
        error: str | None = None,
    ) -> tuple[EventDraft, ...]:
        return self._governance.lifecycle_events(execution, snapshot, kind, status=status, error=error)

    def commit_hook_events(self, execution: _Execution, events: tuple[EventDraft, ...]) -> None:
        """Append hook bookkeeping events without advancing the execution checkpoint."""
        if events:
            self.commit(execution, execution.session.status, None, *events)

    def settle_unsettled_dispatches(self, execution: _Execution) -> None:
        self._fact_delivery.settle_unsettled_dispatches(execution)

    def report_provider_progress(
        self,
        session_id: str,
        *,
        run_id: str,
        dispatch_id: str,
        dispatch_sequence: int,
        channel: str,
        text_delta: str | None,
    ) -> None:
        """Observational stream fragment on the existing control-event path; never raises."""
        if text_delta is None or not text_delta:
            return
        payload = {
            "run_id": run_id,
            "dispatch_id": dispatch_id,
            "dispatch_sequence": dispatch_sequence,
            "channel": channel,
            "text_delta": text_delta,
        }
        if len(canonical_json(payload).encode("utf-8")) > 16 * 1024:
            return
        self.commit_control_event(
            session_id,
            EventDraft(EventType.PROVIDER_PROGRESS, "provider", "model", payload),
        )

    def has_abort(self, session_id: str) -> bool:
        return self._cancellations.has_abort(session_id)

    def invoke(self, execution: _Execution, call: ToolCall, definition: ToolDefinition) -> CoreResult | None:
        return self._tool_execution.invoke(execution, call, definition)

    def invoke_readonly_batch(
        self,
        execution: _Execution,
        calls: tuple[ToolCall, ...],
        definitions: tuple[ToolDefinition, ...],
    ) -> CoreResult | None:
        return self._tool_execution.invoke_readonly_batch(execution, calls, definitions)

    def get_session_usage(
        self,
        session_id: str,
        *,
        since_sequence: int | None = None,
        session: SessionRecord | None = None,
    ) -> SessionUsageReport:
        seq = 0 if since_sequence is None else since_sequence
        return self._get_session_usage(session_id, since_sequence=seq, session=session)

    def cancel_pending_continuations(self, session_id: str, reason: str) -> None:
        self.store.cancel_pending_continuations(session_id, reason)

    @staticmethod
    def measured_run_elapsed_fields(execution: _Execution) -> dict[str, int]:
        """Stamp the duration this process actually measured for the run, if it dispatched at all."""
        starts: list[float] = []
        for event in execution.events:
            if event.run_id != execution.run_id or event.event_type != EventType.PROVIDER_INTENT:
                continue
            start_ms = event.payload.get("start_time_ms")
            if isinstance(start_ms, (int, float)) and not isinstance(start_ms, bool):
                starts.append(float(start_ms))
        if not starts:
            return {}
        return {"elapsed_ms": max(0, int(time.time() * 1000 - min(starts)))}

    def project_messages(
        self,
        execution: _Execution,
        *,
        model: str = "",
        tool_schema_characters: int = 0,
        allow_pending: bool = False,
    ) -> tuple[ProviderMessage, ...]:
        return self._context_projection.messages(
            execution,
            model=model,
            tool_schema_characters=tool_schema_characters,
            allow_pending=allow_pending,
        )

    @property
    def default_abort(self) -> Cancellation:
        return self._abort

    def active_abort(self, session_id: str) -> Cancellation | None:
        return self._cancellations.active_abort(session_id)

    def visible_tools(self, execution: _Execution, snapshot: GovernanceSnapshot) -> tuple[ToolDefinition, ...]:
        return self._governance.visible_tools(execution, snapshot)

    def post_tool_hooks(
        self,
        execution: _Execution,
        snapshot: ToolExecutionSnapshot,
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        return self._governance.post_tool_hooks(execution, snapshot, call, result)

    def bound_target(self, execution: _Execution, call: ToolCall) -> str | None:
        return self._bound_target(execution, call)

    def prepare_report(self, result: ToolResult) -> tuple[ToolResult, int, bool]:
        return self._projection.prepare_report(result)

    def pending_call(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]:
        return self._turn_driver.pending(execution)

    def pending(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]:
        return self._turn_driver.pending(execution)

    def report(
        self,
        execution: _Execution,
        call: ToolCall,
        result: ToolResult,
        *,
        interrupted: bool = False,
        staged_state: tuple[StagedStateUpdate, ...] = (),
        staged_events: tuple[EventDraft, ...] = (),
        staged_conts: tuple[StagedContinuationDraft, ...] = (),
        staged_cancels: tuple[StagedCancellation, ...] = (),
        staged_budget: PluginBudget | None | Literal["clear"] = None,
        staged_receipts: tuple[ObserverReceiptRecord, ...] = (),
        staged_wakeups: tuple[StagedWakeup, ...] = (),
        staged_wakeup_cancels: tuple[StagedWakeupCancel, ...] = (),
        budget_exhaustion: BudgetExhaustion | None = None,
        staged_observations: tuple[Change, ...] = (),
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
        prefix_events: tuple[EventDraft, ...] = (),
        ask_answer_guard: str | None = None,
    ) -> None:
        self._tool_execution.report(
            execution,
            call,
            result,
            interrupted=interrupted,
            staged_state=staged_state,
            staged_events=staged_events,
            staged_conts=staged_conts,
            staged_cancels=staged_cancels,
            staged_budget=staged_budget,
            staged_receipts=staged_receipts,
            staged_wakeups=staged_wakeups,
            staged_wakeup_cancels=staged_wakeup_cancels,
            budget_exhaustion=budget_exhaustion,
            staged_observations=staged_observations,
            pinned_run_id=pinned_run_id,
            enforce_pin=enforce_pin,
            prefix_events=prefix_events,
            ask_answer_guard=ask_answer_guard,
        )

    def stop(
        self,
        execution: _Execution,
        reason: str,
        *,
        stop_cause: StopCause = "user_stop",
        budget_exhaustion: BudgetExhaustion | None = None,
    ) -> CoreResult:
        return self._turn_driver.stop(execution, reason, stop_cause=stop_cause, budget_exhaustion=budget_exhaustion)

    def reset_abort(self) -> None:
        self._abort.set_cancelled(False)

    def register_run(self, session_id: str, run_id: str | None, abort: Cancellation) -> None:
        self._cancellations.register_run(session_id, run_id, abort)

    def unregister_run(self, session_id: str) -> None:
        self._cancellations.unregister_run(session_id)

    def chain_of(self, session_id: str) -> frozenset[str]:
        return self._delegation._chain_of(session_id)

    def settle_parked_cancel(self, session_id: str, reason: str, stop_cause: StopCause) -> None:
        self._settle_parked_cancel(session_id, reason, stop_cause)

    def result(
        self,
        execution: _Execution,
        *,
        output: str | None = None,
        error: str | None = None,
        pending_approval: Mapping[str, object] | None = None,
        pending_ask: Mapping[str, object] | None = None,
        stop_cause: StopCause | None = None,
        budget_exhaustion: BudgetExhaustion | None = None,
    ) -> CoreResult:
        return self._turn_driver.result(
            execution,
            output=output,
            error=error,
            pending_approval=pending_approval,
            pending_ask=pending_ask,
            stop_cause=stop_cause,
            budget_exhaustion=budget_exhaustion,
        )

    def drive(self, execution: _Execution) -> CoreResult:
        return self._turn_driver.drive(execution)

    def notify_requested(self, child_session: SessionRecord, events: Sequence[StoredEvent]) -> bool:
        return self._delegation._notify_requested(child_session, events)

    def publish_child_terminal(
        self,
        parent_session_id: str,
        child_session_id: str,
        *,
        run_id: str,
        status: ChildRunStatus,
        output: str | None,
        error: str | None,
        stop_cause: StopCause | None,
        label: str,
        notify: bool,
        deliver: bool,
        budget_exhaustion: BudgetExhaustion | None,
    ) -> None:
        self._delegation._publish_child_terminal(
            parent_session_id,
            child_session_id,
            run_id=run_id,
            status=status,
            output=output,
            error=error,
            stop_cause=stop_cause,
            label=label,
            notify=notify,
            deliver=deliver,
            budget_exhaustion=budget_exhaustion,
        )

    def deliver_child_notice(self, parent_session_id: str, notice_id: str) -> None:
        self._delegation._deliver_child_notice(parent_session_id, notice_id)

    def drain_background_pending(self, session_id: str) -> None:
        self._background_execution.drain_pending(session_id)

    def drive_pending(self, session_id: str, *, limit: int = 32) -> None:
        self._run_coordinator.drive_pending(session_id, limit=limit)

    def notify_child_state_changed(self) -> None:
        self._delegation.notify_child_state_changed()

    def interrupt_batch(
        self,
        execution: _Execution,
        *,
        budget_exhaustion: BudgetExhaustion | None = None,
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
    ) -> None:
        self._tool_execution.interrupt_batch(
            execution,
            budget_exhaustion=budget_exhaustion,
            pinned_run_id=pinned_run_id,
            enforce_pin=enforce_pin,
        )

    def commit(
        self,
        execution: _Execution,
        status: SessionStatus,
        kind: CheckpointKind | None,
        *events: EventDraft,
        start_turn: bool = False,
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_wakeups: Sequence[StagedWakeup] = (),
        staged_wakeup_cancels: Sequence[StagedWakeupCancel] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: ContinuationStatus = ContinuationStatus.COMPLETED,
        reserve_root: bool = False,
        root_budget_grant: RootBudgetGrant | tuple[str, int, str] | None = None,
        checkpoint_bindings: Sequence[CheckpointBinding | tuple[str, str]] = (),
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
        ask_answer_guard: str | None = None,
    ) -> tuple[StoredEvent, ...]:
        return self._commit_pipeline.commit(
            execution,
            status,
            kind,
            *events,
            start_turn=start_turn,
            staged_state=staged_state,
            staged_continuations=staged_continuations,
            staged_cancellations=staged_cancellations,
            staged_wakeups=staged_wakeups,
            staged_wakeup_cancels=staged_wakeup_cancels,
            staged_budgets=staged_budgets,
            observer_receipts=observer_receipts,
            completed_continuation_id=completed_continuation_id,
            completed_continuation_status=completed_continuation_status,
            reserve_root=reserve_root,
            root_budget_grant=root_budget_grant,
            checkpoint_bindings=checkpoint_bindings,
            pinned_run_id=pinned_run_id,
            enforce_pin=enforce_pin,
            ask_answer_guard=ask_answer_guard,
        )

    def messages(self, execution: _Execution, *, completed_only: bool, model: str) -> tuple[ProviderMessage, ...]:
        return self._context_projection.messages(execution, completed_only=completed_only, model=model)

    def commit_accounted_event(self, session_id: str, event: EventDraft) -> None:
        """Append one account-bearing fact, raising when it cannot be persisted.

        A provider turn already happened when its usage event is written, so a
        failed accounting commit must fail the caller instead of leaving billed
        spend silently unrecorded. An accounted event that is also a public fact
        (a governed model query) is delivered to its frozen owners exactly once.
        """
        # A side query can finish beside an active turn. Reuse the public commit
        # path so a lost sequence CAS retries against the fresh tip instead of
        # failing after the provider was already billed.
        self._commit_pipeline.commit_public_fact(session_id, event)

    def commit_control_event(self, session_id: str, event: EventDraft) -> None:
        """Append one best-effort diagnostic control fact; losing it never blocks owner work."""
        try:
            self.commit_accounted_event(session_id, event)
        except Exception:
            return

    def _report_progress(self, lease: LeaseContext, payload: Mapping[str, object]) -> None:
        if len(canonical_json(payload).encode("utf-8")) > 16 * 1024:
            raise ValueError("progress report exceeds the 16 KiB bound")
        self.commit_control_event(
            lease.identity.session_id,
            EventDraft(
                EventType.TOOL_PROGRESS,
                f"plugin.{lease.identity.owner}",
                lease.identity.actor,
                {
                    "invocation_id": lease.identity.invocation_id,
                    "owner": lease.identity.owner,
                    "run_id": lease.identity.run_id,
                    "revision_id": lease.identity.revision_id,
                    "progress": json_wire_object(payload),
                },
            ),
        )

    def governed_model_query(
        self,
        session_id: str,
        prompt: str,
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
        owner: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> str:
        """A tool-free side turn, optionally reading the current conversation window.

        Actual provider usage is recorded against the invoking ``owner`` and the
        session, but the question and answer never extend the conversation.
        """
        return self._model_query.query(
            session_id,
            prompt,
            model=model,
            max_output_tokens=max_output_tokens,
            include_context=include_context,
            owner=owner,
            abort_signal=abort_signal,
        )

    def governed_model_query_structured(
        self,
        session_id: str,
        prompt: str,
        result_schema: Mapping[str, object],
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
        owner: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> StructuredModelResult:
        """A tool-free side turn parsed and validated against ``result_schema``.

        Same dispatch, abort, usage event, and tool-calls guard as
        ``governed_model_query``.
        """
        return self._model_query.query_structured(
            session_id,
            prompt,
            result_schema,
            model=model,
            max_output_tokens=max_output_tokens,
            include_context=include_context,
            owner=owner,
            abort_signal=abort_signal,
        )

    def commit_staged_rebased(
        self,
        execution: _Execution,
        *,
        status: SessionStatus,
        kind: CheckpointKind | None,
        events: Sequence[EventDraft] = (),
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_wakeups: Sequence[StagedWakeup] = (),
        staged_wakeup_cancels: Sequence[StagedWakeupCancel] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: ContinuationStatus = ContinuationStatus.COMPLETED,
        reserve_root: bool = False,
        root_budget_grant: RootBudgetGrant | tuple[str, int, str] | None = None,
        start_turn: bool = False,
        checkpoint_bindings: Sequence[CheckpointBinding | tuple[str, str]] = (),
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
        ask_answer_guard: str | None = None,
    ) -> tuple[StoredEvent, ...]:
        return self._commit_pipeline.commit_staged_rebased(
            execution,
            status=status,
            kind=kind,
            events=events,
            staged_state=staged_state,
            staged_continuations=staged_continuations,
            staged_cancellations=staged_cancellations,
            staged_wakeups=staged_wakeups,
            staged_wakeup_cancels=staged_wakeup_cancels,
            staged_budgets=staged_budgets,
            observer_receipts=observer_receipts,
            completed_continuation_id=completed_continuation_id,
            completed_continuation_status=completed_continuation_status,
            reserve_root=reserve_root,
            root_budget_grant=root_budget_grant,
            start_turn=start_turn,
            checkpoint_bindings=checkpoint_bindings,
            pinned_run_id=pinned_run_id,
            enforce_pin=enforce_pin,
            ask_answer_guard=ask_answer_guard,
        )

    def commit_public_staged(
        self,
        session_id: str,
        *,
        events: Sequence[EventDraft],
        run_id: str | None = None,
        status: SessionStatus | None = None,
        revision_id: str | None = None,
        checkpoint_kind: CheckpointKind | None = None,
        checkpoint_payload: Mapping[str, object] | None = None,
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_wakeups: Sequence[StagedWakeup] = (),
        staged_wakeup_cancels: Sequence[StagedWakeupCancel] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        staged_confirmations: Sequence[StagedConfirmation] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
    ) -> SessionRecord:
        return self._commit_pipeline.commit_public_staged(
            session_id,
            events=events,
            run_id=run_id,
            status=status,
            revision_id=revision_id,
            checkpoint_kind=checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
            staged_state=staged_state,
            staged_continuations=staged_continuations,
            staged_cancellations=staged_cancellations,
            staged_wakeups=staged_wakeups,
            staged_wakeup_cancels=staged_wakeup_cancels,
            staged_budgets=staged_budgets,
            staged_confirmations=staged_confirmations,
            observer_receipts=observer_receipts,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
        )

    def commit_public_fact(
        self,
        session_id: str,
        event: EventDraft,
        *,
        run_id: str | None = None,
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
    ) -> SessionRecord:
        return self._commit_pipeline.commit_public_fact(
            session_id,
            event,
            run_id=run_id,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
        )

    def session_read_only(self, session_id: str) -> bool:
        """Effective read-only for one session: the frozen core policy OR owner overlays."""
        return self._governance.session_read_only(session_id)

    def overlay_active(self, session_id: str, owner: str) -> bool:
        """Whether one owner's read-only overlay is active on this session."""
        self.ensure_open()
        return self.store.session_overlay(session_id, owner) is not None

    def visible_tool_names_for_session(self, session_id: str, snapshot: Any) -> tuple[str, ...]:
        return self._governance.visible_tool_names_for_session(session_id, snapshot)

    def artifact_page(self, digest: str, offset: int, limit: int) -> ArtifactPage:
        return self._projection.page(digest, offset, limit)

    def _drive_pending_activations(self) -> None:
        self._run_coordinator.drive_pending_activations()

    _drive_pending = drive_pending
    _commit_control_event = commit_control_event
    _interrupt_batch = interrupt_batch
    _commit = commit
