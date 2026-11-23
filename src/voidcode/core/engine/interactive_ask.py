"""Model-initiated interactive ask suspension, adoption, resumption, and expiration sweeps."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from ...sdk.ask import AskAnswerStatus, AskError, AskRequest, AskResultBody, validate_ask_answer
from ...sdk.codec import encode_tool_call
from ...sdk.hooks import StopCause
from ...sdk.json_values import json_wire_object, json_wire_value
from ...sdk.services import BudgetExhaustion
from ...sdk.suspend import ParkRevisionError
from ...sdk.tools import TextOutput, ToolCall, ToolFailure, ToolResult, ToolSuccess
from ..cancellation import Cancellation, CancellationCoordinator
from ..delegation import _child_run_status, _terminal_run_result
from ..events import EventType, child_completion_notice_id
from ..execution import _Cursor, _Execution, committed_event, wire_object
from ..storage.store import (
    AskAnswerConflict,
    CheckpointKind,
    EventDraft,
    SessionRecord,
    SessionStatus,
    SqliteStore,
    StoreConflict,
    StoredEvent,
    WaitingAskPark,
)

if TYPE_CHECKING:
    from .turn_execution import CoreResult

logger = logging.getLogger(__name__)

PARKED_INTERVENING_TYPES = frozenset(
    {
        EventType.COMMAND_COMPLETED,
        EventType.CONFIRMATION_REQUESTED,
        EventType.CONFIRMATION_DECIDED,
        EventType.CHILD_SPAWNED,
        EventType.CHILD_RESERVED,
        EventType.CHILD_FINISHED,
        EventType.MODEL_QUERY,
        EventType.TOOL_DEFERRED,
        EventType.TOOL_PROGRESS,
        EventType.PROVIDER_PROGRESS,
        EventType.OBSERVER_ERROR,
        EventType.WINDOW_PROJECTED,
        EventType.PROVIDER_INTERRUPTED,
        EventType.ACTIVATION_REQUESTED,
        EventType.ACTIVATION_COMMITTED,
        EventType.ACTIVATION_FAILED,
    }
)


def is_parked_intervening(event: StoredEvent) -> bool:
    """Whether an event may legitimately appear after a parked checkpoint."""
    return event.event_type in PARKED_INTERVENING_TYPES or event.event_type.startswith("plugin.")


@dataclass(frozen=True, slots=True)
class AskBinding:
    """One validated live ask park: its committed request plus park-time facts."""

    event: StoredEvent
    request: AskRequest
    parked_at_ms: int
    idempotency_key: str | None


@dataclass(frozen=True, slots=True)
class AskAnswered:
    """An ask park settled with client-supplied values."""

    values: Mapping[str, object]
    status: Literal["answered"] = "answered"


@dataclass(frozen=True, slots=True)
class AskCancelled:
    """An ask park cancelled explicitly by the user."""

    values: Mapping[str, object] = MappingProxyType({})
    status: Literal["cancelled"] = "cancelled"


@dataclass(frozen=True, slots=True)
class AskExpired:
    """An ask park settled automatically because its deadline elapsed."""

    values: Mapping[str, object] = MappingProxyType({})
    status: Literal["expired"] = "expired"


type AskOutcome = AskAnswered | AskCancelled | AskExpired


def is_ask_binding_expired(binding: AskBinding) -> bool:
    """Whether the caller-declared deadline passed; None waits indefinitely like approvals."""
    timeout = binding.request.timeout_ms
    if timeout is None:
        return False
    return int(time.time() * 1000) > binding.parked_at_ms + timeout


def render_ask_answers(request: AskRequest, values: Mapping[str, object]) -> str:
    """Render every answer as model-projected text; bodies are omitted from projection."""
    lines = [f"Ask: {request.title}"]
    for question in request.questions:
        label = question.label or question.name
        if question.name in values:
            value = values[question.name]
            rendered = value if isinstance(value, str) else json.dumps(json_wire_value(value), ensure_ascii=False)
            lines.append(f"{label}: {rendered}")
        else:
            lines.append(f"{label}: (no answer)")
    return "\n".join(lines)


type ChildRunStatus = Literal["completed", "failed", "waiting", "cancelled", "interrupted"]


class InteractiveAskHost(Protocol):
    """Core capabilities required by the interactive ask runtime."""

    @property
    def store(self) -> SqliteStore: ...

    @property
    def revision_id(self) -> str: ...

    @property
    def closed(self) -> bool: ...

    @property
    def abort_signal(self) -> Cancellation: ...

    @property
    def cancellations(self) -> CancellationCoordinator: ...

    def check_expected_revision(self, expected_revision: str) -> None: ...

    def ensure_open(self) -> None: ...

    def session_lock(self, session_id: str) -> AbstractContextManager[None]: ...

    def check_session(self, session: SessionRecord) -> None: ...

    def background_abort_event(self, action_id: str) -> threading.Event | None: ...

    def background_stop_cause(self, action_id: str) -> StopCause: ...

    def child_captured_root_id(self, child_session_id: str) -> str: ...

    def project_messages(self, execution: _Execution, *, allow_pending: bool = False) -> object: ...

    def pending(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]: ...

    def report(
        self,
        execution: _Execution,
        call: ToolCall,
        result: ToolResult,
        *,
        prefix_events: tuple[EventDraft, ...] = (),
        ask_answer_guard: str | None = None,
    ) -> None: ...

    def drive(self, execution: _Execution) -> CoreResult: ...

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

    def drain_deferred_requests(self, session_id: str) -> None: ...

    def drain_background_pending(self, session_id: str) -> None: ...

    def drive_pending(self, session_id: str) -> None: ...

    def notify_child_state_changed(self) -> None: ...


class InteractiveAskManager:
    """Owns model-initiated interactive ask suspension, adoption, resumption, and expiration sweeps."""

    def __init__(self, host: InteractiveAskHost) -> None:
        self._host = host

    def resolve_ask_binding(self, execution: _Execution, cursor: _Cursor) -> AskBinding:
        """Validate the cursor's live ask binding against its committed request event."""
        if cursor.ask_sequence is None or cursor.ask_request_id is None:
            raise ValueError("session holds no live ask binding")
        event = committed_event(execution, cursor.ask_sequence, EventType.ASK_REQUESTED)
        request = AskRequest.from_payload(wire_object(event.payload.get("request"), "ask request"))
        if request.request_id != cursor.ask_request_id:
            raise ValueError("parked ask binding does not match its committed request")
        parked_at = event.payload.get("parked_at_ms")
        if type(parked_at) is not int or parked_at < 0:
            raise ValueError("parked ask request has no honest park time")
        raw_key = event.payload.get("idempotency_key")
        return AskBinding(
            event=event,
            request=request,
            parked_at_ms=parked_at,
            idempotency_key=raw_key if isinstance(raw_key, str) else None,
        )

    def is_ask_binding_expired(self, binding: AskBinding) -> bool:
        """Whether the caller-declared deadline passed; None waits indefinitely like approvals."""
        return is_ask_binding_expired(binding)

    def adopt_ask_terminal(
        self,
        execution: _Execution,
        binding: AskBinding,
        outcome: AskOutcome,
    ) -> None:
        """Adopt one ask terminal without re-invoking the parked tool, exactly once.

        Commits ASK_ANSWERED plus the parked call's TOOL_COMPLETED in one guarded
        transaction (the guard raises AskAnswerConflict when a terminal already
        exists) and clears the park binding via report.
        """
        request = binding.request
        if execution.cursor.intent_sequence is None:
            raise ValueError("ask adoption requires its parked tool intent")
        _, call = self._host.pending(execution)
        intent = committed_event(execution, execution.cursor.intent_sequence, EventType.TOOL_INTENT)
        if json_wire_object(intent.payload)["call"] != encode_tool_call(call):
            raise ValueError("parked ask intent does not match its native batch member")

        match outcome:
            case AskAnswered(values=values):
                result: ToolResult = ToolSuccess(
                    call.tool_name,
                    TextOutput(text=render_ask_answers(request, values)),
                    body=AskResultBody(answers=dict(values), status="answered"),
                )
                source, actor = "client", "user"
                status: AskAnswerStatus = "answered"
                payload_values = values
            case AskCancelled():
                result = ToolFailure(call.tool_name, f"ask {request.request_id} was cancelled by the user")
                source, actor = "client", "user"
                status = "cancelled"
                payload_values = {}
            case AskExpired():
                result = ToolFailure(call.tool_name, f"ask {request.request_id} expired without an answer")
                source, actor = "core", "core"
                status = "expired"
                payload_values = {}

        answered = EventDraft(
            EventType.ASK_ANSWERED,
            source,
            actor,
            {"request_id": request.request_id, "status": status, "values": json_wire_object(payload_values)},
        )
        try:
            self._host.report(execution, call, result, prefix_events=(answered,), ask_answer_guard=request.request_id)
        except AskAnswerConflict as exc:
            raise AskError(f"ask {request.request_id!r} was already answered") from exc

    def resume_prelude(
        self,
        session_id: str,
        request_id: str,
        run_abort_id: str,
        abort: Cancellation,
    ) -> tuple[_Execution, AskBinding]:
        """Rebuild the parked ask's execution under the session lock, resume-style.

        Validates session, checkpoint identity, parked prefix, root budget, and
        the ask binding; the named request must be the live one and must have
        no terminal answer yet. Callers hold the session lock and registered
        the abort; adoption plus drive continue from here.
        """
        session = self._host.store.session(session_id)
        self._host.check_session(session)
        for ev in self._host.store.events(session_id):
            if ev.event_type == EventType.ASK_ANSWERED and ev.payload.get("request_id") == request_id:
                raise AskError(f"ask {request_id!r} was already answered (status: {ev.payload.get('status')})")
        if session.status is not SessionStatus.WAITING:
            raise AskError(f"session {session_id!r} holds no live ask park (status: {session.status.value})")
        checkpoint = self._host.store.checkpoint(session_id)
        if checkpoint is None or checkpoint.kind is CheckpointKind.TERMINAL:
            raise AskError(f"session {session_id!r} holds no live ask park")
        if (
            checkpoint.revision_id != self._host.revision_id
            or checkpoint.run_id != session.run_id
            or checkpoint.last_event_sequence > session.last_event_sequence
        ):
            raise ParkRevisionError("ask checkpoint identity does not match the durable session")
        events_after_cp = self._host.store.events(session_id, after_sequence=checkpoint.last_event_sequence)
        for ev in events_after_cp:
            if not is_parked_intervening(ev):
                raise ValueError(f"Intervening event {ev.event_type} violates parked checkpoint prefix")
        if "initial_root_budget" not in checkpoint.payload:
            raise ValueError("checkpoint has no captured root budget state")
        cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        if not isinstance(cursor.root_id, str) or not cursor.root_id:
            raise ValueError("checkpoint has no captured root action identity")
        if cursor.deferred_action_id is not None:
            action_abort = self._host.background_abort_event(cursor.deferred_action_id)
            if action_abort is not None:
                abort = Cancellation(
                    action_abort,
                    linked_reason="background action stopped",
                    linked_stop_cause_provider=lambda action_id=cursor.deferred_action_id: self._host.background_stop_cause(action_id),
                )
                self._host.cancellations.register_run(session_id, run_abort_id, abort)
        root_state = self._host.store.root_budget_state(cursor.root_id)
        if cursor.initial_root_budget is None:
            if root_state is not None:
                raise StoreConflict("checkpoint captured an uncapped root with a finite budget row")
        elif root_state is None or root_state[0] < cursor.initial_root_budget:
            raise StoreConflict("checkpoint lost its captured finite root authorization")
        if session.parent_session_id is not None:
            self._host.child_captured_root_id(session_id)
        execution = _Execution(session, cursor, list(self._host.store.events(session_id)), run_abort_id)
        self._host.project_messages(execution, allow_pending=True)
        binding = self.resolve_ask_binding(execution, cursor)
        if binding.request.request_id != request_id:
            raise AskError(f"ask {request_id!r} is not the live parked ask of session {session_id!r}")
        if binding.request.revision_id != self._host.revision_id:
            raise ParkRevisionError(f"ask {request_id!r} belongs to a retired revision")
        return execution, binding

    def _resume_and_drive(
        self,
        session_id: str,
        request_id: str,
        outcome_resolver: Callable[[AskBinding], AskOutcome],
        *,
        reset_abort: bool = True,
    ) -> tuple[CoreResult, bool]:
        with self._host.session_lock(session_id):
            self._host.ensure_open()
            if reset_abort:
                self._host.abort_signal.set_cancelled(False)
            abort = Cancellation()
            run_abort_id = uuid4().hex
            self._host.cancellations.register_run(session_id, run_abort_id, abort)
            resumed_notice: tuple[str, str] | None = None
            try:
                execution, binding = self.resume_prelude(session_id, request_id, run_abort_id, abort)
                session = execution.session
                expired = self.is_ask_binding_expired(binding)
                outcome = AskExpired() if expired else outcome_resolver(binding)
                self.adopt_ask_terminal(execution, binding, outcome)
                result = self._host.drive(execution)
                if session.parent_session_id is not None and _terminal_run_result(result):
                    child_now = self._host.store.session(session_id)
                    notify = self._host.notify_requested(child_now, self._host.store.events(session_id))
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
                self._host.cancellations.unregister_run(session_id)
        if resumed_notice is not None:
            self._host.deliver_child_notice(*resumed_notice)
        self._host.drain_deferred_requests(session_id)
        self._host.drain_background_pending(session_id)
        self._host.drive_pending(session_id)
        if session.parent_session_id is not None:
            self._host.notify_child_state_changed()
        return result, expired

    def answer_ask(
        self,
        session_id: str,
        request_id: str,
        values: Mapping[str, object],
        *,
        expected_revision: str,
    ) -> CoreResult:
        """Adopt one client's answer for the session's live ask and continue the run.

        Session-locked and resume-like: the request must be live and unexpired,
        values are validated by the shared strict validator, and the parked
        tool is adopted without re-invoke. A past-deadline park settles as
        expired first and the answer is refused as already terminal.
        """
        self._host.check_expected_revision(expected_revision)
        result, expired = self._resume_and_drive(
            session_id,
            request_id,
            lambda binding: AskAnswered(values=validate_ask_answer(binding.request, values)),
        )
        if expired:
            raise AskError(f"ask {request_id!r} expired without an answer; the run continued with the expiry failure")
        return result

    def cancel_ask(
        self,
        session_id: str,
        request_id: str,
        *,
        expected_revision: str,
    ) -> CoreResult:
        """Adopt one client's cancellation for the session's live ask and continue the run.

        Session-locked and resume-like: the request must be live and unexpired.
        A past-deadline park settles as expired first and the cancellation is
        refused as already terminal.
        """
        self._host.check_expected_revision(expected_revision)
        result, expired = self._resume_and_drive(
            session_id,
            request_id,
            lambda _binding: AskCancelled(),
        )
        if expired:
            raise AskError(f"ask {request_id!r} expired without an answer; the run continued with the expiry failure")
        return result

    def settle_expired_ask_candidate(self, candidate: WaitingAskPark) -> None:
        """Settle one sweep candidate's expired ask and continue its run, if still due.

        Stale candidates (answered, cancelled, or resumed since listing) return
        quietly; anything else fails loudly per session in the sweep caller.
        """
        session_id = candidate.session_id
        with self._host.session_lock(session_id):
            self._host.ensure_open()
            abort = Cancellation()
            run_abort_id = uuid4().hex
            self._host.cancellations.register_run(session_id, run_abort_id, abort)
            resumed_notice: tuple[str, str] | None = None
            settled = False
            try:
                try:
                    execution, binding = self.resume_prelude(session_id, candidate.ask_request_id, run_abort_id, abort)
                except AskError, KeyError:
                    return
                if not self.is_ask_binding_expired(binding):
                    return
                session = execution.session
                self.adopt_ask_terminal(execution, binding, AskExpired())
                result = self._host.drive(execution)
                settled = True
                if session.parent_session_id is not None and _terminal_run_result(result):
                    child_now = self._host.store.session(session_id)
                    notify = self._host.notify_requested(child_now, self._host.store.events(session_id))
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
                self._host.cancellations.unregister_run(session_id)
            if not settled:
                return
        if resumed_notice is not None:
            self._host.deliver_child_notice(*resumed_notice)
        self._host.drain_deferred_requests(session_id)
        self._host.drain_background_pending(session_id)
        self._host.drive_pending(session_id)
        if session.parent_session_id is not None:
            self._host.notify_child_state_changed()

    def sweep_expired_asks(self) -> None:
        """Settle caller-declared ask expiries due now; one bad session never stops the sweep."""
        try:
            candidates = self._host.store.waiting_ask_parks()
        except Exception:
            logger.exception("ask expiry sweep failed to list parked asks")
            return
        for candidate in candidates:
            if self._host.closed:
                return
            try:
                self.settle_expired_ask_candidate(candidate)
            except Exception:
                logger.exception("ask expiry sweep failed for session %s", candidate.session_id)
