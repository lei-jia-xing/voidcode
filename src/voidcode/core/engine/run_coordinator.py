"""Coordination of session run and resume lifecycles, pending work, and activations."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from ...sdk.client import CLIENT_PROMPT_MAX_BYTES, BudgetExhaustion
from ...sdk.codec import encode_tool_call
from ...sdk.hooks import StopCause
from ...sdk.json_values import json_wire_object
from ...sdk.observed_effects import Change
from ...sdk.provider import ImageAttachment, ProviderMessage
from ...sdk.services import ContinuationResult, PluginBudget
from ...sdk.tools import ToolCall, ToolDefinition, ToolFailure, ToolResult
from ..cancellation import Cancellation
from ..child_contracts import ChildRunStatus
from ..delegation import _child_run_status, _terminal_run_result
from ..events import EventType, child_completion_notice_id
from ..execution import _Cursor, _Execution, committed_event
from ..storage.store import (
    Checkpoint,
    CheckpointKind,
    ContinuationRecord,
    ContinuationStatus,
    EventDraft,
    ObserverReceiptRecord,
    SessionRecord,
    SessionStatus,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
    StoreConflict,
    StoredEvent,
)
from .commit_pipeline import CheckpointBinding, RootBudgetGrant
from .governance import (
    validate_interaction,
    validate_root_action_budget,
    validate_root_action_extension,
    validate_steps,
)
from .interactive_ask import (
    AskBinding,
    AskExpired,
    AskOutcome,
    is_parked_intervening,
)
from .turn_execution import CoreResult, TurnExecutionSnapshot

logger = logging.getLogger(__name__)


def _reserved_cancelled(events: list[StoredEvent]) -> bool:
    """Whether a cancel claimed this child before its first run ever started.

    A reserved child that never ran holds an owner-cancel interruption and no user
    message at all. Any user message proves a real run began, so a late driver must
    not mistake ordinary history for a pre-run cancellation.
    """
    cancelled = False
    for event in events:
        if event.event_type == EventType.USER_MESSAGE:
            return False
        if event.event_type == EventType.RUN_INTERRUPTED and event.payload.get("stop_cause") == "owner_cancel":
            cancelled = True
    return cancelled


class RunCoordinatorHost(Protocol):
    @property
    def revision_id(self) -> str: ...
    @property
    def auto_drain(self) -> bool: ...
    @property
    def workspace(self) -> Path: ...
    @property
    def closed(self) -> bool: ...
    @property
    def snapshot(self) -> TurnExecutionSnapshot: ...

    def ensure_open(self) -> None: ...
    def session_lock(self, session_id: str) -> AbstractContextManager[None]: ...
    @property
    def default_abort(self) -> Cancellation: ...
    def register_run(self, session_id: str, run_id: str, abort: Cancellation) -> None: ...
    def unregister_run(self, session_id: str) -> None: ...
    def has_abort(self, session_id: str) -> bool: ...

    def store_session(self, session_id: str) -> SessionRecord: ...
    def create_session_row(self, session_id: str) -> SessionRecord: ...
    def store_checkpoint(self, session_id: str) -> Checkpoint | None: ...
    def store_events(self, session_id: str, *, after_sequence: int | None = None) -> Sequence[StoredEvent]: ...
    def store_sessions(self, workspace: str) -> Sequence[SessionRecord]: ...
    def ensure_root_budget(self, root_id: str, budget: int | None) -> None: ...
    def root_budget_state(self, root_id: str) -> tuple[int, int] | None: ...

    def admits_fresh_work(self, session: SessionRecord) -> bool: ...
    def check_session(self, session: SessionRecord) -> None: ...
    def queue_followup(
        self,
        session: SessionRecord,
        prompt: str,
        max_steps: int | None,
        *,
        images: tuple[ImageAttachment, ...] = (),
    ) -> CoreResult: ...
    def execute_claimed_followup(self, session_id: str, claim: Any) -> CoreResult: ...
    def claim_pending_followup(self, session_id: str) -> Any | None: ...
    def drain_continuations(
        self,
        session_id: str,
        *,
        limit: int = 32,
        continuation_id: str | None = None,
        _automatic: bool = False,
    ) -> tuple[ContinuationResult, ...]: ...
    def drain_deferred_requests(
        self,
        session_id: str,
        *,
        background_action_id: str | None = None,
        action_abort: threading.Event | None = None,
    ) -> None: ...
    def background_drain_pending(self, session_id: str) -> None: ...
    def background_abort_event(self, action_id: str) -> threading.Event | None: ...
    def background_stop_cause(self, action_id: str) -> StopCause | None: ...
    def background_reconcile_lost(self, session_id: str) -> None: ...

    def captured_child_root_id(self, session_id: str) -> str: ...
    def raw_birth_record(self, session_id: str) -> Mapping[str, object] | None: ...
    def notify_child_state_changed(self) -> None: ...
    def notify_requested(self, child_session: SessionRecord, events: Sequence[StoredEvent]) -> bool: ...
    def publish_child_terminal(
        self,
        parent_session_id: str,
        child_session_id: str,
        *,
        run_id: str,
        status: ChildRunStatus,
        output: str | None,
        error: str | None,
        stop_cause: StopCause | None,
        label: str,
        notify: bool,
        deliver: bool,
        budget_exhaustion: BudgetExhaustion | None,
    ) -> None: ...
    def deliver_child_notice(self, parent_session_id: str, notice_id: str) -> None: ...
    def recover_unstarted_child_drivers(self) -> list[tuple[str, str]]: ...
    def recover_child_publications(self) -> list[tuple[str, str]]: ...
    def first_child_message(self, session_id: str) -> ContinuationRecord | None: ...

    def project_messages(
        self,
        execution: _Execution,
        *,
        model: str = "",
        tool_schema_characters: int = 0,
        allow_pending: bool = False,
    ) -> tuple[ProviderMessage, ...]: ...
    def pending(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]: ...
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
    ) -> None: ...
    def invoke(self, execution: _Execution, call: ToolCall, definition: ToolDefinition) -> CoreResult | None: ...
    def invoke_readonly_batch(
        self,
        execution: _Execution,
        calls: tuple[ToolCall, ...],
        definitions: tuple[ToolDefinition, ...],
    ) -> CoreResult | None: ...
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
    ) -> tuple[StoredEvent, ...]: ...
    def drive(self, execution: _Execution) -> CoreResult: ...

    def ask_binding(self, execution: _Execution, cursor: _Cursor) -> AskBinding: ...
    def ask_binding_expired(self, binding: AskBinding) -> bool: ...
    def adopt_ask_terminal(self, execution: _Execution, binding: AskBinding, outcome: AskOutcome) -> None: ...

    def definition_of(self, name: str) -> ToolDefinition | None: ...
    def session_read_only(self, session_id: str) -> bool: ...
    def visible_tools(self, execution: _Execution, snapshot: TurnExecutionSnapshot) -> tuple[ToolDefinition, ...]: ...
    def denial(self, call: ToolCall, definition: ToolDefinition, *, read_only: bool) -> str | None: ...
    def gate_denial(self, execution: _Execution, snapshot: TurnExecutionSnapshot, call: ToolCall, *, readonly: bool) -> str | None: ...


class RunCoordinator:
    """Manages top-level run, resume, follow-up draining, and startup activation cycles."""

    def __init__(self, host: RunCoordinatorHost) -> None:
        self._host = host

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        max_steps: int | None = None,
        root_action_budget: int | None = None,
        images: tuple[ImageAttachment, ...] = (),
        interaction: Literal["interactive", "unattended"] | None = None,
    ) -> CoreResult:
        return self._run(
            prompt,
            session_id=session_id,
            max_steps=max_steps,
            root_action_budget=root_action_budget,
            prompt_max_bytes=CLIENT_PROMPT_MAX_BYTES,
            images=images,
            interaction=interaction,
        )

    def _run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        max_steps: int | None = None,
        root_action_budget: int | None = None,
        prompt_max_bytes: int,
        images: tuple[ImageAttachment, ...] = (),
        _root_id: str | None = None,
        _refuse_reserved_cancelled: bool = False,
        interaction: Literal["interactive", "unattended"] | None = None,
    ) -> CoreResult:
        # A core-driven child run carries a delegated payload (e.g. a review diff).
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > prompt_max_bytes:
            raise ValueError(f"prompt must be nonempty UTF-8 text within {prompt_max_bytes // 1024} KiB")
        validate_steps(max_steps)
        validate_interaction(interaction)
        validate_root_action_budget(root_action_budget)
        if _root_id is not None and (not isinstance(_root_id, str) or not _root_id or "\0" in _root_id):
            raise ValueError("inherited root action id must be nonempty text without NUL")
        if _root_id is not None and root_action_budget is not None:
            raise ValueError("a child cannot assign a new root action budget")
        if session_id is not None and (not isinstance(session_id, str) or not session_id or len(session_id) > 128 or "\0" in session_id):
            raise ValueError("session_id must be nonempty text within 128 characters without NUL")
        if session_id is not None:
            try:
                active_sess = self._host.store_session(session_id)
                if active_sess.status in (SessionStatus.RUNNING, SessionStatus.WAITING):
                    if root_action_budget is not None:
                        raise ValueError("a queued follow-up cannot assign a new root action budget; resume with an explicit extension")
                    return self._host.queue_followup(active_sess, prompt, max_steps, images=images)
            except KeyError:
                pass
        effective_session_id = uuid4().hex if session_id is None else session_id
        with self._host.session_lock(effective_session_id):
            self._host.ensure_open()
            self._host.default_abort.set_cancelled(False)
            abort = Cancellation()
            run_abort_id = uuid4().hex
            self._host.register_run(effective_session_id, run_abort_id, abort)
            try:
                try:
                    session = self._host.store_session(effective_session_id)
                except KeyError:
                    session = self._host.create_session_row(effective_session_id)
                if session.status in (SessionStatus.RUNNING, SessionStatus.WAITING):
                    if root_action_budget is not None:
                        raise ValueError("a queued follow-up cannot assign a new root action budget; resume with an explicit extension")
                    return self._host.queue_followup(session, prompt, max_steps, images=images)
                if not self._host.admits_fresh_work(session):
                    raise ValueError("unfinished session requires explicit resume")
                self._host.check_session(session)
                root_id = _root_id if _root_id is not None else uuid4().hex
                if session.parent_session_id is not None:
                    if _root_id is None:
                        raise ValueError("child sessions cannot start a new root action; use a parent-authorized child send")
                    birth_root_id = self._host.captured_child_root_id(effective_session_id)
                    if birth_root_id != _root_id:
                        raise ValueError("child run root action identity does not match its captured birth authorization")
                elif _root_id is not None:
                    raise ValueError("an inherited root action identity is only valid for a child session")
                initial_root_budget = root_action_budget
                if _root_id is None:
                    self._host.ensure_root_budget(root_id, root_action_budget)
                else:
                    birth = self._host.raw_birth_record(effective_session_id)
                    if birth is None or birth.get("root_id") != root_id:
                        raise StoreConflict(f"child {effective_session_id!r} is missing its captured root authorization")
                    if "root_action_budget" not in birth:
                        raise StoreConflict(f"child {effective_session_id!r} has no captured root budget state")
                    birth_budget = birth.get("root_action_budget")
                    if birth_budget is not None and (type(birth_budget) is not int or birth_budget < 0):
                        raise StoreConflict(f"child {effective_session_id!r} has an invalid captured root budget")
                    root_state = self._host.root_budget_state(root_id)
                    if birth_budget is None:
                        if root_state is not None:
                            raise StoreConflict("captured uncapped root authorization conflicts with a finite budget row")
                    elif root_state is None or root_state[0] < birth_budget:
                        raise StoreConflict("captured finite root authorization has no matching durable budget row")
                    initial_root_budget = birth_budget
                execution = _Execution(
                    session,
                    _Cursor(root_id=root_id, initial_root_budget=initial_root_budget, max_steps=max_steps),
                    list(self._host.store_events(effective_session_id)),
                    run_abort_id,
                    interaction=interaction,
                )
                if _refuse_reserved_cancelled and _reserved_cancelled(execution.events):
                    # A cancel claimed this reserved child before its first run started
                    # (checked under the session lock, so a late driver can never slip
                    # past the claim): report the cancellation instead of starting work.
                    return CoreResult(
                        effective_session_id,
                        "",
                        self._host.revision_id,
                        SessionStatus.INTERRUPTED,
                        error="child was cancelled before its first run",
                        stop_cause="owner_cancel",
                    )
                if session.status is SessionStatus.INTERRUPTED:
                    # Finish the abandoned stopped prefix on its own cursor first,
                    # then start the new user turn with a fresh cursor: it must not
                    # inherit a stale continuation claim or a spent chain budget.
                    previous = self._host.store_checkpoint(effective_session_id)
                    if previous is not None and isinstance(previous.payload, Mapping):
                        execution.cursor = _Cursor.model_validate(json_wire_object(previous.payload))
                        execution.cursor.step = 0
                        execution.cursor.stop_cause = None
                    if execution.cursor.batch_sequence is not None or execution.cursor.deferred_sequence is not None:
                        # The stopped batch is never replayed: report its members before the new turn.
                        self._host.interrupt_batch(execution)
                    # The new user turn has its own root identity and optional authorization.
                    execution.cursor = _Cursor(root_id=root_id, initial_root_budget=initial_root_budget, max_steps=max_steps)
                user_payload: dict[str, object] = {"content": prompt, "trigger": "user"}
                if max_steps is not None:
                    user_payload["max_steps"] = max_steps
                if root_action_budget is not None:
                    user_payload["root_action_budget"] = root_action_budget
                if images:
                    user_payload["images"] = [img.as_payload() for img in images]
                self._host.commit(
                    execution,
                    SessionStatus.RUNNING,
                    CheckpointKind.READY,
                    EventDraft(
                        EventType.USER_MESSAGE,
                        "client",
                        "user",
                        user_payload,
                    ),
                    start_turn=True,
                )
                result = self._host.drive(execution)
            finally:
                self._host.unregister_run(effective_session_id)
        self._host.drain_deferred_requests(effective_session_id)
        self._host.background_drain_pending(effective_session_id)
        self.drive_pending(effective_session_id)
        return result

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        decision: Literal["allow", "deny"] | None = None,
        max_steps: int | None = None,
        extend_root_actions: int | None = None,
        root_action_grant_id: str | None = None,
        interaction: Literal["interactive", "unattended"] | None = None,
        reason: str | None = None,
        grant_session_policy: bool = False,
    ) -> CoreResult:
        validate_steps(max_steps)
        validate_interaction(interaction)
        validate_root_action_extension(extend_root_actions, root_action_grant_id)
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("reason must be non-empty text")
        if reason is not None and decision != "deny":
            raise ValueError("reason can only be provided when decision is 'deny'")
        if grant_session_policy and decision != "allow":
            raise ValueError("grant_session_policy can only be provided when decision is 'allow'")
        with self._host.session_lock(session_id):
            self._host.ensure_open()
            self._host.default_abort.set_cancelled(False)
            abort = Cancellation()
            run_abort_id = uuid4().hex
            self._host.register_run(session_id, run_abort_id, abort)
            resumed_notice: tuple[str, str] | None = None
            stopped_result: CoreResult | None = None
            try:
                session = self._host.store_session(session_id)
                self._host.check_session(session)
                checkpoint = self._host.store_checkpoint(session_id)
                if checkpoint is None or checkpoint.kind is CheckpointKind.TERMINAL:
                    raise ValueError("session has no resumable checkpoint")
                if (
                    checkpoint.revision_id != self._host.revision_id
                    or checkpoint.run_id != session.run_id
                    or checkpoint.last_event_sequence > session.last_event_sequence
                ):
                    raise ValueError("checkpoint identity does not match the durable session")
                events_after_cp = self._host.store_events(session_id, after_sequence=checkpoint.last_event_sequence)
                for ev in events_after_cp:
                    if not is_parked_intervening(ev):
                        raise ValueError(f"Intervening event {ev.event_type} violates parked checkpoint prefix")
                if "initial_root_budget" not in checkpoint.payload:
                    raise ValueError("checkpoint has no captured root budget state")
                cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
                if not isinstance(cursor.root_id, str) or not cursor.root_id:
                    raise ValueError("checkpoint has no captured root action identity")
                if cursor.deferred_action_id is not None:
                    deferred_action_id = cursor.deferred_action_id
                    action_abort = self._host.background_abort_event(deferred_action_id)
                    if action_abort is not None:
                        abort = Cancellation(
                            action_abort,
                            linked_reason="background action stopped",
                            linked_stop_cause_provider=lambda: self._host.background_stop_cause(deferred_action_id) or "user_stop",
                        )
                        self._host.register_run(session_id, run_abort_id, abort)
                root_state = self._host.root_budget_state(cursor.root_id)
                if cursor.initial_root_budget is None:
                    if root_state is not None:
                        raise StoreConflict("checkpoint captured an uncapped root with a finite budget row")
                elif root_state is None or root_state[0] < cursor.initial_root_budget:
                    raise StoreConflict("checkpoint lost its captured finite root authorization")
                if session.parent_session_id is not None:
                    self._host.captured_child_root_id(session_id)
                if max_steps is not None:
                    cursor.max_steps = max_steps
                execution = _Execution(session, cursor, list(self._host.store_events(session_id)), run_abort_id, interaction=interaction)
                self._host.project_messages(execution, allow_pending=True)
                if cursor.intent_sequence is not None:
                    intent = committed_event(execution, cursor.intent_sequence, EventType.TOOL_INTENT)
                    batch, call = self._host.pending(execution)
                    if intent.payload["batch_sequence"] != batch.sequence or json_wire_object(intent.payload)["call"] != encode_tool_call(call):
                        raise ValueError("pending intent does not match its native batch member")
                approval: StoredEvent | None = None
                if cursor.approval_sequence is not None:
                    approval = committed_event(execution, cursor.approval_sequence, EventType.APPROVAL_REQUESTED)
                    if decision not in {"allow", "deny"} or approval_request_id != approval.payload["request_id"]:
                        raise ValueError("approval resume requires the exact pending request identity and decision")
                    batch, call = self._host.pending(execution)
                    if approval.payload["batch_sequence"] != batch.sequence or json_wire_object(approval.payload)["call"] != encode_tool_call(call):
                        raise ValueError("pending approval does not match its native batch member")
                elif approval_request_id is not None or decision is not None:
                    raise ValueError("session is not waiting for approval")
                if cursor.ask_sequence is not None or cursor.ask_request_id is not None:
                    # An ask park resumes only through answer_ask/cancel_ask: a plain
                    # resume would re-invoke the parked tool. Refuse while live; when
                    # the caller-declared deadline passed, settle the expiry here and
                    # fall through so RUN_RESUMED plus _drive continue the run.
                    if approval_request_id is not None or decision is not None:
                        raise ValueError("session is not waiting for approval")
                    binding = self._host.ask_binding(execution, cursor)
                    if not self._host.ask_binding_expired(binding):
                        raise ValueError(f"session is waiting for ask {cursor.ask_request_id!r}; answer via ask.answer or ask.cancel")
                    self._host.adopt_ask_terminal(execution, binding, AskExpired())
                resume_payload: dict[str, object] = {"max_steps": max_steps, "trigger": "resume"}
                root_budget_grant: tuple[str, int, str] | None = None
                if extend_root_actions is not None:
                    if not cursor.root_id:
                        raise ValueError("checkpoint has no root action identity to extend")
                    if not root_action_grant_id:
                        raise ValueError("extend_root_actions requires root_action_grant_id")
                    resume_payload.update(
                        {
                            "root_id": cursor.root_id,
                            "extend_root_actions": extend_root_actions,
                            "root_action_grant_id": root_action_grant_id,
                        }
                    )
                    root_budget_grant = (cursor.root_id, extend_root_actions, root_action_grant_id)
                self._host.commit(
                    execution,
                    SessionStatus.RUNNING,
                    CheckpointKind.READY,
                    EventDraft(EventType.RUN_RESUMED, "core", "user", resume_payload),
                    root_budget_grant=root_budget_grant,
                )
                if approval is not None:
                    cursor.approval_sequence = None
                    _, call = self._host.pending(execution)
                    policy_rule_id: str | None = None
                    if decision == "allow" and grant_session_policy:
                        policy_rule_id = uuid4().hex
                        self._host.commit(
                            execution,
                            SessionStatus.RUNNING,
                            CheckpointKind.READY,
                            EventDraft(
                                EventType.APPROVAL_POLICY_GRANTED,
                                "client",
                                "user",
                                {
                                    "rule_id": policy_rule_id,
                                    "tool_name": call.tool_name,
                                    "pattern": None,
                                },
                            ),
                        )
                    decided_payload: dict[str, object] = {
                        "request_id": approval_request_id,
                        "decision": decision,
                    }
                    if reason:
                        decided_payload["reason"] = reason
                    if policy_rule_id is not None:
                        decided_payload["policy_rule_id"] = policy_rule_id
                    self._host.commit(
                        execution,
                        SessionStatus.RUNNING,
                        CheckpointKind.READY,
                        EventDraft(EventType.APPROVAL_DECIDED, "client", "user", decided_payload),
                    )
                    if decision == "deny":
                        failure_msg = f"user denied this invocation: {reason}" if reason else "user denied this invocation"
                        self._host.report(execution, call, ToolFailure(call.tool_name, failure_msg))
                    else:
                        definition = self._host.definition_of(call.tool_name)
                        if definition is None:
                            raise ValueError("approved tool is not in the active capability revision")
                        resume_read_only = self._host.session_read_only(execution.session.session_id) or cursor.deferred_origin_readonly is True
                        visible = {tool.name for tool in self._host.visible_tools(execution, self._host.snapshot)}
                        denial = (
                            "tool is outside the current or captured origin scope"
                            if call.tool_name not in visible
                            else self._host.denial(call, definition, read_only=resume_read_only)
                        )
                        if denial is None:
                            # Approval is not authority: the current tool gate is re-asked.
                            denial = self._host.gate_denial(execution, self._host.snapshot, call, readonly=resume_read_only)
                        if denial:
                            self._host.report(execution, call, ToolFailure(call.tool_name, denial))
                        else:
                            stopped = self._host.invoke(execution, call, definition)
                            stopped_result = stopped
                elif cursor.abandon_batch:
                    self._host.interrupt_batch(execution)
                elif cursor.intent_sequence is not None:
                    intent = committed_event(execution, cursor.intent_sequence, EventType.TOOL_INTENT)
                    _, call = self._host.pending(execution)
                    if json_wire_object(intent.payload)["call"] != encode_tool_call(call):
                        raise ValueError("pending intent does not match its native batch member")
                    if intent.payload["replay_policy"] == "never":
                        self._host.interrupt_batch(execution)
                    # A safe read reuses the durable invocation identity below.
                result = stopped_result if stopped_result is not None else self._host.drive(execution)
                if session.parent_session_id is not None and _terminal_run_result(result):
                    child_now = self._host.store_session(session_id)
                    notify = self._host.notify_requested(child_now, self._host.store_events(session_id))
                    self._host.publish_child_terminal(
                        session.parent_session_id,
                        session_id,
                        run_id=result.run_id,
                        status=_child_run_status(result),
                        output=result.output,
                        error=result.error,
                        stop_cause=result.stop_cause,
                        label="",
                        notify=notify,
                        deliver=False,
                        budget_exhaustion=result.budget_exhaustion,
                    )
                    if notify:
                        resumed_notice = (
                            session.parent_session_id,
                            child_completion_notice_id(session.parent_session_id, session_id, result.run_id),
                        )
            finally:
                self._host.unregister_run(session_id)
        if resumed_notice is not None:
            self._host.deliver_child_notice(*resumed_notice)
        self._host.drain_deferred_requests(session_id)
        self._host.background_drain_pending(session_id)
        self.drive_pending(session_id)
        if session.parent_session_id is not None:
            self._host.notify_child_state_changed()
        return result

    def drive_pending(self, session_id: str, *, limit: int = 32) -> None:
        """Drive owned queued follow-up work at a safe boundary, outside the run lock.

        Returns nothing on purpose: `run`/`resume` report the result of the
        operation the caller actually requested. Auto-driven run outcomes stay
        observable through durable events, continuation rows (status, output,
        error), and `drain_continuations`. Only "nothing left to claim" is
        absorbable here; malformed checkpoints and invalid payloads must raise.
        """
        if not self._host.auto_drain:
            return
        for _ in range(max(1, limit)):
            claim = self._host.claim_pending_followup(session_id)
            if claim is not None:
                run_res = self._host.execute_claimed_followup(session_id, claim)
                if run_res.status not in ("completed", "idle"):
                    return
                continue
            try:
                results = self._host.drain_continuations(session_id, limit=1, _automatic=True)
            except StoreConflict:
                # No pending or claimed continuation remains: the chain is done.
                return
            if not results:
                return
            if results[-1].run_status not in ("completed", "idle"):
                return

    def drive_pending_activations(self) -> None:
        """Finish resume intents, child drivers, and child messages left across restart.

        Pending model continuations (for example a goal pursuit) are deliberately not
        claimed here: they stay durable until a user run or explicit drain.
        """
        try:
            for session in self._host.store_sessions(str(self._host.workspace)):
                if self._host.closed:
                    return
                self._host.background_reconcile_lost(session.session_id)
                if self._host.admits_fresh_work(session):
                    self._host.drain_deferred_requests(session.session_id)
                checkpoint = self._host.store_checkpoint(session.session_id)
                if checkpoint is None or not isinstance(checkpoint.payload, Mapping):
                    continue
                cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
                if not cursor.resume_intent:
                    continue
                try:
                    self.consume_resume_intent(session.session_id)
                except Exception:
                    # The intent stays durable, so the next process retries it; never crash the core.
                    continue
        except Exception:
            return
        if not self._host.auto_drain or self._host.closed:
            return
        # An accepted spawn may have lost its driver future at close. Continue the exact
        # captured spawn; unstartable reservations become visible terminal facts.
        try:
            notices = self._host.recover_unstarted_child_drivers()
        except Exception:
            notices = []
        # A crash can land between a child's terminal run commit and the parent fact that
        # publishes it. Rebuild the outcome from durable events; never rerun child work.
        try:
            notices.extend(self._host.recover_child_publications())
        except Exception:
            pass
        for parent_id, notice_id in notices:
            if self._host.closed:
                return
            self._host.deliver_child_notice(parent_id, notice_id)
        # Queued follow-ups and notices are durable messages: deliver each exact identity once.
        for session in self._host.store_sessions(str(self._host.workspace)):
            if self._host.closed:
                return
            if not self._host.admits_fresh_work(session):
                continue
            for _ in range(8):
                message = self._host.first_child_message(session.session_id)
                if message is None:
                    break
                try:
                    self._host.drain_continuations(
                        session.session_id,
                        limit=1,
                        continuation_id=message.continuation_id,
                        _automatic=True,
                    )
                except Exception:
                    break

    def consume_resume_intent(self, session_id: str) -> None:
        """Claim and drive one owed continuation, exactly once, on its own captured generation.

        The intent is either an activation request id or an upgrade pause id; the consumption event
        names the real staged fact it came from, never a forged cause.
        """
        with self._host.session_lock(session_id):
            self._host.ensure_open()
            if self._host.has_abort(session_id):
                return
            checkpoint = self._host.store_checkpoint(session_id)
            if checkpoint is None or not isinstance(checkpoint.payload, Mapping):
                return
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
            intent = cursor.resume_intent
            if not intent:
                return
            session = self._host.store_session(session_id)
            if checkpoint.revision_id != self._host.revision_id or session.active_revision_id != self._host.revision_id:
                return
            self._host.default_abort.set_cancelled(False)
            cursor.resume_intent = ""
            execution = _Execution(session, cursor, list(self._host.store_events(session_id)), session.run_id or intent)
            try:
                self._host.commit(
                    execution,
                    SessionStatus.RUNNING,
                    CheckpointKind.READY,
                )
            except StoreConflict:
                return
            abort = Cancellation()
            self._host.register_run(session_id, execution.run_id, abort)
            try:
                self._host.drive(execution)
            finally:
                self._host.unregister_run(session_id)
