"""Tool execution and suspension domain.

Houses:
- Invoking tools through plugin runtime leases and snapshot dispatch
- Native tool interception (e.g. core reload activation requests)
- Handling tool suspensions (interactive ask parks)
- Building observation and coverage metadata into tool completion reports
- Batch interruption settlement with unconfirmed stop markers
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import uuid4

from ...sdk.ask import AskRequest, AskRequestDraft
from ...sdk.codec import decode_provider_result, encode_tool_call, encode_tool_result
from ...sdk.hooks import StopCause
from ...sdk.invocation import ToolContext
from ...sdk.observed_effects import MAX_OBSERVED_CHANGES
from ...sdk.services import BudgetExhaustion, PluginBudget
from ...sdk.tools import (
    Change,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolFailure,
    ToolResult,
    ToolSuccess,
    UnconfirmedStop,
)
from ..activation_coordinator import ActivationFenced
from ..cancellation import Cancellation
from ..coverage import _target_paths, canonical_path
from ..events import EventType
from ..execution import _Execution, committed_event, wire_object
from ..plugins import PluginRuntime, ToolInvocationOutcome, ToolSuspension
from ..storage.store import (
    CheckpointKind,
    ContinuationStatus,
    EventDraft,
    ObserverReceiptRecord,
    SessionStatus,
    SqliteStore,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
    StoredEvent,
    canonical_json,
)
from .commit_pipeline import CheckpointBinding, RootBudgetGrant

if TYPE_CHECKING:
    from .turn_execution import CoreResult

MAX_MERGED_OBSERVATIONS = MAX_OBSERVED_CHANGES


def cancels_claimed(cancellation: StagedCancellation, claimed_id: str | None, claimed_owner: str | None) -> bool:
    """Whether one staged cancellation targets this claim under the claim's OWN owner."""
    if claimed_id is None or claimed_owner is None:
        return False
    return cancellation.owner == claimed_owner and (cancellation.continuation_id is None or cancellation.continuation_id == claimed_id)


class ToolExecutionSnapshot(Protocol):
    """Snapshot capability surface needed during tool dispatch and invocation."""

    @property
    def revision_id(self) -> str: ...

    @property
    def plugin_runtime(self) -> PluginRuntime: ...

    @property
    def native_tools(self) -> Mapping[str, ToolDefinition]: ...

    @property
    def tools(self) -> tuple[ToolDefinition, ...]: ...

    def definition_of(self, name: str) -> ToolDefinition | None: ...

    def validator_of(self, name: str) -> Any | None: ...

    def owner_of(self, tool_name: str) -> str: ...


