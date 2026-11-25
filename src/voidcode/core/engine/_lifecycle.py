"""Core state, lifecycle, dispatch fence, session locks, and shutdown.

This is the private base every engine responsibility mixin extends. It declares
the mutable facade state and the internal operations responsible mixins provide,
then implements store access, locks, the commit clock, dispatch accounting, and
shutdown. Public `Core` in `engine/__init__.py` owns construction and is the
only supported entry point.
"""

from __future__ import annotations

import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Self
from uuid import uuid4

from ...sdk.client import BudgetExhaustion
from ...sdk.hooks import ObservedFactKind, StopCause
from ...sdk.json_values import json_wire_object
from ...sdk.plugin import StateScope
from ...sdk.provider import ProviderAbortSignal, ProviderReasoningEffort
from ...sdk.services import (
    CancelSubtreeReceipt,
    ChildOutcome,
    ChildSendReceipt,
    DescendantNode,
    PluginBudget,
    SessionUsageReport,
    StructuredModelResult,
)
from ...sdk.tools import ToolCall
from ..activation import Admission, ExecutionConfig
from ..activation_coordinator import ActivationCoordinator, ActivationFenced
from ..artifacts import ArtifactStore
from ..background_actions import BackgroundActionRegistry
from ..background_execution import BackgroundExecution
from ..cancellation import CURRENT, Cancellation, CancellationCoordinator, CancelTarget, RunBoundTarget
from ..child_contracts import ChildSpawnRequest
from ..context_projection import ContextProjector
from ..delegation import DelegationManager
from ..events import EventType
from ..execution import _Cursor, _Execution
from ..fact_delivery import FactDelivery
from ..output_projection import ResultProjector
from ..plugins import LeaseContext, PluginRuntime, PluginSource
from ..processes import ProcessHost
from ..sandbox import SandboxBackend
from ..storage.store import (
    Checkpoint,
    CheckpointKind,
    ContinuationStatus,
    EventDraft,
    InvalidRevision,
    ObserverReceiptRecord,
    SessionRecord,
    SessionStatus,
    SqliteStore,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
    StaleRevision,
    StoreConflict,
    StoredEvent,
)
from ..usage import UsageProjector
from ._capability import _Snapshot
from .command_runtime import CommandRuntime
from .commit_pipeline import CheckpointBinding, CommitPipeline, RootBudgetGrant
from .continuation_scheduler import ContinuationScheduler
from .followup import FollowupManager
from .governance import ToolGovernance
from .interactive_ask import InteractiveAskManager
from .model_query import ModelQueryRuntime
from .run_coordinator import RunCoordinator
from .tool_execution import ToolExecutionManager
from .turn_execution import TurnDriver


