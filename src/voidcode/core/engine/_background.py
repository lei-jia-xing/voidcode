"""Background-action driving, waiting, and deferred tool-request settlement.

The registry owns claims and aborts; this mixin exposes the host operations the
registry and the SDK await protocol require, including the cancellation sweep
for requests owned by one stopped action."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import uuid4

from ...sdk.codec import decode_tool_call
from ...sdk.hooks import StopCause
from ...sdk.json_values import json_wire_object
from ..events import EventType
from ..execution import _Cursor, _Execution, wire_object
from ..storage.store import CheckpointKind, ContinuationRecord, EventDraft, SequenceConflict, SessionRecord, SessionStatus
from ._lifecycle import CoreLifecycle


class CoreBackground(CoreLifecycle):
    """Background-action driving, waiting, and deferred tool-request settlement."""

    def drive_background_action(self, claimed: ContinuationRecord, abort: threading.Event) -> None:
        """Delegate one claimed action to the background executor."""
        self._background_execution.drive(claimed, abort)

    def _commit_tool_wait_events(
        self,
        session_id: str,
        expected_session: SessionRecord,
        events: Sequence[EventDraft],
        *,
        status: SessionStatus | str | None = None,
        checkpoint_kind: CheckpointKind | str | None = None,
        checkpoint_payload: Mapping[str, object] | None = None,
        run_id: str | None = None,
    ) -> None:
        target_status = SessionStatus(status) if isinstance(status, str) else (status or expected_session.status)
        target_checkpoint_kind = CheckpointKind(checkpoint_kind) if isinstance(checkpoint_kind, str) else checkpoint_kind
        self.store.commit_staged(
            session_id,
            expected_sequence=expected_session.last_event_sequence,
            expected_run_id=expected_session.run_id,
            run_id=run_id or expected_session.run_id or uuid4().hex,
            revision_id=self.revision_id,
            events=self._commit_pipeline.stamp_fact_owners(events),
            status=target_status,
            checkpoint_kind=target_checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
        )
        self.notify_commit()

    def cancel_background_tool_requests(
        self,
        session_id: str,
        action_id: str,
        *,
        status: Literal["cancelled", "interrupted", "core_lost", "budget_exhausted"],
        stop_cause: StopCause | None,
        reason: str,
    ) -> None:
        while True:
            expected_session = self.store.session(session_id)
            events = list(self.store.events(session_id))
            terminal_request_ids = self._background_execution.terminal_request_ids(events)
            requests = [
                event
                for event in events
                if event.event_type == EventType.TOOL_DEFERRED
                and event.payload.get("background_action_id") == action_id
                and event.payload.get("result_delivery") == "requester"
                and event.payload.get("request_id") not in terminal_request_ids
            ]
            if not requests:
                return
            terminal_events: list[EventDraft] = []
            request_intents: dict[int, bool] = {}
            for request in requests:
                request_id = request.payload.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    continue
                intent_exists = any(
                    event.event_type == EventType.TOOL_INTENT and event.payload.get("batch_sequence") == request.sequence for event in events
                )
                request_intents[request.sequence] = intent_exists
                if status == "cancelled" and intent_exists:
                    continue
                payload: dict[str, object] = {
                    "request_id": request_id,
                    "background_action_id": action_id,
                    "owner": request.payload.get("requester", ""),
                    "revision_id": request.payload.get("expected_revision", ""),
                    "tool_name": decode_tool_call(wire_object(request.payload["call"], "deferred tool call")).tool_name,
                    "status": status,
                    "reason": reason,
                }
                if stop_cause is not None:
                    payload["stop_cause"] = stop_cause
                if intent_exists and status in {"interrupted", "core_lost"}:
                    payload["execution"] = {
                        "cancellation_signalled": stop_cause in {"user_stop", "owner_cancel", "shutdown"},
                        "execution_stopped": False,
                        "side_effect_state": "unknown",
                    }
                terminal_events.append(EventDraft(EventType.TOOL_DEFERRED_CANCELLED, "core", "plugin", payload))
            if not terminal_events:
                return
            checkpoint = self.store.checkpoint(session_id)
            checkpoint_payload: Mapping[str, object] | None = None
            checkpoint_kind: CheckpointKind | None = None
            new_status: SessionStatus | None = None
            terminal_run_id: str | None = None
            if checkpoint is not None and checkpoint.kind is not CheckpointKind.TERMINAL and isinstance(checkpoint.payload, Mapping):
                cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
                current_request = next((request for request in requests if request.sequence == cursor.deferred_sequence), None)
                if (
                    current_request is not None
                    and current_request.payload.get("background_action_id") == action_id
                    and (not request_intents.get(current_request.sequence, False) or status == "core_lost")
                ):
                    approval_id: str | None = None
                    if cursor.approval_sequence is not None:
                        approval = next(
                            (
                                event
                                for event in events
                                if event.sequence == cursor.approval_sequence and event.event_type == EventType.APPROVAL_REQUESTED
                            ),
                            None,
                        )
                        raw_approval_id = None if approval is None else approval.payload.get("request_id")
                        approval_id = raw_approval_id if isinstance(raw_approval_id, str) else None
                    if approval_id is not None:
                        terminal_events.append(
                            EventDraft(
                                EventType.APPROVAL_DECIDED,
                                "core",
                                "core",
                                {"request_id": approval_id, "decision": "deny", "reason": reason},
                            )
                        )
                    run_payload: dict[str, object] = {
                        "reason": reason,
                        "logical_turn": cursor.logical_turn,
                        "deferred_request_id": current_request.payload.get("request_id"),
                        "background_action_id": action_id,
                    }
                    if stop_cause is not None:
                        run_payload["stop_cause"] = stop_cause
                    terminal_events.append(EventDraft(EventType.RUN_INTERRUPTED, "core", "core", run_payload))
                    terminal_run_id = expected_session.run_id or (checkpoint.run_id if checkpoint is not None else None) or uuid4().hex
                    execution = _Execution(expected_session, cursor, events, terminal_run_id)
                    terminal_events.extend(
                        self._governance.lifecycle_events(
                            execution,
                            self.snapshot,
                            "session_end",
                            status="interrupted",
                            error=reason,
                        )
                    )
                    cursor.batch_sequence = None
                    cursor.next_call = 0
                    cursor.intent_sequence = None
                    cursor.approval_sequence = None
                    cursor.deferred_sequence = None
                    cursor.deferred_depth = 0
                    cursor.deferred_action_id = None
                    cursor.deferred_origin_tools = None
                    cursor.deferred_origin_readonly = None
                    cursor.deferred_authority_missing = False
                    checkpoint_payload = cursor.model_dump(mode="json")
                    checkpoint_kind = CheckpointKind.TERMINAL
                    new_status = SessionStatus.INTERRUPTED
            try:
                self._commit_tool_wait_events(
                    session_id,
                    expected_session,
                    terminal_events,
                    status=new_status,
                    checkpoint_kind=checkpoint_kind,
                    checkpoint_payload=checkpoint_payload,
                    run_id=terminal_run_id,
                )
                return
            except SequenceConflict:
                continue

    def cancel_background_action(self, session_id: str, owner: str, action_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel one owner action and only its own queued or in-flight tool requests."""
        return self._background.cancel_action(session_id, owner, action_id, reason)

    def wait_background_actions(
        self, session_id: str, *, action_id: str | None = None, timeout_seconds: float = 300.0
    ) -> tuple[dict[str, object], ...]:
        """Wait until background actions settle and raise if any action failed."""
        return self._background.wait_actions(session_id, action_id=action_id, timeout_seconds=timeout_seconds)

    def background_abort_event(self, action_id: str) -> threading.Event | None:
        return self._background.abort_event(action_id)

    def background_stop_cause(self, action_id: str) -> StopCause:
        return self._background.stop_cause(action_id)

    def background_drain_pending(self, session_id: str) -> None:
        self._background_execution.drain_pending(session_id)

    def background_reconcile_lost(self, session_id: str) -> None:
        self._background_execution.reconcile_lost(session_id)

    def drain_background_pending(self, session_id: str) -> None:
        self._background_execution.drain_pending(session_id)

    def drive_pending(self, session_id: str, *, limit: int = 32) -> None:
        self._run_coordinator.drive_pending(session_id, limit=limit)

    _drive_pending = drive_pending