class ToolExecutionHost(Protocol):
    """Host requirements for invoking tools, reporting results, and settling batches."""

    @property
    def workspace(self) -> Path: ...

    @property
    def revision_id(self) -> str: ...

    @property
    def store(self) -> SqliteStore: ...

    @property
    def default_abort(self) -> Cancellation: ...

    def dispatch(self, description: str) -> AbstractContextManager[ToolExecutionSnapshot]: ...

    def active_abort(self, session_id: str) -> Cancellation | None: ...

    def session_read_only(self, session_id: str) -> bool: ...

    def visible_tools(self, execution: _Execution, snapshot: ToolExecutionSnapshot) -> Sequence[ToolDefinition]: ...

    def post_tool_hooks(
        self,
        execution: _Execution,
        snapshot: ToolExecutionSnapshot,
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult: ...

    def bound_target(self, execution: _Execution, call: ToolCall) -> str | None: ...

    def prepare_report(self, result: ToolResult) -> tuple[ToolResult, int, bool]: ...

    def pending_call(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]: ...

    def commit(
        self,
        execution: _Execution,
        status: SessionStatus,
        kind: CheckpointKind,
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
    ) -> tuple[StoredEvent, ...]: ...

    def stop(
        self,
        execution: _Execution,
        reason: str,
        *,
        stop_cause: StopCause,
    ) -> CoreResult: ...

    def result(
        self,
        execution: _Execution,
        *,
        pending_ask: Mapping[str, object] | None = None,
    ) -> CoreResult: ...


class ToolExecutionManager:
    """Manages tool execution, suspensions, result reporting, and batch interruption."""

    def __init__(self, host: ToolExecutionHost) -> None:
        self._host = host

    def invoke(self, execution: _Execution, call: ToolCall, definition: ToolDefinition) -> CoreResult | None:
        """Invoke one tool call inside a snapshot dispatch, handling outcomes or suspensions."""
        with self._host.dispatch(f"tool:{call.tool_name}") as snapshot:
            cursor = execution.cursor
            session_abort = self._host.active_abort(execution.session.session_id)
            effective_abort = session_abort or self._host.default_abort
            if effective_abort.cancelled:
                reason = effective_abort.reason or "cancelled"
                return self._host.stop(execution, reason, stop_cause=effective_abort.stop_cause or "user_stop")
            if cursor.intent_sequence is None:
                invocation_id = uuid4().hex
                self._host.commit(
                    execution,
                    SessionStatus.RUNNING,
                    CheckpointKind.READY,
                    EventDraft(
                        EventType.TOOL_INTENT,
                        "core",
                        "plugin" if cursor.deferred_sequence is not None else "model",
                        {
                            "invocation_id": invocation_id,
                            "batch_sequence": cursor.deferred_sequence or cursor.batch_sequence,
                            "call": encode_tool_call(call),
                            "owner": snapshot.owner_of(call.tool_name),
                            "effects": sorted(effect.value for effect in definition.effects),
                            "semantics": [],
                            "replay_policy": definition.effective_replay_policy,
                            "icon": {"glyph": definition.icon.glyph} if definition.icon is not None else None,
                            "logical_turn": execution.cursor.logical_turn,
                            "depth": cursor.deferred_depth,
                        },
                    ),
                    checkpoint_bindings=(("intent_sequence", EventType.TOOL_INTENT),),
                )
            else:
                intent = committed_event(execution, cursor.intent_sequence, EventType.TOOL_INTENT)
                invocation_id = str(intent.payload["invocation_id"])

            if call.tool_name in snapshot.native_tools:
                request_id = uuid4().hex
                target = str(self._host.bound_target(execution, call) or "")
                result: ToolResult = ToolSuccess(
                    call.tool_name,
                    TextOutput("activation request recorded; it is applied after this tool batch finishes"),
                )
                self.report(
                    execution,
                    call,
                    result,
                    staged_events=(
                        EventDraft(
                            EventType.ACTIVATION_REQUESTED,
                            "core",
                            "model",
                            {
                                "request_id": request_id,
                                "source": "model",
                                "run_id": execution.run_id,
                                "logical_turn": execution.cursor.logical_turn,
                                "reason": str(call.arguments.get("reason", "")),
                                "target_revision_id": target,
                            },
                        ),
                    ),
                )
                return None
            current_readonly = self._host.session_read_only(execution.session.session_id)
            origin_readonly = current_readonly or cursor.deferred_origin_readonly is True
            origin_visible_tools = tuple(tool.name for tool in self._host.visible_tools(execution, snapshot))
            context = ToolContext(
                workspace=self._host.workspace,
                session_id=execution.session.session_id,
                run_id=execution.run_id,
                invocation_id=invocation_id,
                revision_id=snapshot.revision_id,
                actor="plugin" if cursor.deferred_sequence is not None else "model",
                logical_turn=execution.cursor.logical_turn,
                abort_signal=effective_abort,
                invocation_depth=cursor.deferred_depth,
                root_action_id=cursor.root_id or None,
                initial_root_budget=cursor.initial_root_budget,
                origin_visible_tools=origin_visible_tools,
                origin_readonly=origin_readonly,
                background_action_id=cursor.deferred_action_id,
            )
            try:
                outcome = snapshot.plugin_runtime.invoke(
                    call,
                    context=context,
                    return_outcome=True,
                    readonly=origin_readonly,
                )
                if not isinstance(outcome, ToolInvocationOutcome):
                    raise TypeError("plugin runtime returned a bare tool result for an outcome request")
                result = outcome.result
                staged_state = outcome.staged_state
                staged_events = outcome.staged_events
                staged_conts = outcome.staged_continuations
                staged_cancels = outcome.staged_cancellations
                staged_receipts = outcome.staged_observer_receipts
                staged_budget = outcome.staged_budget
                staged_wakeups = outcome.staged_wakeups
                staged_wakeup_cancels = outcome.staged_wakeup_cancels
                staged_observations = outcome.staged_observations
            except ToolSuspension as suspension:
                return self.park_suspended_tool(execution, call, snapshot, suspension, effective_abort)
            except ActivationFenced as exc:
                if effective_abort.cancelled:
                    return self._host.stop(execution, str(exc), stop_cause=effective_abort.stop_cause or "user_stop")
                return self._host.stop(execution, str(exc), stop_cause="error")
            except Exception as exc:
                result = ToolFailure(call.tool_name, f"tool failed: {type(exc).__name__}: {exc}")
                staged_state = ()
                staged_events = ()
                staged_conts = ()
                staged_cancels = ()
                staged_budget = None
                staged_receipts = ()
                staged_wakeups = ()
                staged_wakeup_cancels = ()
                staged_observations = ()

            if result.tool_name != call.tool_name:
                return self._host.stop(execution, "tool result identity differs from the dispatched native call", stop_cause="error")

            if effective_abort.cancelled:
                stop_cause = effective_abort.stop_cause or "user_stop"
                if isinstance(result, ToolFailure) and result.execution is not None:
                    # The tool observed its own stop (for example after the request may have
                    # reached an external system): preserve that verdict and its staged work
                    # instead of discarding both and fabricating a generic cancellation.
                    execution.cursor.stop_cause = stop_cause
                    self.report(
                        execution,
                        call,
                        result,
                        interrupted=True,
                        staged_state=staged_state,
                        staged_events=staged_events,
                        staged_conts=staged_conts,
                        staged_cancels=staged_cancels,
                        staged_budget=staged_budget,
                        staged_receipts=staged_receipts,
                        staged_wakeups=staged_wakeups,
                        staged_wakeup_cancels=staged_wakeup_cancels,
                        staged_observations=staged_observations,
                    )
                return self._host.stop(execution, "tool invocation was cancelled", stop_cause=stop_cause)

            result = self._host.post_tool_hooks(execution, snapshot, call, result)

            self.report(
                execution,
                call,
                result,
                staged_state=staged_state,
                staged_events=staged_events,
                staged_conts=staged_conts,
                staged_cancels=staged_cancels,
                staged_budget=staged_budget,
                staged_receipts=staged_receipts,
                staged_wakeups=staged_wakeups,
                staged_wakeup_cancels=staged_wakeup_cancels,
                staged_observations=staged_observations,
            )
            claim_id = execution.cursor.continuation_id
            claim_owner = self._host.store.continuation_owner(claim_id) if claim_id is not None else None
            if any(cancels_claimed(cancellation, claim_id, claim_owner) for cancellation in staged_cancels):
                return self._host.stop(execution, "tool cancelled its own durable work", stop_cause="owner_cancel")
            return None

    def park_suspended_tool(
        self,
        execution: _Execution,
        call: ToolCall,
        snapshot: ToolExecutionSnapshot,
        suspension: ToolSuspension,
        effective_abort: Cancellation,
    ) -> CoreResult | None:
        """Commit one suspended tool's park (event + binding + waiting), or refuse it visibly."""
        cursor = execution.cursor
        staged = suspension.staged
        if effective_abort.cancelled:
            return self._host.stop(execution, "tool invocation was cancelled", stop_cause=effective_abort.stop_cause or "user_stop")
        park = suspension.park
        if park.park_kind != "ask":
            self.report(
                execution,
                call,
                ToolFailure(
                    call.tool_name,
                    "foreground composition is not implemented: the composition slice (driver, rows, terminals) has not landed",
                ),
                staged_state=staged.staged_state,
                staged_events=staged.staged_events,
                staged_conts=staged.staged_continuations,
                staged_cancels=staged.staged_cancellations,
                staged_budget=staged.staged_budget,
                staged_receipts=staged.staged_observer_receipts,
                staged_observations=staged.staged_observations,
            )
            return None
        if cursor.ask_sequence is not None or cursor.ask_request_id is not None:
            live = cursor.ask_request_id if cursor.ask_request_id is not None else "unknown"
            self.report(
                execution,
                call,
                ToolFailure(
                    call.tool_name,
                    f"AskAlreadyParked: session {execution.session.session_id!r} already holds live ask {live!r}; refusing a second suspend",
                ),
                staged_state=staged.staged_state,
                staged_events=staged.staged_events,
                staged_conts=staged.staged_continuations,
                staged_cancels=staged.staged_cancellations,
                staged_budget=staged.staged_budget,
                staged_receipts=staged.staged_observer_receipts,
                staged_observations=staged.staged_observations,
            )
            return None
        if execution.interaction == "unattended":
            self.report(
                execution,
                call,
                ToolFailure(call.tool_name, f"ask is unsupported while unattended; {call.tool_name} was not parked"),
                staged_state=staged.staged_state,
                staged_events=staged.staged_events,
                staged_conts=staged.staged_continuations,
                staged_cancels=staged.staged_cancellations,
                staged_budget=staged.staged_budget,
                staged_receipts=staged.staged_observer_receipts,
                staged_observations=staged.staged_observations,
            )
            return None
        draft = AskRequestDraft.from_payload(wire_object(park.intent.get("draft"), "suspend ask draft"))
        raw_key = park.intent.get("idempotency_key")
        idempotency_key = raw_key if isinstance(raw_key, str) and raw_key else None
        request_id = uuid4().hex
        request = AskRequest(
            request_id=request_id,
            session_id=execution.session.session_id,
            revision_id=self._host.revision_id,
            owner=snapshot.owner_of(call.tool_name),
            title=draft.title,
            questions=draft.questions,
            context=draft.context,
            media=draft.media,
            timeout_ms=draft.timeout_ms,
        )
        envelope: dict[str, object] = {"request": request.as_payload(), "parked_at_ms": int(time.time() * 1000)}
        if idempotency_key is not None:
            envelope["idempotency_key"] = idempotency_key
        staged_budget_updates: list[StagedBudgetUpdate] = []
        if staged.staged_budget == "clear":
            staged_budget_updates.append(StagedBudgetUpdate(owner=snapshot.owner_of(call.tool_name), action="clear"))
        elif isinstance(staged.staged_budget, PluginBudget):
            staged_budget_updates.append(StagedBudgetUpdate(owner=staged.staged_budget.owner, action="set", budget=staged.staged_budget))
        cursor.ask_request_id = request_id
        self._host.commit(
            execution,
            SessionStatus.WAITING,
            CheckpointKind.READY,
            EventDraft(EventType.ASK_REQUESTED, "core", "plugin" if cursor.deferred_sequence is not None else "model", envelope),
            *staged.staged_events,
            staged_state=staged.staged_state,
            staged_continuations=staged.staged_continuations,
            staged_cancellations=staged.staged_cancellations,
            staged_budgets=tuple(staged_budget_updates),
            observer_receipts=staged.staged_observer_receipts,
            checkpoint_bindings=(("ask_sequence", EventType.ASK_REQUESTED),),
        )
        return self._host.result(execution, pending_ask=envelope)

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
        """Report tool completion, observation merges, projection, and commit."""
        if staged_observations:
            seen = {(item.path, item.operation, item.old_path) for item in result.changes}
            extra: list[Change] = []
            for observed in staged_observations:
                key = (observed.path, observed.operation, observed.old_path)
                if key in seen:
                    continue
                seen.add(key)
                extra.append(Change(path=observed.path, operation=observed.operation, old_path=observed.old_path))
                if len(extra) >= MAX_MERGED_OBSERVATIONS:
                    break
            if extra:
                result = replace(result, changes=(*result.changes, *extra))
        if result.steering_context:
            execution.ephemeral_steering.append(result.steering_context)
        cursor = execution.cursor
        deferred = cursor.deferred_sequence is not None
        batch_sequence = cursor.deferred_sequence or cursor.batch_sequence
        request = committed_event(execution, batch_sequence or 0, EventType.TOOL_DEFERRED) if deferred else None
        requester_only = request is not None and request.payload.get("result_delivery") == "requester"
        invocation_id: str | None = None
        intent: StoredEvent | None = None
        if cursor.intent_sequence is not None:
            intent = committed_event(execution, cursor.intent_sequence, EventType.TOOL_INTENT)
            raw_invocation = intent.payload.get("invocation_id")
            invocation_id = raw_invocation if isinstance(raw_invocation, str) else None
        cursor.intent_sequence = None
        cursor.approval_sequence = None
        cursor.ask_sequence = None
        cursor.ask_request_id = None
        with self._host.dispatch(f"tool_definition:{call.tool_name}") as snapshot:
            definition = snapshot.definition_of(call.tool_name)
            snapshot_owner = snapshot.owner_of(call.tool_name) if definition else ""
        declaration = intent.payload if intent is not None else {}
        diagnostic = result.diagnostics if isinstance(result, ToolFailure) else None
        committed_result, result_bytes, projection_content_clipped = self._host.prepare_report(result)
        model_event = next(
            (event for event in reversed(execution.events) if event.sequence == batch_sequence and event.event_type == EventType.MODEL_TURN), None
        )
        metadata: dict[str, object] = {
            "semantics": declaration.get("semantics", []) if intent is not None else [],
            "model": model_event.payload.get("model") if model_event is not None else None,
            "argument_bytes": len(canonical_json(dict(call.arguments)).encode("utf-8")),
            "result_bytes": result_bytes,
            "truncated": result.output.bounds.truncated or (projection_content_clipped and not requester_only),
            "partial": result.output.bounds.partial,
            "error_kind": diagnostic.kind if diagnostic else None,
            "has_retry_guidance": bool(diagnostic and diagnostic.guidance),
            "reads": [],
            "changes": [],
            "owner": declaration.get("owner", snapshot_owner) if intent is not None else snapshot_owner,
            "effects": declaration.get("effects") or (sorted(item.value for item in definition.effects) if definition else []),
        }
        paths = [item.path for item in result.coverage] if isinstance(result, ToolSuccess) else []
        paths.extend(item.path for item in result.changes)
        paths.extend(item.old_path for item in result.changes if item.old_path is not None)
        if definition is not None:
            paths.extend(_target_paths(self._host.workspace, call.arguments, definition.path_argument_keys))
        if isinstance(result, ToolSuccess):
            metadata["reads"] = [
                {
                    **item.as_payload(),
                    "path": canonical_path(self._host.workspace, item.path),
                    "clipped": item.clipped or (projection_content_clipped and not requester_only),
                }
                for item in result.coverage
            ]
        metadata["changes"] = [
            {
                **item.as_payload(),
                "path": canonical_path(self._host.workspace, item.path),
                "old_path": canonical_path(self._host.workspace, item.old_path) if item.old_path is not None else None,
            }
            for item in result.changes
        ]
        metadata["resource_paths"] = sorted({str((self._host.workspace / path).resolve()) for path in paths if "://" not in path})
        if deferred:
            cursor.deferred_sequence = None
            cursor.deferred_depth = 0
            cursor.abandon_batch = False
        else:
            cursor.next_call += 1
            turn = decode_provider_result(
                wire_object(committed_event(execution, batch_sequence or 0, EventType.MODEL_TURN).payload["result"], "model turn")
            )
            if cursor.next_call == len(turn.tool_calls):
                cursor.batch_sequence = None
                cursor.next_call = 0
                cursor.abandon_batch = False
        payload: dict[str, object] = {
            "batch_sequence": batch_sequence,
            "call": encode_tool_call(call),
            "result": encode_tool_result(committed_result),
            "fact_metadata": metadata,
            "icon": declaration.get("icon") if intent is not None else ({"glyph": definition.icon.glyph} if definition and definition.icon else None),
            "logical_turn": execution.cursor.logical_turn,
        }
        if deferred:
            assert request is not None
            payload["deferred"] = True
            payload["request_id"] = request.payload["request_id"]
            payload["depth"] = request.payload["depth"]
            if isinstance(request.payload.get("background_action_id"), str):
                payload["background_action_id"] = request.payload["background_action_id"]
            if requester_only:
                payload["result_delivery"] = "requester"
            if interrupted and requester_only:
                payload["stop_cause"] = execution.cursor.stop_cause or "error"
                if budget_exhaustion is not None:
                    payload["budget_exhaustion"] = asdict(budget_exhaustion)
        if invocation_id is not None:
            payload["invocation_id"] = invocation_id
        report_event = EventDraft(
            EventType.TOOL_INTERRUPTED if interrupted else EventType.TOOL_COMPLETED,
            "core" if interrupted else "tool",
            "plugin" if deferred else "model",
            payload,
        )
        all_events = prefix_events + (report_event,) + staged_events

        staged_budget_updates: list[StagedBudgetUpdate] = []
        if staged_budget == "clear":
            owner = snapshot_owner
            staged_budget_updates.append(StagedBudgetUpdate(owner=owner, action="clear"))
        elif isinstance(staged_budget, PluginBudget):
            staged_budget_updates.append(StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget))

        self._host.commit(
            execution,
            SessionStatus.RUNNING,
            CheckpointKind.READY,
            *all_events,
            staged_state=staged_state,
            staged_continuations=staged_conts,
            staged_cancellations=staged_cancels,
            staged_wakeups=staged_wakeups,
            staged_wakeup_cancels=staged_wakeup_cancels,
            staged_budgets=tuple(staged_budget_updates),
            observer_receipts=staged_receipts,
            pinned_run_id=pinned_run_id,
            enforce_pin=enforce_pin,
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
        """Settle remaining tools in the active batch with unconfirmed stop failure."""
        first = execution.cursor.intent_sequence is not None
        bridge_request = execution.cursor.deferred_action_id is not None and execution.cursor.deferred_sequence is not None
        execution.cursor.abandon_batch = True
        while execution.cursor.batch_sequence is not None or execution.cursor.deferred_sequence is not None:
            _, call = self._host.pending_call(execution)
            message = (
                "execution was interrupted; external effects are unknown; not replayed"
                if first
                else "not executed: prior batch member was interrupted"
            )
            result = (
                ToolFailure(
                    call.tool_name,
                    message,
                    execution=UnconfirmedStop(cancellation_signalled=execution.cursor.stop_cause in ("user_stop", "owner_cancel", "shutdown")),
                )
                if bridge_request and first
                else ToolFailure(call.tool_name, message)
            )
            self.report(
                execution,
                call,
                result,
                interrupted=True,
                budget_exhaustion=budget_exhaustion if bridge_request and first else None,
                pinned_run_id=pinned_run_id,
                enforce_pin=enforce_pin,
            )
            first = False
