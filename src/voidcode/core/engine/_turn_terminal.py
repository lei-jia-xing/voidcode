"""Terminal turn outcomes: cancellation stop, failure, and result construction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Literal

from ...sdk.client import BudgetExhaustion
from ...sdk.codec import decode_tool_call
from ...sdk.hooks import StopCause
from ..events import EventType
from ..execution import _Execution, committed_event, wire_object
from ..storage.store import CheckpointKind, ContinuationStatus, EventDraft, SessionStatus, StoreConflict, StoredEvent
from ._turn_contracts import CoreResult, TurnDriverState


class TurnTerminalMixin(TurnDriverState):
    """Terminal turn outcomes: stop, fail, and result construction."""

    def stop(
        self,
        execution: _Execution,
        message: str,
        *,
        stop_cause: StopCause = "user_stop",
        budget_exhaustion: BudgetExhaustion | None = None,
    ) -> CoreResult:
        """Stop a run without replaying an abandoned native batch."""
        cursor = execution.cursor
        cursor.stop_cause = stop_cause
        deliberate = stop_cause in ("user_stop", "owner_cancel")
        bridge_request: StoredEvent | None = None
        bridge_action_id: str | None = None
        bridge_request_id: str | None = None
        if cursor.deferred_sequence is not None and cursor.deferred_action_id is not None:
            bridge_request = committed_event(execution, cursor.deferred_sequence, EventType.TOOL_DEFERRED)
            if bridge_request.payload.get("background_action_id") != cursor.deferred_action_id:
                raise StoreConflict("deferred request action identity changed before stop")
            bridge_action_id = cursor.deferred_action_id
            bridge_request_id = str(bridge_request.payload["request_id"])
        bridge_not_dispatched = bridge_request is not None and cursor.intent_sequence is None
        if (
            (deliberate or bridge_request is not None)
            and not bridge_not_dispatched
            and (cursor.batch_sequence is not None or cursor.deferred_sequence is not None)
        ):
            self._host.interrupt_batch(execution, budget_exhaustion=budget_exhaustion)
        checkpoint_kind = CheckpointKind.TERMINAL if deliberate else CheckpointKind.INTERRUPTED
        self._host.cancel_pending_continuations(execution.session.session_id, "core")
        stop_payload: dict[str, object] = {
            "reason": message,
            "stop_cause": stop_cause,
            "logical_turn": cursor.logical_turn,
            **({"budget_exhaustion": asdict(budget_exhaustion)} if budget_exhaustion is not None else {}),
            **self._host.measured_run_elapsed_fields(execution),
        }
        if bridge_request_id is not None and bridge_action_id is not None:
            stop_payload["deferred_request_id"] = bridge_request_id
            stop_payload["background_action_id"] = bridge_action_id
        stop_event = EventDraft(EventType.RUN_INTERRUPTED, "core", "core", stop_payload)
        terminal_events: list[EventDraft] = []
        if bridge_not_dispatched:
            if budget_exhaustion is not None and stop_cause == "limit_reached":
                terminal_status: Literal["cancelled", "interrupted", "budget_exhausted"] = "budget_exhausted"
            elif stop_cause in ("user_stop", "owner_cancel"):
                terminal_status = "cancelled"
            else:
                terminal_status = "interrupted"
            terminal_payload: dict[str, object] = {
                "request_id": bridge_request_id,
                "background_action_id": bridge_action_id,
                "owner": bridge_request.payload.get("requester", ""),
                "revision_id": bridge_request.payload.get("expected_revision", ""),
                "tool_name": decode_tool_call(wire_object(bridge_request.payload["call"], "deferred tool call")).tool_name,
                "status": terminal_status,
                "reason": message,
                "stop_cause": stop_cause,
            }
            if budget_exhaustion is not None:
                terminal_payload["budget_exhaustion"] = asdict(budget_exhaustion)
            terminal_events.append(EventDraft(EventType.TOOL_DEFERRED_CANCELLED, "core", "plugin", terminal_payload))
            cursor.deferred_sequence = None
            cursor.deferred_depth = 0
            cursor.deferred_action_id = None
            cursor.deferred_origin_tools = None
            cursor.deferred_origin_readonly = None
            cursor.deferred_authority_missing = False
            cursor.batch_sequence = None
            cursor.next_call = 0
            cursor.intent_sequence = None
            cursor.approval_sequence = None
        terminal_events.append(stop_event)
        # A stop is this run's terminal event too: the hook outcome rides the same atomic
        # commit as RUN_INTERRUPTED, and the durable per-run guard prevents a duplicate
        # end when this session is later resumed under a new run identity.
        terminal_events.extend(
            self._host.lifecycle_events(
                execution,
                self._host.snapshot,
                "session_end",
                status="interrupted",
                error=message,
            )
        )
        if deliberate and cursor.continuation_id is not None:
            self._host.commit(
                execution,
                SessionStatus.INTERRUPTED,
                checkpoint_kind,
                *terminal_events,
                completed_continuation_id=cursor.continuation_id,
                completed_continuation_status=ContinuationStatus.CANCELLED,
            )
        else:
            self._host.commit(execution, SessionStatus.INTERRUPTED, checkpoint_kind, *terminal_events)
        return self.result(
            execution,
            error=message,
            stop_cause=stop_cause,
            budget_exhaustion=budget_exhaustion,
        )

    def fail(self, execution: _Execution, message: str) -> CoreResult:
        execution.cursor.stop_cause = "error"
        self._host.commit(
            execution,
            SessionStatus.FAILED,
            CheckpointKind.TERMINAL,
            EventDraft(
                EventType.RUN_FAILED,
                "core",
                "core",
                {"error": message, "logical_turn": execution.cursor.logical_turn, **self._host.measured_run_elapsed_fields(execution)},
            ),
            *self._host.lifecycle_events(execution, self._host.snapshot, "session_end", status="failed", error=message),
            completed_continuation_id=execution.cursor.continuation_id,
            completed_continuation_status=ContinuationStatus.FAILED,
        )
        return self.result(execution, error=message, stop_cause="error")

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
        return CoreResult(
            execution.session.session_id,
            execution.run_id,
            self._host.revision_id,
            execution.session.status,
            output=output,
            error=error,
            pending_approval=pending_approval,
            pending_ask=pending_ask,
            stop_cause=stop_cause,
            budget_exhaustion=budget_exhaustion,
        )
