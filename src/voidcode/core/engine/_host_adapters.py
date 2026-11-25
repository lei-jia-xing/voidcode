"""Host-protocol adapters for managers, tools, turns, and projections.

These methods are the explicit narrow surface the engine collaborators consume:
governance decisions, tool invocation and reporting, turn results, context
projection, model queries, and store reads. Bodies stay thin by design; the
real work lives in the owning component."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from ...sdk.client import BudgetExhaustion
from ...sdk.hooks import StopCause
from ...sdk.provider import ProviderAbortSignal, ProviderMessage
from ...sdk.services import PluginBudget, StructuredModelResult
from ...sdk.tools import Change, ToolCall, ToolDefinition, ToolResult
from ..execution import _Execution
from ..storage.store import (
    Checkpoint,
    EventDraft,
    ObserverReceiptRecord,
    SessionRecord,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
    StoredEvent,
)
from ._lifecycle import CoreLifecycle
from .governance import GovernanceSnapshot
from .tool_execution import ToolExecutionSnapshot
from .turn_execution import CoreResult


class CoreHostAdapters(CoreLifecycle):
    """Host-protocol adapters for managers, tools, turns, and projections."""

    def bound_target(self, execution: _Execution, call: ToolCall) -> str | None:
        return self._bound_target(execution, call)

    def settle_parked_cancel(self, session_id: str, reason: str, stop_cause: StopCause) -> None:
        self._settle_parked_cancel(session_id, reason, stop_cause)

    def _get_owner_budget(self, session_id: str, owner: str) -> PluginBudget | None:
        return self.store.owner_budget(session_id, owner)

    def definition_of(self, name: str) -> ToolDefinition | None:
        return self._snapshot.definition_of(name)

    def denial(self, call: ToolCall, definition: ToolDefinition, *, read_only: bool) -> str | None:
        return self._governance.denial(call, definition, read_only=read_only)

    def gate_denial(self, execution: _Execution, snapshot: Any, call: ToolCall, *, readonly: bool) -> str | None:
        return self._governance.gate_denial(execution, snapshot, call, readonly=readonly)

    def needs_approval(self, definition: ToolDefinition) -> bool:
        return self._governance.needs_approval(definition)

    def visible_tools(self, execution: _Execution, snapshot: GovernanceSnapshot) -> tuple[ToolDefinition, ...]:
        return self._governance.visible_tools(execution, snapshot)

    def post_tool_hooks(
        self,
        execution: _Execution,
        snapshot: ToolExecutionSnapshot,
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        return self._governance.post_tool_hooks(execution, snapshot, call, result)

    def visible_tool_names_for_session(self, session_id: str, snapshot: Any) -> tuple[str, ...]:
        return self._governance.visible_tool_names_for_session(session_id, snapshot)

    def session_read_only(self, session_id: str) -> bool:
        """Effective read-only for one session: the frozen core policy OR owner overlays."""
        return self._governance.session_read_only(session_id)

    def overlay_active(self, session_id: str, owner: str) -> bool:
        """Whether one owner's read-only overlay is active on this session."""
        self.ensure_open()
        return self.store.session_overlay(session_id, owner) is not None

    def prepare_report(self, result: ToolResult) -> tuple[ToolResult, int, bool]:
        return self._projection.prepare_report(result)

    def pending_call(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]:
        return self._turn_driver.pending(execution)

    def pending(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]:
        return self._turn_driver.pending(execution)

    def stop(
        self,
        execution: _Execution,
        reason: str,
        *,
        stop_cause: StopCause = "user_stop",
        budget_exhaustion: BudgetExhaustion | None = None,
    ) -> CoreResult:
        return self._turn_driver.stop(execution, reason, stop_cause=stop_cause, budget_exhaustion=budget_exhaustion)

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
        return self._turn_driver.result(
            execution,
            output=output,
            error=error,
            pending_approval=pending_approval,
            pending_ask=pending_ask,
            stop_cause=stop_cause,
            budget_exhaustion=budget_exhaustion,
        )

    def drive(self, execution: _Execution) -> CoreResult:
        return self._turn_driver.drive(execution)

    def invoke(self, execution: _Execution, call: ToolCall, definition: ToolDefinition) -> CoreResult | None:
        return self._tool_execution.invoke(execution, call, definition)

    def invoke_readonly_batch(
        self,
        execution: _Execution,
        calls: tuple[ToolCall, ...],
        definitions: tuple[ToolDefinition, ...],
    ) -> CoreResult | None:
        return self._tool_execution.invoke_readonly_batch(execution, calls, definitions)

    def report(
        self,
        execution: _Execution,
        call: ToolCall,
        result: ToolResult,
        *,
        interrupted: bool = False,
        staged_state: tuple[StagedStateUpdate, ...] = (),
        staged_events: tuple[EventDraft, ...] = (),
        staged_conts: tuple[StagedContinuationDraft, ...] = (),
        staged_cancels: tuple[StagedCancellation, ...] = (),
        staged_budget: PluginBudget | None | Literal["clear"] = None,
        staged_receipts: tuple[ObserverReceiptRecord, ...] = (),
        staged_wakeups: tuple[StagedWakeup, ...] = (),
        staged_wakeup_cancels: tuple[StagedWakeupCancel, ...] = (),
        budget_exhaustion: BudgetExhaustion | None = None,
        staged_observations: tuple[Change, ...] = (),
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
        prefix_events: tuple[EventDraft, ...] = (),
        ask_answer_guard: str | None = None,
    ) -> None:
        self._tool_execution.report(
            execution,
            call,
            result,
            interrupted=interrupted,
            staged_state=staged_state,
            staged_events=staged_events,
            staged_conts=staged_conts,
            staged_cancels=staged_cancels,
            staged_budget=staged_budget,
            staged_receipts=staged_receipts,
            staged_wakeups=staged_wakeups,
            staged_wakeup_cancels=staged_wakeup_cancels,
            budget_exhaustion=budget_exhaustion,
            staged_observations=staged_observations,
            pinned_run_id=pinned_run_id,
            enforce_pin=enforce_pin,
            prefix_events=prefix_events,
            ask_answer_guard=ask_answer_guard,
        )

    def interrupt_batch(
        self,
        execution: _Execution,
        *,
        budget_exhaustion: BudgetExhaustion | None = None,
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
    ) -> None:
        self._tool_execution.interrupt_batch(
            execution,
            budget_exhaustion=budget_exhaustion,
            pinned_run_id=pinned_run_id,
            enforce_pin=enforce_pin,
        )

    def project_messages(
        self,
        execution: _Execution,
        *,
        model: str = "",
        tool_schema_characters: int = 0,
        allow_pending: bool = False,
    ) -> tuple[ProviderMessage, ...]:
        return self._context_projection.messages(
            execution,
            model=model,
            tool_schema_characters=tool_schema_characters,
            allow_pending=allow_pending,
        )

    def messages(self, execution: _Execution, *, completed_only: bool, model: str) -> tuple[ProviderMessage, ...]:
        return self._context_projection.messages(execution, completed_only=completed_only, model=model)

    def settle_unsettled_dispatches(self, execution: _Execution) -> None:
        self._fact_delivery.settle_unsettled_dispatches(execution)

    def governed_model_query(
        self,
        session_id: str,
        prompt: str,
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
        owner: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> str:
        """A tool-free side turn, optionally reading the current conversation window.

        Actual provider usage is recorded against the invoking ``owner`` and the
        session, but the question and answer never extend the conversation.
        """
        return self._model_query.query(
            session_id,
            prompt,
            model=model,
            max_output_tokens=max_output_tokens,
            include_context=include_context,
            owner=owner,
            abort_signal=abort_signal,
        )

    def governed_model_query_structured(
        self,
        session_id: str,
        prompt: str,
        result_schema: Mapping[str, object],
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
        owner: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> StructuredModelResult:
        """A tool-free side turn parsed and validated against ``result_schema``.

        Same dispatch, abort, usage event, and tool-calls guard as
        ``governed_model_query``.
        """
        return self._model_query.query_structured(
            session_id,
            prompt,
            result_schema,
            model=model,
            max_output_tokens=max_output_tokens,
            include_context=include_context,
            owner=owner,
            abort_signal=abort_signal,
        )

    def store_sessions(self, workspace: str) -> Sequence[SessionRecord]:
        return self.store.sessions(workspace=workspace)

    def store_session(self, session_id: str) -> SessionRecord:
        return self.store.session(session_id)

    def store_checkpoint(self, session_id: str) -> Checkpoint | None:
        return self.store.checkpoint(session_id)

    def store_events(self, session_id: str, *, after_sequence: int | None = None) -> Sequence[StoredEvent]:
        seq = 0 if after_sequence is None else after_sequence
        return self.store.events(session_id, after_sequence=seq)

    def ensure_root_budget(self, root_id: str, budget: int | None) -> None:
        self.store.ensure_root_budget(root_id, budget)

    def root_budget_state(self, root_id: str) -> tuple[int, int] | None:
        return self.store.root_budget_state(root_id)

    _interrupt_batch = interrupt_batch
