"""Execution ownership: the runtime's single authority for "may this execution still commit?".

A background-task execution — one dispatched worker turn for one task id — runs
under an :class:`ExecutionLease` issued by the background-task supervisor at
dispatch time. The lease is the *only* thing that makes that execution's writes
legal:

* the supervisor binds the lease to the worker thread for the whole turn, so
  every persistence call the execution makes is validated by
  :meth:`ExecutionOwnershipRegistry.assert_writes_allowed` at the single storage
  write gateway (``SqliteSessionStore._write_connect``). A write path added
  later cannot forget the check because the check is not at the call site.
  The binding is thread-scoped, so this is exactly where the guarantee stops:
  runtime-owned commits MUST run on the execution's own thread — a manager that
  spawns its own thread and persists from it (tool executor, MCP/ACP, background
  process) would step outside the lease and must have the lease propagated to it
  or commit on the execution thread;
* ownership is revoked *by lease identity*, never by task lookup. A new
  execution that takes ownership of the same task (resume/steer/retry) receives
  a new generation, so it can never re-authorize the previous execution's
  thread — a late completion from the old worker is not a resume;
* a refused write is preserved as a diagnostic (``logger.warning`` plus the
  bounded ring served by :meth:`ExecutionOwnershipRegistry.late_writes`) and
  never mutates session or task truth.

The corresponding invariant text lives in
``docs/contracts/background-task-delegation.md`` ("Execution ownership").
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

_MAX_RECORDED_LATE_WRITES: Final[int] = 256


class ExecutionOwnershipRevokedError(Exception):
    """Raised when a revoked execution attempts a runtime-owned write.

    The storage write gateway raises this before opening a transaction, so the
    write can never land; the refusal is recorded on the lease first. Callers
    that only want to *skip* a late commit should pre-check with
    :meth:`ExecutionOwnershipRegistry.authorize_bound` instead of catching this.
    """

    code = "execution_ownership_revoked"


@dataclass(frozen=True, slots=True)
class LateWriteDiagnostic:
    """One deduplicated observation of a write refused after ownership loss."""

    task_id: str
    generation: int
    operation: str
    reason: str | None
    count: int
    recorded_at_unix_ms: int
    thread: str

    def as_payload(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "generation": self.generation,
            "operation": self.operation,
            "revocation_reason": self.reason,
            "count": self.count,
            "recorded_at_unix_ms": self.recorded_at_unix_ms,
            "thread": self.thread,
        }


@dataclass(slots=True, eq=False)
class ExecutionLease:
    """Ownership token for exactly one background-task execution.

    Identity (``task_id`` + ``generation``) is what the commit paths validate;
    equality is intentionally identity-based so a lease can never be confused
    with a same-generation token rebuilt by a caller. Revocation is permanent
    and private to the registry.
    """

    task_id: str
    generation: int
    workspace: Path
    _revocation_reason: str | None = None

    @property
    def revoked(self) -> bool:
        return self._revocation_reason is not None

    @property
    def revocation_reason(self) -> str | None:
        return self._revocation_reason

    def _revoke(self, reason: str) -> None:
        if self._revocation_reason is None:
            self._revocation_reason = reason


class _ThreadBinding(threading.local):
    """Per-thread lease slot.

    A plain ``threading.local`` carries no declared attributes, so reading the
    bound lease would need a ``getattr`` probe; the typed subclass declares the
    slot instead. ``__init__`` runs per thread, so an unbound thread reads
    ``None`` exactly as the probe's default did.
    """

    def __init__(self) -> None:
        self.lease: ExecutionLease | None = None


def _lease_key(*, workspace: Path, task_id: str) -> tuple[str, str]:
    return (str(workspace), task_id)


class ExecutionOwnershipRegistry:
    """Issues, revokes, and validates background-execution ownership leases.

    Process-local and thread-safe, mirroring ``ACTIVE_SESSION_REGISTRY``: the
    truth it guards (which execution may write) is inherently per-process,
    because an execution is a live thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[tuple[str, str], ExecutionLease] = {}
        self._generations: dict[tuple[str, str], int] = {}
        self._late_writes: OrderedDict[tuple[str, int, str, str], LateWriteDiagnostic] = OrderedDict()
        self._thread_binding = _ThreadBinding()

    # ------------------------------------------------------------------ #
    # Issuance / revocation (supervisor control plane)
    # ------------------------------------------------------------------ #
    def grant(self, *, workspace: Path, task_id: str) -> ExecutionLease:
        """Issue a lease to a starting execution, superseding any older one.

        Granting is how ownership is *taken*. The previous execution (if any)
        is revoked by identity, so a superseded worker keeps losing its writes
        even after a newer execution has taken over the same task.
        """
        key = _lease_key(workspace=workspace, task_id=task_id)
        with self._lock:
            generation = self._generations.get(key, 0) + 1
            self._generations[key] = generation
            superseded = self._current.get(key)
            if superseded is not None:
                superseded._revoke(f"superseded by execution generation {generation}")
            lease = ExecutionLease(task_id=task_id, generation=generation, workspace=workspace)
            self._current[key] = lease
        if superseded is not None:
            logger.warning(
                "background task %s execution generation %d superseded before it released ownership",
                superseded.task_id,
                superseded.generation,
            )
        return lease

    def revoke(self, *, workspace: Path, task_id: str, reason: str) -> ExecutionLease | None:
        """Revoke the task's current execution; returns the revoked lease, if any."""
        key = _lease_key(workspace=workspace, task_id=task_id)
        with self._lock:
            lease = self._current.pop(key, None)
        if lease is None:
            return None
        lease._revoke(reason)
        return lease

    def revoke_if_current(self, lease: ExecutionLease | None, *, reason: str) -> bool:
        """Revoke ``lease`` only while it is still the live execution for its task."""
        if lease is None:
            return False
        key = _lease_key(workspace=lease.workspace, task_id=lease.task_id)
        with self._lock:
            if self._current.get(key) is not lease:
                return False
            del self._current[key]
        lease._revoke(reason)
        return True

    def current(self, *, workspace: Path, task_id: str) -> ExecutionLease | None:
        """Return the live execution lease for ``task_id``, if one exists."""
        with self._lock:
            return self._current.get(_lease_key(workspace=workspace, task_id=task_id))

    def acquire(self, *, workspace: Path, task_id: str) -> ExecutionLease:
        """Adopt the live lease for ``task_id``, granting a fresh one when none is live."""
        live = self.current(workspace=workspace, task_id=task_id)
        if live is not None and not live.revoked:
            return live
        return self.grant(workspace=workspace, task_id=task_id)

    # ------------------------------------------------------------------ #
    # Validation (commit paths)
    # ------------------------------------------------------------------ #
    def is_authorized(self, lease: ExecutionLease) -> bool:
        """True while ``lease`` is the unrevoked live execution of its task."""
        if lease.revoked:
            return False
        with self._lock:
            return self._current.get(_lease_key(workspace=lease.workspace, task_id=lease.task_id)) is lease

    def authorize(self, lease: ExecutionLease, *, operation: str) -> bool:
        """Return whether ``lease`` may perform ``operation``, recording refusals."""
        if self.is_authorized(lease):
            return True
        self._record_late_write(lease=lease, operation=operation)
        return False

    def authorize_bound(self, *, task_id: str, operation: str) -> bool:
        """Return whether the current thread may commit for ``task_id``.

        Threads with no binding are the runtime control plane (shutdown drain,
        reconcile, cancel, resume entry) and are always authorized. A bound
        thread is authorized only while its own lease is still the live
        execution for that task, so a late commit is refused and recorded
        instead of mutating truth.
        """
        lease = self.bound_lease()
        if lease is None or lease.task_id != task_id:
            return True
        return self.authorize(lease, operation=operation)

    def assert_writes_allowed(self, *, operation: str) -> None:
        """Raise :class:`ExecutionOwnershipRevokedError` for a revoked-bound thread.

        Called by the storage write gateway, i.e. where the commits actually
        happen, so every present and future persistence path is covered.
        """
        lease = self.bound_lease()
        if lease is None or self.is_authorized(lease):
            return
        self._record_late_write(lease=lease, operation=operation)
        raise ExecutionOwnershipRevokedError(
            f"execution generation {lease.generation} of background task {lease.task_id} lost ownership "
            f"({lease.revocation_reason}); refused {operation}"
        )

    # ------------------------------------------------------------------ #
    # Thread binding (the execution's write eligibility)
    # ------------------------------------------------------------------ #
    @contextmanager
    def bind(self, lease: ExecutionLease | None) -> Iterator[None]:
        """Bind ``lease`` to the current thread for the duration of the block.

        The worker drives its run loop in its own thread, so the binding covers
        the whole execution — every storage write the run loop makes, plus the
        worker's own commit attempts. ``bind(None)`` marks a block as
        control-plane work (for example dispatching other queued tasks).
        """
        previous = self.bound_lease()
        self._thread_binding.lease = lease
        try:
            yield
        finally:
            self._thread_binding.lease = previous

    def bound_lease(self) -> ExecutionLease | None:
        """Return the lease bound to the current thread, if any."""
        return self._thread_binding.lease

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #
    def late_writes(self) -> tuple[LateWriteDiagnostic, ...]:
        """Return the recorded refusals (bounded, one entry per task, operation and refusing thread)."""
        with self._lock:
            return tuple(self._late_writes.values())

    def _record_late_write(self, *, lease: ExecutionLease, operation: str) -> None:
        """Preserve one refusal per refusing thread, without mutating truth.

        The refusing thread is part of the identity, not just a payload field: a
        single collapsed entry would report a count that disagrees with the
        per-refusal warnings and could attribute a refused write to a thread that
        did not make it.
        """
        thread_name = threading.current_thread().name
        key = (lease.task_id, lease.generation, operation, thread_name)
        with self._lock:
            existing = self._late_writes.get(key)
            self._late_writes[key] = LateWriteDiagnostic(
                task_id=lease.task_id,
                generation=lease.generation,
                operation=operation,
                reason=lease.revocation_reason,
                count=(existing.count + 1) if existing is not None else 1,
                recorded_at_unix_ms=int(time.time() * 1000),
                thread=thread_name,
            )
            self._late_writes.move_to_end(key)
            while len(self._late_writes) > _MAX_RECORDED_LATE_WRITES:
                self._late_writes.popitem(last=False)
        if existing is None:
            logger.warning(
                "refused %s for background task %s: execution generation %d lost ownership (%s); refusing thread=%s",
                operation,
                lease.task_id,
                lease.generation,
                lease.revocation_reason,
                thread_name,
            )


EXECUTION_OWNERSHIP = ExecutionOwnershipRegistry()
