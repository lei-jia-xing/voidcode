"""Atomic staged commit, reconciliation, and public fact delivery pipeline.

This module encapsulates:
- Reconciling in-memory execution event prefixes against SQLite session tips
- Freezing generation-bound observer owner subscriptions onto durable fact events
- Adopting committed event sequences into cursor checkpoint bindings
- Committing staged transactions rebased onto fresh tips with SequenceConflict retry
- Staging and committing non-turn public facts and deliver them to observers
- Delivering facts committed by non-turn transactions under re-entrancy protection
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import uuid4

from ...sdk.json_values import json_wire_object
from ..events import EventType
from ..execution import _Cursor, _Execution
from ..facts import FACT_EVENT_TYPES
from ..storage.store import (
    CheckpointKind,
    ContinuationStatus,
    EventDraft,
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
    StoreConflict,
    StoredEvent,
)
from .interactive_ask import is_parked_intervening


@dataclass(frozen=True, slots=True)
class CheckpointBinding:
    """A field-to-event-type binding adopting committed event sequences into the cursor."""

    field_name: str
    event_type: str


@dataclass(frozen=True, slots=True)
class RootBudgetGrant:
    """Transfer of root action budget to an admitted owner."""

    owner: str
    amount: int
    source_run_id: str

    def as_tuple(self) -> tuple[str, int, str]:
        return (self.owner, self.amount, self.source_run_id)


@dataclass(frozen=True, slots=True)
class StagedTurnBatch:
    """Input batch for an atomic turn commit."""

    status: SessionStatus
    kind: CheckpointKind | None
    events: Sequence[EventDraft] = ()
    staged_state: Sequence[StagedStateUpdate] = ()
    staged_continuations: Sequence[StagedContinuationDraft] = ()
    staged_cancellations: Sequence[StagedCancellation] = ()
    staged_budgets: Sequence[StagedBudgetUpdate] = ()
    observer_receipts: Sequence[ObserverReceiptRecord] = ()
    completed_continuation_id: str | None = None
    completed_continuation_status: ContinuationStatus = ContinuationStatus.COMPLETED
    reserve_root: bool = False
    root_budget_grant: RootBudgetGrant | tuple[str, int, str] | None = None
    start_turn: bool = False
    checkpoint_bindings: Sequence[CheckpointBinding | tuple[str, str]] = ()
    pinned_run_id: str | None = None
    enforce_pin: bool = False
    ask_answer_guard: str | None = None


@dataclass(frozen=True, slots=True)
class PublicStagedBatch:
    """Input batch for a non-turn public commit with frozen owners and live delivery."""

    session_id: str
    events: Sequence[EventDraft]
    run_id: str | None = None
    status: SessionStatus | None = None
    revision_id: str | None = None
    checkpoint_kind: CheckpointKind | None = None
    checkpoint_payload: Mapping[str, object] | None = None
    staged_state: Sequence[StagedStateUpdate] = ()
    staged_continuations: Sequence[StagedContinuationDraft] = ()
    staged_cancellations: Sequence[StagedCancellation] = ()
    staged_budgets: Sequence[StagedBudgetUpdate] = ()
    staged_confirmations: Sequence[StagedConfirmation] = ()
    observer_receipts: Sequence[ObserverReceiptRecord] = ()
    root_action_id: str | None = None
    initial_root_budget: int | None = None


@dataclass(frozen=True, slots=True)
class CommitResult:
    """Outcome of a durable atomic staged commit."""

    new_events: tuple[StoredEvent, ...]
    session: SessionRecord


class CommitHost(Protocol):
    """Host requirements for committing staged batches and facts."""

    @property
    def store(self) -> SqliteStore: ...

    @property
    def revision_id(self) -> str: ...

    @property
    def observer_owners(self) -> Mapping[str, tuple[str, ...]]: ...

    def notify_commit(self) -> None: ...

    def notify_tool_waiters(self) -> None: ...

    def delivering_session(self, session_id: str) -> bool: ...

    def deliver_facts(
        self,
        execution: _Execution,
        events: Sequence[StoredEvent],
        *,
        tolerate_conflicts: bool = True,
    ) -> None: ...


class CommitPipeline:
    """Commit and staging engine collaborator managing durable atomic persistence."""

    def __init__(self, host: CommitHost) -> None:
        self._host = host

    @staticmethod
    def assert_execution_prefix(execution: _Execution) -> int:
        """Validate that the in-memory events match the execution's session snapshot tip.

        Every commit writes an append-only slice against the latest durable tip; this
        check protects every commit from a duplicated/holed tail without rescanning long runs.
        """
        known = len(execution.events)
        if execution.session.last_event_sequence != known:
            raise ValueError(f"execution session tip {execution.session.last_event_sequence} differs from in-memory prefix {known}")
        if execution.events and execution.events[-1].sequence != known:
            raise ValueError(f"execution event prefix ends at sequence {execution.events[-1].sequence}, expected {known}")
        return known

    def reconcile_execution_events(
        self,
        execution: _Execution,
        *,
        upto: int,
        session: SessionRecord | None = None,
        parked: bool = False,
        allowed_sequences: frozenset[int] = frozenset(),
    ) -> None:
        """Append exactly the durable events missing from the in-memory prefix, never a tail.

        The execution projection is only valid as one unique contiguous prefix. Every
        concurrent publisher appends to SQLite outside the session lock, so all callers must
        reconcile against a bounded target tip and then adopt that same session snapshot.
        A parked-prefix reconcile accepts only control facts that may legally cross a
        checkpoint, plus explicitly claimed sequences from the same running run.
        """
        known = self.assert_execution_prefix(execution)
        if upto < known:
            raise ValueError(f"cannot reconcile backwards: session tip {upto} is below in-memory prefix {known}")
        if session is not None and session.last_event_sequence != upto:
            raise ValueError(f"reconcile target {upto} does not match its session snapshot {session.last_event_sequence}")
        if upto > known:
            events = self._host.store.events(execution.session.session_id, after_sequence=known, limit=upto - known)
            if len(events) != upto - known:
                raise ValueError(f"durable event prefix {known + 1}..{upto} is incomplete")
            for offset, event in enumerate(events, start=1):
                if event.sequence != known + offset:
                    raise ValueError(f"durable event prefix is not contiguous at sequence {event.sequence}")
                if parked and event.sequence not in allowed_sequences and not is_parked_intervening(event):
                    raise ValueError(f"intervening event {event.event_type} violates the parked checkpoint prefix")
            execution.events.extend(events)
        if session is not None:
            execution.session = session

    @staticmethod
    def bind_checkpoint_refs(
        cursor: _Cursor,
        events: Sequence[StoredEvent],
        bindings: Sequence[CheckpointBinding | tuple[str, str]],
    ) -> None:
        """Adopt the durable event references this commit bound into the checkpoint.

        The store wrote exactly these sequences inside its transaction; the
        in-memory cursor must agree before any observer callback reads it.
        """
        for item in bindings:
            bound_field, bound_event_type = (item.field_name, item.event_type) if isinstance(item, CheckpointBinding) else item
            sequence = next((ev.sequence for ev in events if ev.event_type == bound_event_type), None)
            if sequence is None:
                raise ValueError(f"checkpoint binding {bound_field!r} has no committed {bound_event_type!r} event")
            setattr(cursor, bound_field, sequence)

    def stamp_fact_owners(self, events: Sequence[EventDraft]) -> tuple[EventDraft, ...]:
        """Freeze the fact subscriptions of the producing generation onto each fact event.

        Delivery eligibility must survive a restart with changed plugin source: the owners that
        were registered when the fact was written stay its only eligible recipients, forever.
        """
        owners = self._host.observer_owners
        stamped: list[EventDraft] = []
        for event in events:
            fact_type = "usage" if event.event_type == EventType.PROVIDER_INTENT else FACT_EVENT_TYPES.get(event.event_type)
            if fact_type is None or "eligible_owners" in event.payload:
                stamped.append(event)
                continue
            stamped.append(
                EventDraft(
                    event.event_type,
                    event.source,
                    event.actor,
                    {**event.payload, "eligible_owners": list(owners.get(fact_type, ()))},
                )
            )
        return tuple(stamped)

    def deliver_committed_facts(
        self,
        session_id: str,
        *,
        after_sequence: int,
        upto_sequence: int,
        run_id: str,
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
    ) -> None:
        """Deliver public facts committed by a non-turn transaction under the normal rules."""
        if self._host.delivering_session(session_id):
            # An active same-session delivery loop will append this commit to its
            # pending queue after the current observer returns, preserving event order.
            return
        session = self._host.store.session(session_id)
        checkpoint = self._host.store.checkpoint(session_id)
        cursor = (
            _Cursor.model_validate(json_wire_object(checkpoint.payload))
            if checkpoint is not None
            else _Cursor(root_id=root_action_id or "", initial_root_budget=initial_root_budget)
        )
        events = list(self._host.store.events(session_id, limit=session.last_event_sequence)) if session.last_event_sequence else []
        execution = _Execution(session, cursor, events, run_id)
        committed = tuple(event for event in events if after_sequence < event.sequence <= upto_sequence)
        if committed:
            self._host.deliver_facts(execution, committed, tolerate_conflicts=True)

    def commit_staged_batch(
        self,
        execution: _Execution,
        batch: StagedTurnBatch,
    ) -> tuple[StoredEvent, ...]:
        """Commit against the fresh session tip, validating any intervening parked events first."""
        events = self.stamp_fact_owners(batch.events)
        if batch.enforce_pin and execution.run_id != batch.pinned_run_id:
            raise StoreConflict(f"execution run {execution.run_id!r} does not match bound run {batch.pinned_run_id!r}")
        for attempt in range(16):
            current = self._host.store.session(execution.session.session_id)
            if batch.enforce_pin and current.run_id != batch.pinned_run_id:
                raise StoreConflict(f"session run {current.run_id!r} does not match bound run {batch.pinned_run_id!r}")
            self.reconcile_execution_events(
                execution,
                upto=current.last_event_sequence,
                session=current,
                parked=True,
            )
            before = len(execution.events)
            previous = current
            root_id = execution.cursor.root_id
            root_reservation: tuple[str, int | None] | None = None
            if batch.reserve_root and root_id:
                root_reservation = (root_id, execution.cursor.initial_root_budget)
            stamped_continuations = tuple(
                draft if draft.root_id is not None or not root_id else replace(draft, root_id=root_id) for draft in batch.staged_continuations
            )
            grant_tuple = batch.root_budget_grant.as_tuple() if isinstance(batch.root_budget_grant, RootBudgetGrant) else batch.root_budget_grant
            raw_bindings = tuple((b.field_name, b.event_type) if isinstance(b, CheckpointBinding) else b for b in batch.checkpoint_bindings)
            try:
                committed = self._host.store.commit_staged(
                    previous.session_id,
                    expected_sequence=previous.last_event_sequence,
                    expected_run_id=previous.run_id,
                    run_id=execution.run_id,
                    revision_id=self._host.revision_id,
                    events=tuple(events),
                    status=batch.status,
                    checkpoint_kind=batch.kind,
                    checkpoint_payload=execution.cursor.model_dump() if batch.kind is not None else None,
                    root_reservation=root_reservation,
                    root_action_id=root_id or None,
                    root_action_budget=execution.cursor.initial_root_budget,
                    root_budget_grant=grant_tuple,
                    staged_state=batch.staged_state,
                    staged_continuations=stamped_continuations,
                    staged_cancellations=batch.staged_cancellations,
                    staged_budgets=batch.staged_budgets,
                    observer_receipts=batch.observer_receipts,
                    completed_continuation_id=batch.completed_continuation_id,
                    completed_continuation_status=batch.completed_continuation_status,
                    start_turn=batch.start_turn,
                    checkpoint_bindings=raw_bindings,
                    ask_answer_guard=batch.ask_answer_guard,
                )
                self._host.notify_commit()
            except SequenceConflict:
                if attempt < 15:
                    continue
                raise
            self.reconcile_execution_events(execution, upto=committed.last_event_sequence, session=committed)
            new_events = tuple(execution.events[before:])
            self.bind_checkpoint_refs(execution.cursor, new_events, batch.checkpoint_bindings)
            return new_events
        raise AssertionError("unreachable commit retry loop")

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
        batch = StagedTurnBatch(
            status=status,
            kind=kind,
            events=events,
            staged_state=staged_state,
            staged_continuations=staged_continuations,
            staged_cancellations=staged_cancellations,
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
        return self.commit_staged_batch(execution, batch)

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
        known_before = len(execution.events)
        batch = StagedTurnBatch(
            status=status,
            kind=kind,
            events=events,
            staged_state=staged_state,
            staged_continuations=staged_continuations,
            staged_cancellations=staged_cancellations,
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
        new_events = self.commit_staged_batch(execution, batch)
        self._host.notify_tool_waiters()
        self._host.deliver_facts(execution, execution.events[known_before:], tolerate_conflicts=True)
        return new_events

    def commit_public_batch(self, batch: PublicStagedBatch) -> SessionRecord:
        """Commit a public event batch with frozen fact owners and live delivery."""
        session = self._host.store.session(batch.session_id)
        before = session.last_event_sequence
        resolved_run_id = batch.run_id if batch.run_id is not None else (session.run_id or uuid4().hex)
        resolved_status = batch.status if batch.status is not None else session.status
        revision_id = batch.revision_id if batch.revision_id is not None else self._host.revision_id
        committed = self._host.store.commit_staged(
            batch.session_id,
            expected_sequence=before,
            expected_run_id=session.run_id,
            run_id=resolved_run_id,
            revision_id=revision_id,
            events=self.stamp_fact_owners(batch.events),
            status=resolved_status,
            checkpoint_kind=batch.checkpoint_kind,
            checkpoint_payload=batch.checkpoint_payload,
            staged_state=batch.staged_state,
            staged_continuations=batch.staged_continuations,
            staged_cancellations=batch.staged_cancellations,
            staged_budgets=batch.staged_budgets,
            staged_confirmations=batch.staged_confirmations,
            observer_receipts=batch.observer_receipts,
        )
        self._host.notify_commit()
        self.deliver_committed_facts(
            batch.session_id,
            after_sequence=before,
            upto_sequence=committed.last_event_sequence,
            run_id=resolved_run_id,
            root_action_id=batch.root_action_id,
            initial_root_budget=batch.initial_root_budget,
        )
        return committed

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
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        staged_confirmations: Sequence[StagedConfirmation] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
    ) -> SessionRecord:
        """Commit a public event transaction with frozen fact owners and live delivery."""
        batch = PublicStagedBatch(
            session_id=session_id,
            events=events,
            run_id=run_id,
            status=status,
            revision_id=revision_id,
            checkpoint_kind=checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
            staged_state=staged_state,
            staged_continuations=staged_continuations,
            staged_cancellations=staged_cancellations,
            staged_budgets=staged_budgets,
            staged_confirmations=staged_confirmations,
            observer_receipts=observer_receipts,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
        )
        return self.commit_public_batch(batch)

    def commit_public_fact(
        self,
        session_id: str,
        event: EventDraft,
        *,
        run_id: str | None = None,
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
    ) -> SessionRecord:
        """Commit one public fact with the same stamping and delivery as a staged transaction."""
        return self.commit_public_staged(
            session_id,
            run_id=run_id,
            events=(event,),
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
        )
