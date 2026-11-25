"""Commit, fact, hook, and progress persistence hooks.

All durable writes go through the commit pipeline; this mixin is the facade
surface turn execution, tools, plugins, and observers call. Accounted events
surface failures, diagnostic control facts stay best effort."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from ...sdk.json_values import json_wire_object
from ..events import EventType
from ..execution import _Execution
from ..plugins import LeaseContext
from ..storage.store import (
    CheckpointKind,
    ContinuationStatus,
    EventDraft,
    ObserverReceiptRecord,
    SessionRecord,
    SessionStatus,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedConfirmation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
    StoredEvent,
    canonical_json,
)
from ._lifecycle import CoreLifecycle
from .commit_pipeline import CheckpointBinding, RootBudgetGrant


class CorePersistence(CoreLifecycle):
    """Commit, fact, hook, and progress persistence hooks."""

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

    def commit_hook_events(self, execution: _Execution, events: tuple[EventDraft, ...]) -> None:
        """Append hook bookkeeping events without advancing the execution checkpoint."""
        if events:
            self.commit(execution, execution.session.status, None, *events)

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

    _commit = commit
    _commit_control_event = commit_control_event
