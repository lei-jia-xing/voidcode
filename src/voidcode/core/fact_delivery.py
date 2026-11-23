"""Observer fact settlement and delivery over the committed event stream.

The store owns append-only facts and observer receipts; this module owns the
ordered delivery protocol: which events still need delivery, which owners were
frozen onto each fact when it was produced, when a missing owner is a durable
diagnostic versus an activation blocker, and how staged observer proposals
commit atomically with their receipts. It never executes a turn and never
writes outside the host's existing atomic commit capability.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ..sdk.hooks import ObservedFactKind
from ..sdk.json_values import json_wire_object
from ..sdk.services import PluginBudget
from .events import EventType
from .execution import _Cursor, _Execution
from .facts import FACT_EVENT_TYPES, fact_for_event
from .plugins import PluginRuntime
from .storage.store import (
    CheckpointKind,
    EventDraft,
    ObserverReceiptRecord,
    SessionStatus,
    SqliteStore,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StoreConflict,
    StoredEvent,
)


class FactDeliverySnapshot(Protocol):
    """The capability generation that may observe one fact type."""

    @property
    def plugin_runtime(self) -> PluginRuntime: ...


@dataclass(frozen=True, slots=True)
class BirthAuthorization:
    """The root authorization captured in one child's durable birth record."""

    root_id: str
    root_action_budget: int | None


class FactDeliveryContractError(ValueError):
    """A committed fact's recorded authorization violates the delivery contract."""

    def __init__(self, message: str, *, session_id: str) -> None:
        super().__init__(message)
        self.session_id = session_id


class FactDeliveryHost(Protocol):
    """The core capabilities fact delivery may use, stated explicitly."""

    @property
    def store(self) -> SqliteStore: ...

    @property
    def workspace(self) -> Path: ...

    @property
    def revision_id(self) -> str: ...

    def dispatch(self, description: str) -> AbstractContextManager[FactDeliverySnapshot]: ...

    def birth_record(self, session_id: str) -> BirthAuthorization | None: ...

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
    ) -> tuple[StoredEvent, ...]: ...


