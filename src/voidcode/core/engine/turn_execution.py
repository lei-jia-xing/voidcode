"""Governed turn execution loop and provider turn streaming."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Protocol
from uuid import uuid4

from ...sdk.client import BudgetExhaustion
from ...sdk.codec import (
    decode_provider_result,
    decode_tool_call,
    encode_provider_result,
    encode_tool_call,
)
from ...sdk.hooks import StopCause
from ...sdk.json_values import json_wire_object
from ...sdk.observed_effects import Change
from ...sdk.provider import (
    ProviderDoneReason,
    ProviderExecutionError,
    ProviderMessage,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from ...sdk.services import PluginBudget, SessionUsageReport
from ...sdk.tools import ToolCall, ToolDefinition, ToolFailure, ToolResult
from ..activation import Admission, ExecutionConfig
from ..cancellation import Cancellation
from ..events import EventType
from ..execution import _Execution, committed_event, wire_object
from ..plugins import PluginRuntime
from ..storage.store import (
    CheckpointKind,
    ContinuationStatus,
    EventDraft,
    ObserverReceiptRecord,
    RootBudgetExhausted,
    SessionRecord,
    SessionStatus,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StoreConflict,
    StoredEvent,
)
from ..usage import budget_block
from .commit_pipeline import CheckpointBinding, RootBudgetGrant
from .governance import request_window, tool_schema_characters

logger = logging.getLogger(__name__)

#: One truthful, actionable message for a providerless core. Launch, commands, tools, and
#: sessions stay available; only a model turn or governed query fails with this text.
MODEL_CONFIGURATION_GUIDANCE = "no model configured or registered"


def _tool_calls_from_content(content: str, *, provider_name: str, model_name: str) -> tuple[ToolCall, ...]:
    """Decode one whole-message tool content event into turn tool calls."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name=provider_name,
            model_name=model_name,
            message="provider stream tool content was not JSON",
            retryable=False,
        ) from exc
    if isinstance(payload, Mapping) and isinstance(payload.get("tool_calls"), list):
        items = payload["tool_calls"]
    elif isinstance(payload, Mapping):
        items = [payload]
    else:
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name=provider_name,
            model_name=model_name,
            message="provider stream tool content was not an object",
            retryable=False,
        )
    calls: list[ToolCall] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("tool_name"), str) or not item["tool_name"]:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream tool content was malformed",
                retryable=False,
            )
        arguments = item.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream tool content arguments were not an object",
                retryable=False,
            )
        call_id = item.get("tool_call_id")
        if call_id is not None and (not isinstance(call_id, str) or not call_id):
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream tool content id was malformed",
                retryable=False,
            )
        calls.append(ToolCall(tool_name=item["tool_name"], arguments=dict(arguments), tool_call_id=call_id))
    return tuple(calls)


def _tool_call_from_entry(entry: Mapping[str, object]) -> ToolCall:
    """One aggregated lifecycle entry into its turn tool call; malformed entries fail the turn."""
    name = entry.get("tool_name")
    arguments = entry.get("arguments")
    call_id = entry.get("tool_call_id")
    if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name="",
            model_name="",
            message="provider stream ended with an incomplete tool call",
            retryable=False,
        )
    if call_id is not None and (not isinstance(call_id, str) or not call_id):
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name="",
            model_name="",
            message="provider stream tool call id was malformed",
            retryable=False,
        )
    return ToolCall(tool_name=name, arguments=dict(arguments), tool_call_id=call_id)


type CoreRunStatus = SessionStatus | Literal["queued"]


@dataclass(frozen=True, slots=True)
class CoreResult:
    session_id: str
    run_id: str
    revision_id: str
    status: CoreRunStatus
    output: str | None = None
    error: str | None = None
    pending_approval: Mapping[str, object] | None = None
    pending_ask: Mapping[str, object] | None = None
    stop_cause: StopCause | None = None
    budget_exhaustion: BudgetExhaustion | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "status": self.status.value if isinstance(self.status, SessionStatus) else self.status,
            "output": self.output,
            "error": self.error,
            "pending_approval": None if self.pending_approval is None else json_wire_object(self.pending_approval),
        }
        if self.pending_ask is not None:
            payload["pending_ask"] = json_wire_object(self.pending_ask)
        if self.stop_cause is not None:
            payload["stop_cause"] = self.stop_cause
        if self.budget_exhaustion is not None:
            payload["budget_exhaustion"] = asdict(self.budget_exhaustion)
        return payload


