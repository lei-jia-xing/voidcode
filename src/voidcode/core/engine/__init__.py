"""Governed turns over immutable revisions and durable native tool batches.

`Core` is the stable public facade. Construction lives here because it is the
one place that wires every responsibility together; the behaviour itself lives
in private module mixins. Only the facade and the result types callers already
depend on are exported.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from pathlib import Path
from threading import Lock

from ..activation import Admission, ExecutionConfig
from ..activation_coordinator import ActivationCoordinator
from ..artifacts import ArtifactStore
from ..background_actions import BackgroundActionRegistry
from ..background_execution import BackgroundExecution
from ..cancellation import Cancellation, CancellationCoordinator
from ..context_projection import ContextProjector
from ..delegation import DelegationManager
from ..execution import _Cursor, _Execution
from ..fact_delivery import FactDelivery
from ..output_projection import ResultProjector
from ..plugins import PluginSource
from ..processes import ProcessHost
from ..sandbox import SandboxBackend
from ..storage.store import DATABASE_FILENAME, SqliteStore
from ..usage import UsageProjector
from ._activation import CoreActivation
from ._background import CoreBackground
from ._capability import _Snapshot
from ._children import CoreChildren
from ._commands import CoreCommands
from ._host_adapters import CoreHostAdapters
from ._lifecycle import CoreLifecycle
from ._persistence import CorePersistence
from ._sessions import CoreSessions
from ._wakeups import CoreWakeups
from .command_runtime import CommandRuntime
from .commit_pipeline import CommitPipeline
from .continuation_scheduler import ContinuationScheduler
from .followup import FollowupManager
from .governance import ToolGovernance
from .interactive_ask import InteractiveAskManager
from .model_query import ModelQueryRuntime
from .run_coordinator import RunCoordinator
from .tool_execution import ToolExecutionManager
from .turn_execution import MODEL_CONFIGURATION_GUIDANCE, CoreResult, TurnDriver

logger = logging.getLogger(__name__)

__all__ = (
    "Core",
    "CoreResult",
    "MODEL_CONFIGURATION_GUIDANCE",
    "_Cursor",
    "_Execution",
)


class Core(
    CoreCommands,
    CoreSessions,
    CoreChildren,
    CoreWakeups,
    CoreBackground,
    CorePersistence,
    CoreHostAdapters,
    CoreActivation,
    CoreLifecycle,
):
    """Authority for governed runs, dispatch, persistence, and capability generations."""

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
        # Product-owned config filename (e.g. voidcode.toml): the product layer
        # passes its own name in; the core never spells it, only guards it.
        config_filename: str = "voidcode.toml",
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