class FactDelivery:
    def __init__(self, host: FactDeliveryHost) -> None:
        self._host = host
        # Live delivery is re-entrant: an observer may commit another public fact on
        # the same session. Deferring that commit's delivery back to the active loop
        # keeps observer-visible order equal to durable event order.
        self._local = threading.local()

    def delivering_session(self, session_id: str) -> bool:
        """Whether this thread is currently delivering facts for `session_id`."""
        return getattr(self._local, "depth", 0) > 0 and getattr(self._local, "session_id", None) == session_id

    def settlement_candidates(self, session_id: str) -> list[StoredEvent]:
        """Events needing observer delivery: terminal facts plus unsettled dispatches.

        A dispatch intent with no completion event means the process died after
        requesting a turn; it must settle as an unknown-cost usage fact exactly
        once (receipts dedupe it) instead of leaving consumers with stale totals.
        """
        events = self._host.store.events(session_id)
        completed_dispatches: set[int] = set()
        for event in events:
            if event.event_type in (EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED):
                dispatch_seq = event.payload.get("dispatch_sequence")
                if isinstance(dispatch_seq, (int, float)):
                    completed_dispatches.add(int(dispatch_seq))
        pending: list[StoredEvent] = []
        for event in events:
            if event.event_type == EventType.PROVIDER_INTENT:
                if event.sequence not in completed_dispatches:
                    pending.append(event)
            elif event.event_type in FACT_EVENT_TYPES:
                if event.event_type == EventType.MODEL_QUERY and "eligible_owners" not in event.payload:
                    # Model-query usage predates this fact contract: its recipients
                    # were never frozen, so there is no honest owner list to replay to.
                    continue
                pending.append(event)
        return pending

    def replay_pending(self, *, require_complete: bool = False, defer_to_candidate: frozenset[str] = frozenset()) -> None:
        """Deliver facts whose observer receipts never committed before accepting new work.

        Eligibility comes from each source event's OWN revision (its manifest declarations),
        not from whoever is subscribed now, so a reload can neither re-deliver an old fact to
        a new subscriber nor drop one an eligible owner already received. With
        `require_complete`, an eligible owner that is no longer registered aborts instead of
        being papered over: an activation must never mask an undelivered fact.
        """
        for session in self._host.store.sessions():
            if session.workspace != str(self._host.workspace) or session.active_revision_id != self._host.revision_id:
                continue
            pending = self.settlement_candidates(session.session_id)
            if not pending:
                continue
            checkpoint = self._host.store.checkpoint(session.session_id)
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload)) if checkpoint is not None else _Cursor()
            execution = _Execution(session, cursor, list(self._host.store.events(session.session_id)), session.run_id or uuid4().hex)
            settled = [self._record_unknown_dispatch(execution, ev) if ev.event_type == EventType.PROVIDER_INTENT else ev for ev in pending]
            self.deliver(
                execution,
                sorted(settled, key=lambda event: event.sequence),
                require_complete=require_complete,
                defer_to_candidate=defer_to_candidate,
            )

    def eligible_observers(self, event: StoredEvent) -> tuple[str, ...]:
        """Owners that subscribed this fact type when the event was produced.

        The list is frozen on the durable event, so a restart with changed plugin source can
        neither hand an old fact to a new subscriber nor drop an old one.
        """
        owners = event.payload.get("eligible_owners")
        if not isinstance(owners, (list, tuple)) or not all(isinstance(owner, str) for owner in owners):
            raise ValueError(
                f"cannot deliver event {event.sequence}: it carries no recorded eligible owners; "
                "the recorded fact does not satisfy the current contract"
            )
        return tuple(str(owner) for owner in owners)

    def _observer_gap_event(self, event: StoredEvent, missing: Sequence[str]) -> EventDraft:
        """A durable diagnostic for a fact that its own revision's owner can no longer receive."""
        return EventDraft(
            EventType.OBSERVER_ERROR,
            source="core",
            actor="core",
            payload={
                "owner": ", ".join(missing),
                "error": f"event {event.sequence} was produced by revision {event.revision_id} but its registered owner is not loaded",
                "event_sequence": event.sequence,
            },
        )

    def _has_observer_root(self, execution: _Execution) -> bool:
        """Whether this execution has a recorded authorization observers can lease under."""
        session = execution.session
        if session.parent_session_id is not None:
            return self._host.birth_record(session.session_id) is not None
        return bool(execution.cursor.root_id)

    def _observer_root_authorization(self, execution: _Execution) -> tuple[str, int | None]:
        """Recorded root authorization for observer leases delivering this execution's facts.

        Reads the run's own durable root — the birth record for child sessions, the
        checkpoint cursor otherwise (the same sources `_capture_root_authorization`
        uses) — and never mints a fresh identity. A missing or invalid recorded root
        fails visibly; delivery never proceeds on a rootless lease.
        """
        session = execution.session
        if session.parent_session_id is not None:
            birth = self._host.birth_record(session.session_id)
            if birth is None:
                raise FactDeliveryContractError(
                    f"child session {session.session_id!r} has no captured root authorization",
                    session_id=session.session_id,
                )
            root_id = birth.root_id
            budget = birth.root_action_budget
        else:
            root_id = execution.cursor.root_id
            budget = execution.cursor.initial_root_budget
        if not isinstance(root_id, str) or not root_id or "\0" in root_id:
            raise FactDeliveryContractError(
                f"session {session.session_id!r} has no captured root action identity",
                session_id=session.session_id,
            )
        if budget is not None and (type(budget) is not int or budget < 0):
            raise FactDeliveryContractError(
                f"session {session.session_id!r} has an invalid captured root budget",
                session_id=session.session_id,
            )
        return root_id, budget

    def _deliver_event(
        self,
        execution: _Execution,
        ev: StoredEvent,
        *,
        require_complete: bool = False,
        defer_to_candidate: frozenset[str] = frozenset(),
    ) -> None:
        """Notify exactly the owners this event's revision registered, then persist those receipts."""
        if ev.event_type == EventType.MODEL_QUERY and not self._has_observer_root(execution):
            # A side query can commit before this root session ever ran, so it
            # has no captured root to lease observers under. Keep the receipt
            # absent: replay delivers the same fact exactly once after a run
            # (or a child's birth record) supplies the root.
            return
        fact = fact_for_event(ev, execution.events)
        if fact is None:
            return
        fact_type: ObservedFactKind
        fact_payload: dict[str, object]
        fact_type, fact_payload = fact
        session_id = ev.session_id
        with self._host.dispatch(f"observer:{fact_type}") as snapshot:
            eligible = self.eligible_observers(ev)
            registered = snapshot.plugin_runtime.observer_owners.get(fact_type, ())
            missing = [owner for owner in eligible if owner not in registered and owner not in defer_to_candidate]
            if missing:
                if require_complete:
                    missing_list = ", ".join(missing)
                    raise ValueError(
                        f"cannot activate: event {ev.sequence} ({ev.event_type}) of session {session_id} is still undelivered for {missing_list}"
                    )
                self._host.commit_staged_rebased(
                    execution,
                    status=execution.session.status,
                    kind=None,
                    events=(self._observer_gap_event(ev, missing),),
                )
            active = [o for o in eligible if o in registered and not self._host.store.has_observer_receipt(session_id, ev.sequence, o)]
            if not active:
                return
            logical_turn = str(fact_payload.get("logical_turn", ""))
            root_id, root_budget = self._observer_root_authorization(execution)
            obs_res = snapshot.plugin_runtime.notify_fact(
                fact_type, fact_payload, owners=tuple(active), logical_turn=logical_turn, root_action_id=root_id, initial_root_budget=root_budget
            )
            receipts = tuple(ObserverReceiptRecord(session_id=ev.session_id, event_sequence=ev.sequence, owner=o) for o in active)
            staged_events: list[EventDraft] = []
            staged_budget_updates: list[StagedBudgetUpdate] = []
            if obs_res:
                staged_events.extend(obs_res.staged_events)
                for owner, b_val in obs_res.staged_budgets:
                    if b_val == "clear":
                        staged_budget_updates.append(StagedBudgetUpdate(owner=owner, action="clear"))
                    elif isinstance(b_val, PluginBudget):
                        staged_budget_updates.append(StagedBudgetUpdate(owner=b_val.owner, action="set", budget=b_val))
                for err in obs_res.errors:
                    staged_events.append(
                        EventDraft(
                            EventType.OBSERVER_ERROR,
                            source=f"plugin.{err.get('owner', 'observer')}",
                            actor="plugin",
                            payload={"owner": err.get("owner"), "error": err.get("error")},
                        )
                    )
            self._host.commit_staged_rebased(
                execution,
                status=execution.session.status,
                kind=None,
                events=tuple(staged_events),
                staged_state=obs_res.staged_state if obs_res else (),
                staged_continuations=obs_res.staged_continuations if obs_res else (),
                staged_cancellations=obs_res.staged_cancellations if obs_res else (),
                staged_budgets=tuple(staged_budget_updates),
                observer_receipts=receipts,
            )

    def deliver(
        self,
        execution: _Execution,
        events: Sequence[StoredEvent],
        *,
        require_complete: bool = False,
        defer_to_candidate: frozenset[str] = frozenset(),
        tolerate_conflicts: bool = False,
    ) -> None:
        """One ordered delivery path for live commits, recovery and observer-produced facts."""
        pending = list(events)
        seen: set[int] = set()
        for event in pending:
            if event.sequence in seen or event.event_type not in FACT_EVENT_TYPES:
                continue
            seen.add(event.sequence)
            before_delivery = len(execution.events)
            depth = getattr(self._local, "depth", 0)
            previous_session = getattr(self._local, "session_id", None)
            self._local.depth = depth + 1
            self._local.session_id = execution.session.session_id
            try:
                self._deliver_event(
                    execution,
                    event,
                    require_complete=require_complete,
                    defer_to_candidate=defer_to_candidate,
                )
            except StoreConflict as exc:
                if not tolerate_conflicts:
                    raise
                # The outcome is durable. Leave the receipt absent for recovery,
                # and expose the rejected observer proposal without failing the run.
                try:
                    self._host.commit_staged_rebased(
                        execution,
                        status=execution.session.status,
                        kind=None,
                        events=(
                            EventDraft(
                                EventType.OBSERVER_ERROR,
                                "plugin.observer",
                                "plugin",
                                {"owner": None, "error": f"observer proposals rejected: {exc}"},
                            ),
                        ),
                    )
                except StoreConflict, ValueError:
                    pass
            finally:
                self._local.depth = depth
                self._local.session_id = previous_session
            pending.extend(execution.events[before_delivery:])

    def _record_unknown_dispatch(self, execution: _Execution, intent: StoredEvent) -> StoredEvent:
        committed = self._host.commit_staged_rebased(
            execution,
            status=execution.session.status,
            kind=None,
            events=(
                EventDraft(
                    EventType.PROVIDER_INTERRUPTED,
                    "core",
                    "core",
                    {
                        "dispatch_sequence": intent.sequence,
                        "logical_turn": intent.payload.get("logical_turn", ""),
                        "eligible_owners": list(self.eligible_observers(intent)),
                    },
                ),
            ),
        )
        return committed[0]

    def settle_unsettled_dispatches(self, execution: _Execution) -> None:
        """Settle dispatches of this run that never produced a completion event."""
        completed: set[int] = set()
        for event in execution.events:
            if event.event_type in (EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED):
                dispatch_seq = event.payload.get("dispatch_sequence")
                if isinstance(dispatch_seq, (int, float)):
                    completed.add(int(dispatch_seq))
        for event in execution.events:
            if event.event_type == EventType.PROVIDER_INTENT and event.sequence not in completed:
                settled = self._record_unknown_dispatch(execution, event)
                self.deliver(execution, (settled,))