class CoreLifecycle(ABC):
    """Own the facade state and the activation/shutdown fence."""

    auto_drain: bool
    workspace: Path
    store: SqliteStore
    _snapshot: _Snapshot
    _closed: bool
    _abort: Cancellation
    _session_locks: dict[str, Lock]
    _session_locks_guard: Lock
    _cancellations: CancellationCoordinator
    _background: BackgroundActionRegistry
    _background_execution: BackgroundExecution
    _commit_cond: threading.Condition
    _commit_generation: int
    _calls_lock: threading.Condition
    _active_calls: dict[str, str]
    _active_dispatch_threads: dict[int, int]
    _activating_thread: int | None
    _activation_requested: threading.Event
    _state_dir: Path
    _admits: Callable[..., Admission] | None
    _config_filename: str
    _process_host: ProcessHost
    _delegation: DelegationManager
    _artifacts: ArtifactStore
    _lease_fd: int
    _activation: ActivationCoordinator
    _usage: UsageProjector
    _projection: ResultProjector
    _context_projection: ContextProjector
    _command_runtime: CommandRuntime
    _fact_delivery: FactDelivery
    _model_query: ModelQueryRuntime
    _governance: ToolGovernance
    _interactive_ask: InteractiveAskManager
    _followup: FollowupManager
    _commit_pipeline: CommitPipeline
    _tool_execution: ToolExecutionManager
    _continuation_scheduler: ContinuationScheduler
    _turn_driver: TurnDriver
    _run_coordinator: RunCoordinator
    _background_started: bool
    _background_workers: list[threading.Thread]
    _wakeup_workers: list[threading.Thread]
    _wakeup_tick_lock: Lock
    _wakeup_tick_state: dict[str, object]
    _wakeup_tick_mode: threading.local
    _resources: ExitStack

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

    def __enter__(self) -> Self:
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

    def check_expected_revision(self, expected_revision: str) -> None:
        self._check_expected_revision(expected_revision)

    def session_lock(self, session_id: str) -> AbstractContextManager[None]:
        return self._session_lock(session_id)

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

    @property
    def default_abort(self) -> Cancellation:
        return self._abort

    def register_run(self, session_id: str, run_id: str | None, abort: Cancellation) -> None:
        self._cancellations.register_run(session_id, run_id, abort)

    def unregister_run(self, session_id: str) -> None:
        self._cancellations.unregister_run(session_id)

    def has_abort(self, session_id: str) -> bool:
        return self._cancellations.has_abort(session_id)

    def active_abort(self, session_id: str) -> Cancellation | None:
        return self._cancellations.active_abort(session_id)

    def reset_abort(self) -> None:
        self._abort.set_cancelled(False)

    def _drive_pending_activations(self) -> None:
        self._run_coordinator.drive_pending_activations()

    @abstractmethod
    def session(self, session_id: str) -> SessionRecord: ...

    @abstractmethod
    def _get_session_usage(
        self, session_id: str, since_sequence: int = 0, until_sequence: int | None = None, *, session: SessionRecord | None = None
    ) -> SessionUsageReport: ...

    @abstractmethod
    def _get_owner_budget(self, session_id: str, owner: str) -> PluginBudget | None: ...

    @abstractmethod
    def _spawn_child_callback(self, request: ChildSpawnRequest) -> str: ...

    @abstractmethod
    def join_child(self, parent_session_id: str, child_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome: ...

    @abstractmethod
    def send_child(
        self,
        parent_session_id: str,
        child_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
        admission_guard: Callable[[], bool] | None = None,
    ) -> ChildSendReceipt: ...

    @abstractmethod
    def cancel_child(self, parent_session_id: str, child_id: str, reason: str = "owner cancellation") -> bool: ...

    @abstractmethod
    def list_descendants(self, session_id: str) -> tuple[DescendantNode, ...]: ...

    @abstractmethod
    def cancel_descendant_subtree(
        self, bound_session_id: str, target_session_id: str, *, expected_run_id: str | None, reason: str = "owner cancellation"
    ) -> CancelSubtreeReceipt: ...

    @abstractmethod
    def cancel_background_action(self, session_id: str, owner: str, action_id: str, reason: str = "owner cancellation") -> bool: ...

    @abstractmethod
    def governed_model_query(
        self,
        session_id: str,
        prompt: str,
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
        owner: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> str: ...

    @abstractmethod
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
    ) -> StructuredModelResult: ...

    @abstractmethod
    def _report_progress(self, lease: LeaseContext, payload: Mapping[str, object]) -> None: ...

    @abstractmethod
    def _commit(
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
    ) -> tuple[StoredEvent, ...]: ...

    @abstractmethod
    def _interrupt_batch(
        self, execution: _Execution, *, budget_exhaustion: BudgetExhaustion | None = None, pinned_run_id: str | None = None, enforce_pin: bool = False
    ) -> None: ...

    @staticmethod
    @abstractmethod
    def measured_run_elapsed_fields(execution: _Execution) -> dict[str, int]: ...

    @abstractmethod
    def visible_tool_names_for_session(self, session_id: str, snapshot: Any) -> tuple[str, ...]: ...

    @abstractmethod
    def session_read_only(self, session_id: str) -> bool: ...

    @abstractmethod
    def _wakeup_loop(self) -> None: ...

    @abstractmethod
    def cancel_background_tool_requests(
        self,
        session_id: str,
        action_id: str,
        *,
        status: Literal["cancelled", "interrupted", "core_lost", "budget_exhausted"],
        stop_cause: StopCause | None,
        reason: str,
    ) -> None: ...

    @abstractmethod
    def show(self, session_id: str, *, events: bool = False) -> dict[str, object]: ...

    @abstractmethod
    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport: ...

    @abstractmethod
    def capture_root_authorization(self, session: SessionRecord) -> tuple[str, int | None]: ...

    @abstractmethod
    def _build_runtime(self, sources: tuple[PluginSource, ...], *, revision_id: str, sandbox_backend: SandboxBackend | None) -> PluginRuntime: ...

    @abstractmethod
    def _bound_target(self, execution: _Execution, call: ToolCall) -> str | None: ...

    @abstractmethod
    def activate(self, *, session_id: str, source: str, request_id: str = "", target_revision_id: str = "") -> dict[str, object]: ...
