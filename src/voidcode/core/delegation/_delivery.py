"""Child notice and follow-up delivery into the owning session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..events import CHILD_MESSAGE_OWNER
from ..storage.store import (
    ContinuationRecord,
    SessionStatus,
    StoreConflict,
)
from ._outcomes import _DelegationOutcomes

if TYPE_CHECKING:
    from ..engine import _Execution


class _DelegationDelivery(_DelegationOutcomes):
    """Child notice and follow-up delivery into the owning session."""

    def _deliver_child_notice(self, session_id: str, continuation_id: str | None = None) -> None:
        """Wake an idle session; a busy one merges the message at its next batch boundary."""
        if not self.core.auto_drain:
            return
        try:
            if continuation_id is None:
                self.core.drive_pending(session_id, limit=32)
            else:
                self.core.drain_continuations(session_id, limit=1, continuation_id=continuation_id, _automatic=True)
        except Exception:
            # The message is durable; the next run, drain, or restart delivers it.
            return

    def _deliver_child_messages(self, session_id: str, continuation_id: str) -> None:
        """Deliver the exact follow-up a send reserved, even if another continuation is older."""
        try:
            self.core.drain_continuations(session_id, limit=1, continuation_id=continuation_id)
        except Exception:
            return

    def _merge_child_messages(self, execution: _Execution) -> int:
        """Claim messages into the CURRENT run after a committed batch, before next dispatch."""
        if execution.cursor.batch_sequence is not None or execution.cursor.deferred_sequence is not None:
            return 0
        session_id = execution.session.session_id
        try:
            claimed = self.core.store.claim_child_messages(
                session_id,
                execution.run_id,
                revision_id=self.core.revision_id,
            )
            self.core.notify_commit()
        except StoreConflict:
            return 0
        if not claimed:
            # Ineligible (another run/revision/status) or no rows: keep this execution's
            # snapshot where it is; the next commit validates/reconciles the parked prefix.
            return 0
        # The claim inserts continuation_started at the current tip but returns only its own
        # rows; any control event committed before it (typically the atomic child fact +
        # notice) is durable but absent from that return. Reconcile the whole bounded range
        # to the post-claim tip, allowing exactly the claimed sequences.
        current = self.core.store.session(session_id)
        self.core._commit_pipeline.reconcile_execution_events(
            execution,
            upto=current.last_event_sequence,
            session=current,
            parked=True,
            allowed_sequences=frozenset(event.sequence for event in claimed),
        )
        return len(claimed)

    def _first_child_message(self, session_id: str) -> ContinuationRecord | None:
        """The oldest eligible core child message, excluding implicit stopped-session wakeups."""
        interrupted = self.core.store.session(session_id).status is SessionStatus.INTERRUPTED
        for records in (
            self.core.store.claimed_continuations(session_id, limit=100),
            self.core.store.pending_continuations(session_id, limit=100),
        ):
            for record in records:
                if record.owner == CHILD_MESSAGE_OWNER:
                    if interrupted and record.payload.get("kind") == "child_finished":
                        continue
                    return record
        return None
