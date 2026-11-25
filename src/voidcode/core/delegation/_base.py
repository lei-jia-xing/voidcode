"""Shared delegation state, constants, and wait notification.

`DelegationManager.__init__` initializes this state; the private orchestration
mixins inherit these declarations so each concern is checked against one
contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ..engine import Core

#: A parent may nest children at most this deep. A child inherits the parent's policy
#: unchanged, so delegation never widens it; the chain-wide child count is governed by
#: the operator-configurable ExecutionConfig.max_root_children.
DELEGATION_MAX_DEPTH = 3
#: Larger terminal child output is represented by a full artifact reference and bounded preview;
#: this threshold is an inline representation policy, not a transport-frame or total-output cap.
CHILD_INLINE_OUTPUT_BYTES = 32 * 1024
#: Preview carried by a completion notice when the output is artifact-backed.
CHILD_OUTPUT_PREVIEW_BYTES = 4 * 1024
#: Max concurrent child drivers and notice deliveries on the shared pool.
_DRIVER_MAX_WORKERS = 32
#: Bound on the visible cause published when an accepted spawn cannot be recovered.
_CHILD_RECOVERY_ERROR_BYTES = 4 * 1024
#: Child-fact publication retries a moved parent tip this many times before leaving
#: the terminal child session for the startup recovery pass. Backoff between attempts
#: is uniform(0, 50ms), (0, 100ms), … capped at 2s; anything that is not a tip race
#: fails fast instead of spinning.
_CHILD_FACT_MAX_ATTEMPTS = 8
_CHILD_FACT_BACKOFF_BASE_SECONDS = 0.05
_CHILD_FACT_BACKOFF_CAP_SECONDS = 2.0


class DelegationBase:
    """Declarations for the manager fields plus the shared state-change wakeup."""

    core: Core
    #: In-process outcome cache: {child_id: {run_id: payload}}. The durable parent fact is
    #: restart truth.
    _child_finished: dict[str, dict[str, dict[str, object]]]
    _child_finished_lock: threading.Condition
    _child_finished_generation: int
    #: One durable publication per (child, run); a later recovery pass is idempotent.
    _published_child_runs: set[tuple[str, str]]
    #: (child, run) whose terminal fact/artifact is currently being produced.
    _child_publication_inflight: set[tuple[str, str]]
    #: Child ids this process already drives. A keyed retry that lands here must not
    #: start a second driver for a child whose first thread simply has not run yet;
    #: only a fresh process (empty set) may repair the crash window by starting one.
    _started_children: set[str]
    #: Subtree targets currently being cancelled, with an install count so
    #: overlapping cancels of the same target never lift each other's fence.
    _lifecycle_lock: threading.Lock
    _cancel_fences: dict[str, int]
    #: One bounded pool for child drivers and notice deliveries instead of bare
    #: threads. close() cancels queued work; in-flight drivers run to completion.
    _driver_executor: ThreadPoolExecutor

    def notify_child_state_changed(self) -> None:
        """Wake first-ready waiters after a durable child or parent-input transition."""
        with self._child_finished_lock:
            self._child_finished_generation += 1
            self._child_finished_lock.notify_all()