class TurnExecutionSnapshot(Protocol):
    @property
    def revision_id(self) -> str: ...
    @property
    def config(self) -> ExecutionConfig: ...
    @property
    def plugin_runtime(self) -> PluginRuntime: ...
    @property
    def native_tools(self) -> Mapping[str, ToolDefinition]: ...
    @property
    def tools(self) -> tuple[ToolDefinition, ...]: ...
    def definition_of(self, name: str) -> ToolDefinition | None: ...
    def validator_of(self, name: str) -> Any | None: ...
    def owner_of(self, tool_name: str) -> str: ...


class TurnExecutionHost(Protocol):
    @property
    def revision_id(self) -> str: ...
    @property
    def default_abort(self) -> Cancellation: ...
    def active_abort(self, session_id: str) -> Cancellation | None: ...
    @property
    def snapshot(self) -> TurnExecutionSnapshot: ...
    def dispatch(self, description: str) -> AbstractContextManager[TurnExecutionSnapshot]: ...
    def session_read_only(self, session_id: str) -> bool: ...
    def visible_tools(self, execution: _Execution, snapshot: TurnExecutionSnapshot) -> tuple[ToolDefinition, ...]: ...
    def denial(self, call: ToolCall, definition: ToolDefinition, *, read_only: bool) -> str | None: ...
    def gate_denial(self, execution: _Execution, snapshot: TurnExecutionSnapshot, call: ToolCall, *, readonly: bool) -> str | None: ...
    def needs_approval(self, definition: ToolDefinition) -> bool: ...
    def prepare_activation_target(self) -> tuple[str, Admission]: ...
    def pending_activation_request(self, execution: _Execution) -> Mapping[str, object] | None: ...
    def activate(self, *, session_id: str, source: str, request_id: str, target_revision_id: str) -> Mapping[str, object]: ...
    def session_budgets(self, session_id: str) -> Sequence[Any]: ...
    def get_session_usage(
        self,
        session_id: str,
        *,
        since_sequence: int | None = None,
        session: SessionRecord | None = None,
    ) -> SessionUsageReport: ...
    def cancel_pending_continuations(self, session_id: str, reason: str) -> None: ...
    def merge_child_messages(self, execution: _Execution) -> None: ...
    def lifecycle_events(
        self,
        execution: _Execution,
        snapshot: TurnExecutionSnapshot,
        kind: Literal["session_start", "session_end"],
        *,
        status: Literal["completed", "failed", "interrupted", ""] = "",
        error: str | None = None,
    ) -> tuple[EventDraft, ...]: ...
    def project_messages(
        self,
        execution: _Execution,
        *,
        model: str,
        tool_schema_characters: int,
        allow_pending: bool = False,
    ) -> tuple[ProviderMessage, ...]: ...
    def settle_unsettled_dispatches(self, execution: _Execution) -> None: ...
    def report_provider_progress(
        self,
        session_id: str,
        *,
        run_id: str,
        dispatch_id: str,
        dispatch_sequence: int,
        channel: str,
        text_delta: str | None,
    ) -> None: ...
    def invoke(self, execution: _Execution, call: ToolCall, definition: ToolDefinition) -> CoreResult | None: ...
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
        budget_exhaustion: BudgetExhaustion | None = None,
        staged_observations: tuple[Change, ...] = (),
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
        prefix_events: tuple[EventDraft, ...] = (),
        ask_answer_guard: str | None = None,
    ) -> None: ...
    def interrupt_batch(
        self,
        execution: _Execution,
        *,
        budget_exhaustion: BudgetExhaustion | None = None,
        pinned_run_id: str | None = None,
        enforce_pin: bool = False,
    ) -> None: ...
    def commit(
        self,
        execution: _Execution,
        status: SessionStatus,
        kind: CheckpointKind | None,
        *events: EventDraft,
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
    ) -> tuple[StoredEvent, ...]: ...
    def measured_run_elapsed_fields(self, execution: _Execution) -> dict[str, int]: ...
    def store_session(self, session_id: str) -> SessionRecord: ...
    def store_events(self, session_id: str) -> Sequence[StoredEvent]: ...


