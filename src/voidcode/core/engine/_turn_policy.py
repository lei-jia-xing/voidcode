"""Turn policy: budgets, abort checks, deferred calls, and readonly batch eligibility."""

from __future__ import annotations

from collections.abc import Sequence

from ...sdk.codec import decode_provider_result, decode_tool_call
from ...sdk.tools import ToolCall, ToolEffect
from ..cancellation import Cancellation
from ..events import EventType
from ..execution import _Execution, committed_event, wire_object
from ..storage.store import StoredEvent
from ..usage import budget_block
from ._turn_contracts import CoreResult, TurnExecutionSnapshot
from ._turn_terminal import TurnTerminalMixin


class TurnPolicyMixin(TurnTerminalMixin):
    """Turn policy: owner budgets, abort checks, deferred calls, readonly batches."""

    def budget_admission(self, execution: _Execution) -> CoreResult | None:
        """Refuse dispatch when an active owner budget is already satisfied.

        Each metric is checked independently with its own unknown flag: a known
        input total with an unknown output total must still obey an input cap,
        and only the caps whose metric is genuinely unknown fail visibly.
        """
        for budget in self._host.session_budgets(execution.session.session_id):
            report = self._host.get_session_usage(
                execution.session.session_id,
                since_sequence=budget.since_sequence,
                session=execution.session,
            )
            block = budget_block(report, budget)
            if block is None:
                continue
            if block.unknown or block.exhaustion is None:
                return self.fail(execution, block.message)
            return self.stop(execution, block.message, stop_cause="limit_reached", budget_exhaustion=block.exhaustion)
        return None

    def abort_stop(self, execution: _Execution, session_abort: Cancellation | None) -> CoreResult | None:
        """Stop the run when the core or its session was cancelled."""
        if session_abort is not None and session_abort.cancelled:
            return self.stop(execution, session_abort.reason or "cancelled", stop_cause=session_abort.stop_cause or "user_stop")
        default_abort = self._host.default_abort
        if default_abort.cancelled:
            return self.stop(execution, default_abort.reason or "cancelled", stop_cause=default_abort.stop_cause or "shutdown")
        return None

    def pending(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]:
        cursor = execution.cursor
        if cursor.deferred_sequence is not None:
            event = committed_event(execution, cursor.deferred_sequence, EventType.TOOL_DEFERRED)
            return event, decode_tool_call(wire_object(event.payload["call"], "deferred tool call"))
        if cursor.batch_sequence is None:
            raise ValueError("checkpoint has no pending batch")
        event = committed_event(execution, cursor.batch_sequence, EventType.MODEL_TURN)
        turn = decode_provider_result(wire_object(event.payload["result"], "model turn"))
        if cursor.next_call >= len(turn.tool_calls):
            raise ValueError("checkpoint is outside its native batch")
        return event, turn.tool_calls[cursor.next_call]

    def next_deferred(self, execution: _Execution) -> StoredEvent | None:
        """Unsettled requests of this chain, in commit order; no synthetic provider turns."""
        completed_sequences = {
            event.payload.get("batch_sequence")
            for event in execution.events
            if event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED} and event.payload.get("deferred") is True
        }
        terminal_request_ids = {
            event.payload.get("request_id") for event in execution.events if event.event_type == EventType.TOOL_DEFERRED_CANCELLED
        }
        action_id = execution.cursor.deferred_action_id
        return next(
            (
                event
                for event in execution.events
                if event.event_type == EventType.TOOL_DEFERRED
                and event.sequence not in completed_sequences
                and event.payload.get("request_id") not in terminal_request_ids
                and event.payload.get("root_id", "") in {"", execution.cursor.root_id}
                and (
                    event.payload.get("background_action_id") == action_id
                    if action_id is not None
                    else event.payload.get("background_action_id") is None
                )
            ),
            None,
        )

    def _can_run_concurrent_readonly(
        self,
        execution: _Execution,
        snapshot: TurnExecutionSnapshot,
        calls: Sequence[ToolCall],
    ) -> bool:
        scope = execution.session.tool_allowlist
        effective_read_only = self._host.session_read_only(execution.session.session_id) or execution.cursor.deferred_origin_readonly is True
        for call in calls:
            if scope is not None and call.tool_name not in scope:
                return False
            if execution.cursor.deferred_origin_tools is not None and call.tool_name not in execution.cursor.deferred_origin_tools:
                return False
            definition = snapshot.definition_of(call.tool_name)
            if definition is None:
                return False
            if not frozenset(definition.effects).issubset({ToolEffect.READ}):
                return False
            if call.tool_name in snapshot.native_tools:
                return False
            if self._host.denial(call, definition, read_only=effective_read_only) is not None:
                return False
            if self._host.gate_denial(execution, snapshot, call, readonly=effective_read_only) is not None:
                return False
            if self._host.needs_approval(definition) and not self._host.has_approval_policy(
                execution.session.session_id, call.tool_name, call.arguments
            ):
                return False
        return True
