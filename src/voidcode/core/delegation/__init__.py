"""Child delegation orchestration.

`DelegationManager` is the stable facade for spawn, join, send, cancel,
completion publication, notice delivery, and startup recovery. The durable
child mechanics live in private mixins; only the facade and the constants and
run-status helpers engine code already imports are exported.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from ._base import _DRIVER_MAX_WORKERS, CHILD_INLINE_OUTPUT_BYTES, CHILD_OUTPUT_PREVIEW_BYTES, DELEGATION_MAX_DEPTH
from ._outcomes import _child_run_status, _terminal_run_result
from ._recovery import _DelegationRecovery

if TYPE_CHECKING:
    from ..engine import Core

__all__ = (
    "CHILD_INLINE_OUTPUT_BYTES",
    "CHILD_OUTPUT_PREVIEW_BYTES",
    "DELEGATION_MAX_DEPTH",
    "DelegationManager",
    "_child_run_status",
    "_terminal_run_result",
)


class DelegationManager(_DelegationRecovery):
    """Orchestrates child delegation lifecycle, publications, and notices."""

    def __init__(self, core: Core) -> None:
        self.core = core
        #: In-process outcome cache: {child_id: {run_id: payload}}. The durable parent fact is
        #: restart truth.
        self._child_finished: dict[str, dict[str, dict[str, object]]] = {}
        self._child_finished_lock = threading.Condition()
        self._child_finished_generation = 0
        #: One durable publication per (child, run); a later recovery pass is idempotent.
        self._published_child_runs: set[tuple[str, str]] = set()
        #: (child, run) whose terminal fact/artifact is currently being produced.
        self._child_publication_inflight: set[tuple[str, str]] = set()
        #: Child ids this process already drives. A keyed retry that lands here must not
        #: start a second driver for a child whose first thread simply has not run yet;
        #: only a fresh process (empty set) may repair the crash window by starting one.
        #: ponytail: unbounded, one small id per spawn this process drove; cap it if a
        #: long-lived core ever drives enough children for this to matter.
        self._started_children: set[str] = set()
        #: Subtree targets currently being cancelled, with an install count so
        #: overlapping cancels of the same target never lift each other's fence.
        #: Install/release happens under the lifecycle lock; registration
        #: transactions consult the guard under the store lock over a
        #: caller-captured ancestor chain, so no store call happens under the
        #: lifecycle lock and lock order never inverts.
        self._lifecycle_lock = threading.Lock()
        self._cancel_fences: dict[str, int] = {}
        #: One bounded pool for child drivers and notice deliveries instead of bare
        #: threads. close() cancels queued work; in-flight drivers run to completion.
        self._driver_executor = ThreadPoolExecutor(max_workers=_DRIVER_MAX_WORKERS, thread_name_prefix="DelegationDriver")

    def close(self) -> None:
        with self._child_finished_lock:
            self._child_finished_generation += 1
            self._child_finished_lock.notify_all()
        self._driver_executor.shutdown(wait=False, cancel_futures=True)