class TurnDriver:
    """Governed turn execution loop and provider turn streaming manager."""

    def __init__(self, host: TurnExecutionHost) -> None:
        self._host = host

    def stream_provider_turn(
        self,
        snapshot: TurnExecutionSnapshot,
        execution: _Execution,
        request: ProviderTurnRequest,
        *,
        provider_name: str,
        dispatch_id: str,
        dispatch_sequence: int,
    ) -> ProviderTurnResult:
        """Aggregate one provider stream into propose_turn's turn shape, with progress.

        Single-attempt by contract: once bytes flowed there is no safe resend,
        so this path never retries — the attempt is attempt 1 of the turn.
        """
        numbered = replace(request, attempt=1)
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, object]] = {}
        contents: list[str] = []
        metadata: dict[str, object] = {}
        usage = None
        done_reason: ProviderDoneReason = "unknown"
        finish_reason_reported = False
        saw_done = False
        session_id = execution.session.session_id
        for event in snapshot.plugin_runtime.stream_turn(numbered, provider=provider_name):
            if event.kind == "delta" and event.text:
                if event.channel == "reasoning":
                    reasoning_parts.append(event.text)
                    self._host.report_provider_progress(
                        session_id,
                        run_id=execution.run_id,
                        dispatch_id=dispatch_id,
                        dispatch_sequence=dispatch_sequence,
                        channel="reasoning",
                        text_delta=event.text,
                    )
                else:
                    text_parts.append(event.text)
                    self._host.report_provider_progress(
                        session_id,
                        run_id=execution.run_id,
                        dispatch_id=dispatch_id,
                        dispatch_sequence=dispatch_sequence,
                        channel="text",
                        text_delta=event.text,
                    )
            elif event.kind == "tool_call_start":
                ordinal = event.tool_call_ordinal if event.tool_call_ordinal is not None else len(calls)
                calls[ordinal] = {
                    "tool_call_id": event.tool_call_id,
                    "tool_name": event.tool_name,
                    "arguments": {},
                }
            elif event.kind == "tool_call_end":
                ordinal = event.tool_call_ordinal if event.tool_call_ordinal is not None else len(calls)
                entry = calls.setdefault(ordinal, {})
                if event.tool_call_id is not None:
                    entry["tool_call_id"] = event.tool_call_id
                if event.tool_name is not None:
                    entry["tool_name"] = event.tool_name
                entry["arguments"] = dict(event.parsed_arguments or {})
            elif event.kind == "content" and event.channel == "tool" and event.text:
                contents.append(event.text)
            elif event.kind == "error":
                raise ProviderExecutionError(
                    kind=event.error_kind or "provider_error",
                    provider_name=provider_name,
                    model_name=request.model_name or "unknown",
                    message=event.error or "provider stream failed",
                    retryable=False,
                )
            elif event.kind == "done":
                saw_done = True
                if event.done_reason is not None:
                    done_reason = event.done_reason
                if event.usage is not None:
                    usage = event.usage
                if event.metadata:
                    metadata.update(event.metadata)
                    finish_reason_reported = bool(event.metadata.get("finish_reason_reported", finish_reason_reported))
        if not saw_done:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=request.model_name or "unknown",
                message="provider stream ended without a terminal event",
                retryable=False,
            )
        try:
            tool_calls = tuple(_tool_call_from_entry(entry) for _, entry in sorted(calls.items()))
        except ProviderExecutionError as exc:
            raise ProviderExecutionError(
                kind=exc.kind,
                provider_name=provider_name,
                model_name=request.model_name or "unknown",
                message=exc.message,
                retryable=False,
            ) from exc
        for content in contents:
            tool_calls += _tool_calls_from_content(content, provider_name=provider_name, model_name=request.model_name or "unknown")
        return ProviderTurnResult(
            tool_calls=tool_calls,
            output="".join(text_parts),
            reasoning="".join(reasoning_parts) or None,
            usage=usage,
            done_reason=done_reason,
            finish_reason_reported=finish_reason_reported,
            metadata=metadata or None,
        )

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
            if block.unknown:
                return self.fail(execution, block.message)
            return self.stop(execution, block.message, stop_cause="limit_reached")
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

    def drive(self, execution: _Execution) -> CoreResult:
        while True:
            # The process abort stays on self; the live run abort lives in the registry.
            session_abort = self._host.active_abort(execution.session.session_id)
            default_abort = self._host.default_abort
            if (session_abort and session_abort.cancelled) or default_abort.cancelled:
                cause = (session_abort.stop_cause if session_abort and session_abort.cancelled else "shutdown") or "user_stop"
                reason = (session_abort.reason if session_abort and session_abort.cancelled else default_abort.reason) or "cancelled"
                return self.stop(execution, reason, stop_cause=cause)
            snapshot = self._host.snapshot
            cursor = execution.cursor
            if cursor.batch_sequence is None and cursor.deferred_sequence is None:
                request = self.next_deferred(execution)
                if request is not None:
                    cursor.deferred_sequence = request.sequence
                    raw_depth = request.payload.get("depth", 0)
                    cursor.deferred_depth = raw_depth if isinstance(raw_depth, int) and not isinstance(raw_depth, bool) else 0
                    if cursor.deferred_authority_missing:
                        call = decode_tool_call(wire_object(request.payload["call"], "deferred tool call"))
                        self._host.report(execution, call, ToolFailure(call.tool_name, "background tool request lacks captured origin authorization"))
                        continue
                    try:
                        self._host.commit(
                            execution,
                            SessionStatus.RUNNING,
                            CheckpointKind.READY,
                            reserve_root=True,
                        )
                    except RootBudgetExhausted as exc:
                        exhaustion = BudgetExhaustion(
                            dimension="root_actions",
                            source="root_action_budget",
                            limit=exc.budget,
                            consumed=exc.reserved,
                            remaining=max(0, exc.budget - exc.reserved),
                            continue_with="extend_root_actions",
                        )
                        message = f"root action budget exhausted: consumed {exc.reserved} of {exc.budget}, remaining 0"
                        if request.payload.get("background_action_id") is not None and request.payload.get("result_delivery") == "requester":
                            return self.stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)
                        cursor.deferred_sequence = None
                        cursor.deferred_depth = 0
                        return self.stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)
            if cursor.batch_sequence is None and cursor.deferred_sequence is None:
                # A staged activation applies only here: the whole previous native batch has been
                # reported and committed, so no result can mix generations, and the request is
                # consumed durably in the same transaction as the switch.
                pending_request = self._host.pending_activation_request(execution)
                if pending_request is not None:
                    outcome = self._host.activate(
                        session_id=execution.session.session_id,
                        source="model",
                        request_id=str(pending_request.get("request_id", "")),
                        target_revision_id=str(pending_request.get("target_revision_id", "")),
                    )
                    if outcome["activated"]:
                        # This run is the automatic continuation the activation implied; the
                        # next commit persists the consumed intent.
                        execution.cursor.resume_intent = ""
                    execution.session = self._host.store_session(execution.session.session_id)
                    execution.events = list(self._host.store_events(execution.session.session_id))
                    cursor = execution.cursor
                    snapshot = self._host.snapshot
                if cursor.deferred_only:
                    self._host.commit(
                        execution,
                        SessionStatus.COMPLETED,
                        CheckpointKind.TERMINAL,
                        EventDraft(
                            EventType.RUN_COMPLETED,
                            "core",
                            "plugin",
                            {"output": None, "logical_turn": cursor.logical_turn, **self._host.measured_run_elapsed_fields(execution)},
                        ),
                    )
                    return self.result(execution)
                if cursor.max_steps is not None and cursor.step >= cursor.max_steps:
                    exhaustion = BudgetExhaustion(
                        dimension="provider_turns",
                        source="max_steps",
                        limit=cursor.max_steps,
                        consumed=cursor.step,
                        remaining=max(0, cursor.max_steps - cursor.step),
                        continue_with="resume_with_max_steps",
                    )
                    message = f"provider turn cap exhausted: consumed {cursor.step} of {cursor.max_steps}, remaining 0"
                    return self.stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)

                budget_stop = self.budget_admission(execution)
                if budget_stop is not None:
                    return budget_stop

                # Safe boundary: every member of the previous native batch is reported and
                # committed, so messages addressed to this running session may join THIS run
                # before the next provider dispatch. Never across an in-flight tool call, an
                # approval checkpoint, or a token stream.
                self._host.merge_child_messages(execution)

                with self._host.dispatch("provider") as dispatch_snapshot:
                    if not execution.lifecycle_started:
                        self._host.lifecycle_events(execution, dispatch_snapshot, "session_start")
                        execution.lifecycle_started = True
                    model = dispatch_snapshot.config.model
                    if model is None:
                        return self.fail(execution, MODEL_CONFIGURATION_GUIDANCE)
                    provider_name, separator, model_name = model.partition("/")
                    if not separator or not model_name or provider_name not in dispatch_snapshot.plugin_runtime.providers:
                        return self.fail(execution, "model must name a configured provider/model")
                    # Settle any earlier dispatch of this run that never completed BEFORE
                    # context assembly: observers see unknown usage exactly once, and any
                    # budget they set (even a zero cap) or cancellation they raise is
                    # already durable when the admission checks below read it.
                    self._host.settle_unsettled_dispatches(execution)
                    try:
                        visible_tools = self._host.visible_tools(execution, dispatch_snapshot)
                        messages = self._host.project_messages(
                            execution,
                            model=model,
                            tool_schema_characters=tool_schema_characters(visible_tools),
                        )
                    except ValueError as exc:
                        return self.fail(execution, str(exc))
                    except (OSError, RuntimeError) as exc:
                        # Critical context/admission failure: the run finishes durably
                        # failed before any intent is committed or provider is billed.
                        return self.fail(execution, f"context assembly failed: {type(exc).__name__}: {exc}")

                    effective_abort = session_abort or default_abort
                    request = ProviderTurnRequest(
                        messages=messages,
                        available_tools=visible_tools,
                        raw_model=model,
                        provider_name=provider_name,
                        model_name=model_name,
                        reasoning_effort=dispatch_snapshot.config.reasoning_effort,
                        abort_signal=effective_abort,
                    )

                    dispatch_id = uuid4().hex
                    start_time_ms = int(time.time() * 1000)
                    # Final admission: context assembly may have taken a long RPC, so
                    # re-check cancellation and budgets immediately before the native
                    # call that reserves the root step and bills the provider.
                    late_abort = self.abort_stop(execution, session_abort)
                    if late_abort is not None:
                        return late_abort
                    late_budget = self.budget_admission(execution)
                    if late_budget is not None:
                        return late_budget
                    # A fresh provider dispatch is a new logical turn. The current
                    # native batch keeps this identity (checkpointed), so approvals,
                    # tool reports, and resumes all attribute to the same turn.
                    cursor.logical_turn = uuid4().hex
                    window = request_window(messages, request.available_tools)
                    allocation = execution.projection_allocation
                    allocation_payload: dict[str, object] | None = None
                    if allocation is not None:
                        allocation_payload = dict(allocation.as_payload())
                        allocation_payload["ineffective_tools"] = execution.allocation_ineffective_tools
                    try:
                        intent_events = self._host.commit(
                            execution,
                            SessionStatus.RUNNING,
                            CheckpointKind.READY,
                            EventDraft(
                                EventType.PROVIDER_INTENT,
                                "core",
                                "core",
                                {
                                    "attempt": cursor.step,
                                    "provider": provider_name,
                                    "model": model_name,
                                    "dispatch_id": dispatch_id,
                                    "start_time_ms": start_time_ms,
                                    "logical_turn": execution.cursor.logical_turn,
                                    # Measured inventory of this exact request: a client
                                    # panel reads facts here instead of estimating tokens.
                                    "request_window": window.as_payload(),
                                    # The sizing decision behind the tool projections above.
                                    "projection_allocation": allocation_payload,
                                },
                            ),
                            reserve_root=True,
                        )
                    except RootBudgetExhausted as exc:
                        exhaustion = BudgetExhaustion(
                            dimension="root_actions",
                            source="root_action_budget",
                            limit=exc.budget,
                            consumed=exc.reserved,
                            remaining=max(0, exc.budget - exc.reserved),
                            continue_with="extend_root_actions",
                        )
                        message = f"root action budget exhausted: consumed {exc.reserved} of {exc.budget}, remaining 0"
                        return self.stop(execution, message, stop_cause="limit_reached", budget_exhaustion=exhaustion)
                    intent_seq = next(ev.sequence for ev in intent_events if ev.event_type == EventType.PROVIDER_INTENT)

                    t0 = time.monotonic()
                    try:
                        if dispatch_snapshot.plugin_runtime.provider_supports_streaming(provider_name):
                            turn = self.stream_provider_turn(
                                dispatch_snapshot,
                                execution,
                                request,
                                provider_name=provider_name,
                                dispatch_id=dispatch_id,
                                dispatch_sequence=intent_seq,
                            )
                        else:
                            turn = dispatch_snapshot.plugin_runtime.propose_turn(request, provider=provider_name)
                        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                    except ProviderExecutionError as exc:
                        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                        self._host.commit(
                            execution,
                            SessionStatus.INTERRUPTED,
                            CheckpointKind.INTERRUPTED,
                            EventDraft(
                                EventType.PROVIDER_INTERRUPTED,
                                "core",
                                "core",
                                {
                                    "error": str(exc),
                                    "unknown_usage": True,
                                    "dispatch_sequence": intent_seq,
                                    "dispatch_id": dispatch_id,
                                    "elapsed_ms": elapsed_ms,
                                    "provider": provider_name,
                                    "model": model_name,
                                },
                            ),
                        )
                        # Only a real cancellation may report a user decision; a provider
                        # failure is a service failure, never a user stop.
                        if effective_abort.cancelled:
                            return self.stop(
                                execution,
                                str(exc),
                                stop_cause=effective_abort.stop_cause or "user_stop",
                            )
                        return self.fail(execution, f"provider failed: {exc}")
                    except Exception as exc:
                        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                        self._host.commit(
                            execution,
                            SessionStatus.INTERRUPTED,
                            CheckpointKind.INTERRUPTED,
                            EventDraft(
                                EventType.PROVIDER_INTERRUPTED,
                                "core",
                                "core",
                                {
                                    "error": str(exc),
                                    "unknown_usage": True,
                                    "dispatch_sequence": intent_seq,
                                    "dispatch_id": dispatch_id,
                                    "elapsed_ms": elapsed_ms,
                                    "provider": provider_name,
                                    "model": model_name,
                                },
                            ),
                        )
                        return self.fail(execution, f"provider failed: {type(exc).__name__}: {exc}")

                    cursor.step += 1

                    ids = [call.tool_call_id for call in turn.tool_calls]
                    has_duplicate_or_missing_ids = any(not identity for identity in ids) or len(ids) != len(set(ids))
                    invalid_done_reason = turn.done_reason not in {"stop", "tool_calls", "completed"}

                    model_turn_payload: dict[str, object] = {
                        "result": encode_provider_result(turn),
                        "dispatch_sequence": intent_seq,
                        "dispatch_id": dispatch_id,
                        "elapsed_ms": elapsed_ms,
                        "provider": provider_name,
                        "model": model_name,
                    }
                    if effective_abort.cancelled and turn.done_reason == "cancelled":
                        # A provider may acknowledge cancellation with a typed result
                        # instead of raising. Preserve its measured result/usage, then
                        # settle the actual user stop rather than inventing a failure.
                        self._host.commit(
                            execution,
                            SessionStatus.INTERRUPTED,
                            CheckpointKind.INTERRUPTED,
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                        )
                        return self.stop(
                            execution,
                            effective_abort.reason or "run cancelled",
                            stop_cause=effective_abort.stop_cause or "user_stop",
                        )

                    if has_duplicate_or_missing_ids:
                        self._host.commit(
                            execution,
                            SessionStatus.FAILED,
                            CheckpointKind.TERMINAL,
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            completed_continuation_id=execution.cursor.continuation_id,
                            completed_continuation_status=ContinuationStatus.FAILED,
                        )
                        return self.fail(execution, "provider returned missing/duplicate native call identities")

                    if invalid_done_reason:
                        self._host.commit(
                            execution,
                            SessionStatus.FAILED,
                            CheckpointKind.TERMINAL,
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            completed_continuation_id=execution.cursor.continuation_id,
                            completed_continuation_status=ContinuationStatus.FAILED,
                        )
                        return self.fail(execution, f"provider did not complete its turn: {turn.done_reason}")

                    if turn.tool_calls:
                        # A native batch starts at its first member, and the checkpoint
                        # references this exact appended turn in the same transaction:
                        # a crash can never leave a batch event without its checkpoint ref.
                        cursor.next_call = 0
                        self._host.commit(
                            execution,
                            SessionStatus.RUNNING,
                            CheckpointKind.READY,
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            checkpoint_bindings=(("batch_sequence", EventType.MODEL_TURN),),
                        )
                    else:
                        self._host.commit(
                            execution,
                            SessionStatus.COMPLETED,
                            CheckpointKind.TERMINAL,
                            EventDraft(EventType.MODEL_TURN, "provider", "model", model_turn_payload),
                            EventDraft(
                                EventType.RUN_COMPLETED,
                                "core",
                                "core",
                                {"logical_turn": execution.cursor.logical_turn, **self._host.measured_run_elapsed_fields(execution)},
                            ),
                            *self._host.lifecycle_events(execution, dispatch_snapshot, "session_end", status="completed"),
                            completed_continuation_id=execution.cursor.continuation_id,
                            completed_continuation_status=ContinuationStatus.COMPLETED,
                        )
                        return self.result(execution, output=turn.output, stop_cause="completed")
                    if default_abort.cancelled or (session_abort and session_abort.cancelled):
                        cause = (session_abort.stop_cause if session_abort and session_abort.cancelled else "shutdown") or "user_stop"
                        reason = (session_abort.reason if session_abort and session_abort.cancelled else default_abort.reason) or "cancelled"
                        return self.stop(execution, reason, stop_cause=cause)

            batch, call = self.pending(execution)
            if cursor.deferred_sequence is not None and batch.payload["expected_revision"] != snapshot.revision_id:
                self._host.report(execution, call, ToolFailure(call.tool_name, "requested tool belongs to a different capability revision"))
                continue
            scope = execution.session.tool_allowlist
            if scope is not None and call.tool_name not in scope:
                self._host.report(execution, call, ToolFailure(call.tool_name, "tool is outside this session's profile scope"))
                continue
            if cursor.deferred_origin_tools is not None and call.tool_name not in cursor.deferred_origin_tools:
                self._host.report(execution, call, ToolFailure(call.tool_name, "tool is outside the captured origin scope"))
                continue
            definition = snapshot.definition_of(call.tool_name)
            if definition is None:
                self._host.report(execution, call, ToolFailure(call.tool_name, "tool is not in the active capability revision"))
                continue
            effective_read_only = self._host.session_read_only(execution.session.session_id) or cursor.deferred_origin_readonly is True
            denial = self._host.denial(call, definition, read_only=effective_read_only)
            if denial is None:
                denial = self._host.gate_denial(execution, snapshot, call, readonly=effective_read_only)
            if denial:
                self._host.report(execution, call, ToolFailure(call.tool_name, denial))
                continue
            if cursor.intent_sequence is None and self._host.needs_approval(definition):
                if execution.interaction == "unattended":
                    # D3: an unattended run refuses the approval park at the
                    # park-commit site with a typed failure and continues; the
                    # model sees explicit unsupported, never a hang or a fabricated allow.
                    self._host.report(
                        execution,
                        call,
                        ToolFailure(call.tool_name, f"approval is unsupported while unattended; {call.tool_name} was not executed"),
                    )
                    continue
                payload = {
                    "request_id": uuid4().hex,
                    "batch_sequence": batch.sequence,
                    "call": encode_tool_call(call),
                    "owner": snapshot.owner_of(call.tool_name),
                }
                if call.tool_name in snapshot.native_tools:
                    # Pin the exact candidate this approval is about, before the user is asked:
                    # admission imports nothing, and after approval the already-pinned artifact
                    # is activated, so editing the workspace source in between cannot change it.
                    target_revision_id, _admission = self._host.prepare_activation_target()
                    payload["target_revision_id"] = target_revision_id
                    payload["activation_pending"] = True
                self._host.commit(
                    execution,
                    SessionStatus.WAITING,
                    CheckpointKind.APPROVAL_WAIT,
                    EventDraft(EventType.APPROVAL_REQUESTED, "core", "plugin" if cursor.deferred_sequence is not None else "model", payload),
                    checkpoint_bindings=(("approval_sequence", EventType.APPROVAL_REQUESTED),),
                )
                return self.result(execution, pending_approval=payload)
            stopped = self._host.invoke(execution, call, definition)
            if stopped is not None:
                return stopped

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
