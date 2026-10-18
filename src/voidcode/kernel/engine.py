"""Governed turns over immutable revisions and durable native tool batches."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock
from typing import Literal, get_args
from uuid import uuid4

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from ..sdk.codec import (
    decode_provider_result,
    decode_tool_result,
    encode_message,
    encode_provider_result,
    encode_tool_call,
    encode_tool_result,
)
from ..sdk.commands import (
    CommandDescriptor,
    CommandResult,
)
from ..sdk.config import KernelConfig
from ..sdk.hooks import StopCause
from ..sdk.invocation import ToolContext
from ..sdk.json_values import json_wire_object
from ..sdk.plugin import HostPlugin
from ..sdk.provider import ProviderMessage, ProviderTurnRequest
from ..sdk.services import (
    ContinuationResult,
    PluginBudget,
    SessionUsageReport,
)
from ..sdk.tools import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult, has_external_mutations
from ..sdk.views import PluginViewDescriptor
from .host import HostClient, HostTimeout, HostUnavailable, InvocationCancelled, ToolInvocationOutcome
from .storage.store import (
    Checkpoint,
    EventDraft,
    ObserverReceiptRecord,
    SessionRecord,
    SqliteStore,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StoreConflict,
    StoredEvent,
)


class Cancellation:
    def __init__(self) -> None:
        self._event = Event()
        self._reason: str | None = None
        self._stop_cause: StopCause = "user_stop"
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    @property
    def stop_cause(self) -> StopCause:
        with self._lock:
            return self._stop_cause

    def set_cancelled(self, value: bool, *, reason: str | None = None, stop_cause: StopCause = "user_stop") -> None:
        with self._lock:
            self._reason = reason if value else None
            self._stop_cause = stop_cause if value else "user_stop"
            if value:
                self._event.set()
            else:
                self._event.clear()


def _wire_object(value: object, what: str) -> Mapping[str, object]:
    """Require a JSON object from a durable payload, failing visibly when malformed."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a JSON object")
    return json_wire_object(value)


_PARKED_INTERVENING_TYPES = frozenset({"kernel.command_completed", "kernel.command_intent", "kernel.observer_error"})


_CORE_COMMAND_SPECS: tuple[tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], bool], ...] = (
    (
        "run",
        "Run an agent turn with a user prompt",
        {
            "type": "object",
            "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 65536}},
            "required": ["prompt"],
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
        True,
    ),
    (
        "resume",
        "Resume a paused session (approval or interrupted)",
        {
            "type": "object",
            "properties": {
                "approval_request_id": {"type": "string", "minLength": 1},
                "decision": {"enum": ["allow", "deny"]},
            },
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
        True,
    ),
    (
        "show",
        "Show session state, checkpoint, and events",
        {"type": "object", "properties": {"events": {"type": "boolean"}}, "additionalProperties": False},
        (ToolEffect.READ,),
        False,
    ),
    ("status", "Show kernel and workspace status", {"type": "object", "additionalProperties": False}, (ToolEffect.READ,), False),
    ("help", "Show available commands and documentation", {"type": "object", "additionalProperties": False}, (ToolEffect.READ,), False),
)
_CORE_COMMAND_NAMES = frozenset(name for name, _d, _s, _e, _g in _CORE_COMMAND_SPECS)


def _is_parked_intervening(event: StoredEvent) -> bool:
    """Whether an event may legitimately appear after a parked checkpoint."""
    return event.event_type in _PARKED_INTERVENING_TYPES or event.event_type.startswith("plugin.")


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    batch_sequence: int | None = Field(default=None, ge=1)
    next_call: int = Field(default=0, ge=0)
    intent_sequence: int | None = Field(default=None, ge=1)
    approval_sequence: int | None = Field(default=None, ge=1)
    step: int = Field(default=0, ge=0)
    abandon_batch: bool = False
    continuation_id: str | None = None
    chain_step: int = Field(default=0, ge=0)
    logical_turn: str = ""
    stop_cause: StopCause | None = None
    action_budget_remaining: int | None = None
    root_id: str = ""


@dataclass(frozen=True, slots=True)
class KernelResult:
    session_id: str
    run_id: str
    revision_id: str
    status: str
    output: str | None = None
    error: str | None = None
    pending_approval: Mapping[str, object] | None = None
    stop_cause: StopCause | None = None

    def as_payload(self) -> dict[str, object]:
        payload = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "pending_approval": None if self.pending_approval is None else json_wire_object(self.pending_approval),
        }
        if self.stop_cause is not None:
            payload["stop_cause"] = self.stop_cause
        return payload


@dataclass(slots=True)
class _Execution:
    session: SessionRecord
    cursor: _Cursor
    events: list[StoredEvent]
    run_id: str


class Kernel:
    def __init__(
        self,
        *,
        workspace: Path,
        state_dir: Path,
        config: KernelConfig,
        plugins: tuple[HostPlugin, ...],
        revision_payload: Mapping[str, object],
        auto_drain: bool = True,
    ) -> None:
        self.auto_drain = auto_drain
        self.workspace = workspace.resolve(strict=True)
        self.config = config
        self._abort = Cancellation()
        self._closed = False
        # ponytail: serialize workspace runs; per-session scheduling when concurrent runs are needed.
        self._run_lock = Lock()
        self._session_aborts: dict[str, Cancellation] = {}
        self._aborts_lock = Lock()
        with ExitStack() as resources:
            # Lock the directory inode, independent of the selected database path.
            # Opening/locking it does not create or rewrite workspace files.
            lease = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY)
            resources.callback(os.close, lease)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another kernel owns this workspace") from exc
            state_dir.mkdir(parents=True, exist_ok=True)
            self.store = SqliteStore(state_dir / "kernel-v1.sqlite3")
            resources.callback(self.store.close)
            self.revision_id = self.store.put_revision(revision_payload)
            self.host = HostClient(
                plugins,
                workspace=self.workspace,
                revision_id=self.revision_id,
                store=self.store,
                usage_callback=self._get_session_usage,
                budget_callback=self._get_owner_budget,
                lease_fd=lease,
            )
            resources.callback(self.host.close)
            self._definitions = {tool.name: tool for tool in self.host.tools}
            self._validators = {tool.name: jsonschema.Draft202012Validator(json_wire_object(tool.input_schema)) for tool in self.host.tools}
            self._resources = resources.pop_all()
            self._replay_pending_observers()

    def __enter__(self) -> Kernel:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def cancel_session(self, session_id: str, reason: str = "user cancellation", *, cause: StopCause = "user_stop") -> bool:
        """Signal cancellation only to the currently executing run of a specific session."""
        self._ensure_open()
        # Ownership first: a session id from another workspace sharing this
        # database may never be signalled or settled.
        self.session(session_id)
        with self._aborts_lock:
            abort = self._session_aborts.get(session_id)
        if abort is not None and not abort.cancelled:
            abort.set_cancelled(True, reason=reason, stop_cause=cause)
            return True
        return self._settle_parked_cancel(session_id, reason, cause)

    def _settle_parked_cancel(self, session_id: str, reason: str, cause: StopCause) -> bool:
        """Settle parked work at its already-safe boundary.

        Parked work is an approval wait or a recoverable (non-terminal)
        interruption with no active abort in this process: an explicit user or
        owner cancellation ends it here instead of leaving a session that
        refuses fresh work AND cannot resume. The session must belong to this
        workspace and carry its own durable run identity, and claimed
        continuations are cancelled in the SAME transaction as the terminal
        checkpoint, so no crash can leave a cancelled claim next to a resumable
        session or vice versa. The run lock is deliberately not taken: an
        unrelated run may be executing in another thread, and the store's CAS on
        (sequence, run) is what keeps this settle atomic.
        """
        session = self.session(session_id)
        checkpoint = self.store.checkpoint(session_id)
        parked_run_id = self._parked_run_id(session, checkpoint)
        if parked_run_id is None:
            return False
        cursor = _Cursor()
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        execution = _Execution(session, cursor, list(self.store.events(session_id)), parked_run_id)
        parked_claim = (
            next((c for c in self.store.claimed_continuations(session_id, limit=100) if c.continuation_id == cursor.continuation_id), None)
            if cursor.continuation_id is not None
            else None
        )
        try:
            if cursor.batch_sequence is not None:
                # The parked batch is never replayed: report its members as interrupted first.
                self._interrupt_batch(execution)
            self._commit(
                execution,
                "interrupted",
                "terminal",
                EventDraft(
                    "kernel.run_interrupted",
                    "kernel",
                    "kernel",
                    {
                        "reason": reason,
                        "stop_cause": cause,
                        "logical_turn": execution.cursor.logical_turn,
                        **self._measured_run_elapsed_fields(execution),
                    },
                ),
                staged_cancellations=(
                    (StagedCancellation(owner=parked_claim.owner, continuation_id=parked_claim.continuation_id),) if parked_claim is not None else ()
                ),
            )
        except StoreConflict:
            # The parked session moved on under another writer; settling is moot.
            return False
        return True

    def _parked_run_id(self, session: SessionRecord, checkpoint: Checkpoint | None) -> str | None:
        """The run identity of parked work this session may safely settle, else None.

        Parked work is an approval wait or a recoverable interruption, and it must
        carry the session's own durable run identity with a non-terminal checkpoint
        of that same run. A terminal checkpoint (or a finished session) holds
        nothing to settle, but a session that still claims parked work without an
        authentic identity is a corrupt state and fails visibly here - before any
        write - instead of being reported as an ordinary no-work cancellation.
        """
        if session.status not in ("waiting", "interrupted"):
            return None
        if checkpoint is not None and checkpoint.kind == "terminal":
            return None
        run_id = session.run_id
        if not run_id or checkpoint is None or checkpoint.run_id != run_id:
            raise ValueError(f"session {session.session_id!r} reports parked {session.status} work without its authentic run identity")
        return run_id

    def _admits_fresh_work(self, session: SessionRecord) -> bool:
        """Whether a session accepts a fresh user turn or auto-driven follow-up work.

        Idle, finished, and failed sessions are open, and so is a terminal
        interruption of the session's own run. Anything else still holds
        unfinished native work that only an explicit resume may re-enter.
        """
        if session.status in {"idle", "completed", "failed"}:
            return True
        checkpoint = self.store.checkpoint(session.session_id)
        return session.status == "interrupted" and checkpoint is not None and checkpoint.kind == "terminal" and checkpoint.run_id == session.run_id

    def cancel(self, reason: str = "user cancellation", *, cause: StopCause = "user_stop") -> None:
        self._abort.set_cancelled(True, reason=reason, stop_cause=cause)
        with self._aborts_lock:
            for ab in self._session_aborts.values():
                ab.set_cancelled(True, reason=reason, stop_cause=cause)

    def close(self) -> None:
        self.cancel("kernel shutdown", cause="shutdown")
        with self._run_lock:
            if not self._closed:
                self._closed = True
                self._resources.close()

    def run(self, prompt: str, *, session_id: str | None = None, max_steps: int = 32) -> KernelResult:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 64 * 1024:
            raise ValueError("prompt must be nonempty UTF-8 text within 64 KiB")
        self._validate_steps(max_steps)
        if session_id is not None and (not isinstance(session_id, str) or not session_id or len(session_id) > 128 or "\0" in session_id):
            raise ValueError("session_id must be nonempty text within 128 characters without NUL")
        with self._run_lock:
            self._ensure_open()
            self._abort.set_cancelled(False)
            session_id = uuid4().hex if session_id is None else session_id
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = abort
            try:
                try:
                    session = self.store.session(session_id)
                except KeyError:
                    session = self.store.create_session(session_id, str(self.workspace), self.revision_id)
                if not self._admits_fresh_work(session):
                    raise ValueError("unfinished session requires explicit resume")
                self._check_session(session)
                root_id = uuid4().hex
                self.store.ensure_root_budget(root_id, max_steps)
                execution = _Execution(
                    session,
                    _Cursor(root_id=root_id, action_budget_remaining=max_steps),
                    list(self.store.events(session_id)),
                    uuid4().hex,
                )
                if session.status == "interrupted":
                    # Finish the abandoned stopped prefix on its own cursor first,
                    # then start the new user turn with a fresh cursor: it must not
                    # inherit a stale continuation claim or a spent chain budget.
                    previous = self.store.checkpoint(session_id)
                    if previous is not None and isinstance(previous.payload, Mapping):
                        execution.cursor = _Cursor.model_validate(json_wire_object(previous.payload))
                        execution.cursor.step = 0
                        execution.cursor.stop_cause = None
                    if execution.cursor.batch_sequence is not None:
                        # The stopped batch is never replayed: report its members before the new turn.
                        self._interrupt_batch(execution)
                    # The new user turn owns its own fresh root budget: the stopped
                    # chain's exhausted root is never reused or reset.
                    execution.cursor = _Cursor(root_id=root_id, action_budget_remaining=max_steps)
                self._commit(execution, "running", "ready", EventDraft("kernel.user_message", "client", "user", {"content": prompt}), start_turn=True)
                result = self._drive(execution, max_steps=max_steps)
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)
        self._drive_pending(session_id, max_steps)
        return result

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        decision: Literal["allow", "deny"] | None = None,
        max_steps: int = 32,
    ) -> KernelResult:
        self._validate_steps(max_steps)
        with self._run_lock:
            self._ensure_open()
            self._abort.set_cancelled(False)
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = abort
            try:
                session = self.store.session(session_id)
                self._check_session(session)
                checkpoint = self.store.checkpoint(session_id)
                if checkpoint is None or checkpoint.kind == "terminal":
                    raise ValueError("session has no resumable checkpoint")
                if (
                    checkpoint.revision_id != self.revision_id
                    or checkpoint.run_id != session.run_id
                    or checkpoint.last_event_sequence > session.last_event_sequence
                ):
                    raise ValueError("checkpoint identity does not match the durable session")
                events_after_cp = self.store.events(session_id, after_sequence=checkpoint.last_event_sequence)
                for ev in events_after_cp:
                    if not _is_parked_intervening(ev):
                        raise ValueError(f"Intervening event {ev.event_type} violates parked checkpoint prefix")
                cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
                execution = _Execution(session, cursor, list(self.store.events(session_id)), uuid4().hex)
                self._messages(execution, allow_pending=True)
                if cursor.intent_sequence is not None:
                    intent = self._event(execution, cursor.intent_sequence, "kernel.tool_intent")
                    batch, call = self._pending(execution)
                    if intent.payload["batch_sequence"] != batch.sequence or json_wire_object(intent.payload)["call"] != encode_tool_call(call):
                        raise ValueError("pending intent does not match its native batch member")
                approval: StoredEvent | None = None
                if cursor.approval_sequence is not None:
                    approval = self._event(execution, cursor.approval_sequence, "kernel.approval_requested")
                    if decision not in {"allow", "deny"} or approval_request_id != approval.payload["request_id"]:
                        raise ValueError("approval resume requires the exact pending request identity and decision")
                    batch, call = self._pending(execution)
                    if approval.payload["batch_sequence"] != batch.sequence or json_wire_object(approval.payload)["call"] != encode_tool_call(call):
                        raise ValueError("pending approval does not match its native batch member")
                elif approval_request_id is not None or decision is not None:
                    raise ValueError("session is not waiting for approval")
                self._commit(execution, "running", "ready", EventDraft("kernel.run_resumed", "kernel", "user", {}))
                if approval is not None:
                    cursor.approval_sequence = None
                    self._commit(
                        execution,
                        "running",
                        "ready",
                        EventDraft("kernel.approval_decided", "client", "user", {"request_id": approval_request_id, "decision": decision}),
                    )
                    _, call = self._pending(execution)
                    if decision == "deny":
                        self._report(execution, call, ToolFailure(call.tool_name, "user denied this invocation"))
                    else:
                        definition = self._definitions.get(call.tool_name)
                        if definition is None:
                            raise ValueError("approved tool is not in the active capability revision")
                        denial = self._denial(call, definition)
                        if denial:
                            self._report(execution, call, ToolFailure(call.tool_name, denial))
                        else:
                            stopped = self._invoke(execution, call, definition)
                            if stopped is not None:
                                return stopped
                elif cursor.abandon_batch:
                    self._interrupt_batch(execution)
                elif cursor.intent_sequence is not None:
                    intent = self._event(execution, cursor.intent_sequence, "kernel.tool_intent")
                    _, call = self._pending(execution)
                    if json_wire_object(intent.payload)["call"] != encode_tool_call(call):
                        raise ValueError("pending intent does not match its native batch member")
                    if intent.payload["replay_policy"] == "never":
                        self._interrupt_batch(execution)
                    # A safe read reuses the durable invocation identity below.
                result = self._drive(execution, max_steps=max_steps)
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)
        self._drive_pending(session_id, max_steps)
        return result

    def _drive_pending(self, session_id: str, max_steps: int, *, limit: int = 32) -> None:
        """Drive owned queued follow-up work at a safe boundary, outside the run lock.

        Returns nothing on purpose: `run`/`resume` report the result of the
        operation the caller actually requested. Auto-driven run outcomes stay
        observable through durable events, continuation rows (status, output,
        error), and `drain_continuations`. Only "nothing left to claim" is
        absorbable here; malformed checkpoints and invalid payloads must raise.
        """
        if not self.auto_drain:
            return
        for _ in range(max(1, limit)):
            try:
                results = self.drain_continuations(session_id, limit=1, max_steps=max_steps)
            except StoreConflict:
                # No pending or claimed continuation remains: the chain is done.
                return
            if not results:
                return
            if results[-1].run_status not in ("completed", "idle"):
                return

    def command_catalog(self) -> tuple[CommandDescriptor, ...]:
        """Catalog of admitted plugin commands and reserved core commands."""
        self._ensure_open()
        core_commands = tuple(
            CommandDescriptor(
                name=name,
                owner="core",
                description=description,
                argument_schema=dict(schema),
                effects=effects,
                busy_gate=busy_gate,
                client_capabilities=(),
            )
            for name, description, schema, effects, busy_gate in _CORE_COMMAND_SPECS
        )
        return core_commands + self.host.commands

    def _core_result(self, res: KernelResult) -> CommandResult:
        """Map a native run result into the command variant contract.

        Waiting and interrupted are legitimate native outcomes: they are reported
        with their real status, output, and stop cause in `data`, and a failure
        always carries a non-empty error message instead of a fake null.
        """
        if res.status in ("completed", "idle", "waiting"):
            return CommandResult(status="ok", output=res.output, data=res.as_payload())
        return CommandResult(
            status="error",
            output=res.output,
            error=res.error or f"run {res.status}",
            data=res.as_payload(),
        )

    def _owner_cancel_claimed_work(
        self,
        session_id: str,
        cancellations: Sequence[StagedCancellation],
        reason: str,
    ) -> None:
        """Settle work whose own claimed continuation an owner just cancelled.

        A running chain is signalled with cause `owner_cancel` so it stops before
        its next native call; a parked chain is settled to a terminal checkpoint.
        Rows owned by anybody else are untouched (the store scopes cancellations
        by owner), and nothing here starts new work.
        """
        if not cancellations:
            return
        checkpoint = self.store.checkpoint(session_id)
        cursor = _Cursor()
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        if cursor.continuation_id is None:
            return
        claim_owner = self.store.continuation_owner(cursor.continuation_id)
        if not any(self._cancels_claimed(cancellation, cursor.continuation_id, claim_owner) for cancellation in cancellations):
            return
        with self._aborts_lock:
            active = self._session_aborts.get(session_id)
        if active is not None and not active.cancelled:
            active.set_cancelled(True, reason=reason, stop_cause="owner_cancel")
            return
        self._settle_parked_cancel(session_id, reason, "owner_cancel")

    @staticmethod
    def _cancels_claimed(cancellation: StagedCancellation, claimed_id: str | None, claimed_owner: str | None) -> bool:
        """Whether one staged cancellation targets this claim under the claim's OWN owner.

        Cancellation scope is the pair (owner, id): a foreign owner naming this
        claim's id, and a foreign unscoped cancellation, never scope this claim -
        exactly like the store's owner-scoped update, so a foreign owner can never
        stop another owner's run or strip its durable claim.
        """
        if claimed_id is None or claimed_owner is None:
            return False
        return cancellation.owner == claimed_owner and (cancellation.continuation_id is None or cancellation.continuation_id == claimed_id)

    def create_session(self, session_id: str | None = None) -> SessionRecord:
        """Create a new durable session in idle state bound to workspace and active revision."""
        self._ensure_open()
        sid = uuid4().hex if session_id is None else session_id
        return self.store.create_session(sid, str(self.workspace), self.revision_id)

    def session(self, session_id: str) -> SessionRecord:
        """Fetch session metadata by ID, verifying workspace."""
        self._ensure_open()
        s = self.store.session(session_id)
        if s.workspace != str(self.workspace):
            raise ValueError("session belongs to another workspace")
        return s

    def sessions(self, *, limit: int = 100) -> tuple[SessionRecord, ...]:
        """Fetch sessions for this workspace, ordered by updated_at descending, up to limit."""
        self._ensure_open()
        return self.store.sessions(workspace=str(self.workspace), limit=limit)

    def show(self, session_id: str, *, events: bool = False) -> dict[str, object]:
        """Return session metadata, active checkpoint, and optional events as JSON-serializable dict."""
        self._ensure_open()
        s = self.session(session_id)
        checkpoint = self.store.checkpoint(session_id)
        payload: dict[str, object] = {
            "session_id": s.session_id,
            "status": s.status,
            "run_id": s.run_id,
            "revision_id": s.active_revision_id,
            "turn": s.turn,
            "last_event_sequence": s.last_event_sequence,
            "checkpoint": None if checkpoint is None else {"kind": checkpoint.kind, "payload": json_wire_object(checkpoint.payload)},
        }
        if events:
            payload["events"] = [
                {
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "run_id": event.run_id,
                    "revision_id": event.revision_id,
                    "source": event.source,
                    "actor": event.actor,
                    "payload": json_wire_object(event.payload),
                }
                for event in self.store.events(session_id)
            ]
        return payload

    def events_after(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> tuple[StoredEvent, ...]:
        """Bounded tail read of stored events for continuous SSE streaming."""
        self._ensure_open()
        self.session(session_id)
        return self.store.events_after(session_id, after_sequence=after_sequence, limit=limit)

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        """Calculate truthful token usage metrics strictly from immutable core facts."""
        self._ensure_open()
        s = self.session(session_id)
        return self._get_session_usage(session_id, since_sequence, until_sequence, session=s)

    def _get_session_usage(
        self,
        session_id: str,
        since_sequence: int = 0,
        until_sequence: int | None = None,
        *,
        session: SessionRecord | None = None,
    ) -> SessionUsageReport:
        if session is None:
            s = self.session(session_id)
        else:
            s = session

        all_events = self.store.events(session_id)
        dispatches_in_window: list[int] = []
        completions_by_dispatch: dict[int, StoredEvent] = {}

        for ev in all_events:
            if ev.sequence > s.last_event_sequence:
                # Only events already inside the captured session tip are reported,
                # so through_sequence never claims less than what was included.
                break
            if ev.event_type == "kernel.provider_intent":
                seq = ev.sequence
                if seq > since_sequence and (until_sequence is None or seq <= until_sequence):
                    dispatches_in_window.append(seq)
            elif ev.event_type in ("kernel.model_turn", "kernel.provider_interrupted"):
                disp_seq = ev.payload.get("dispatch_sequence")
                if disp_seq is not None and isinstance(disp_seq, (int, float)):
                    completions_by_dispatch[int(disp_seq)] = ev

        if not dispatches_in_window:
            return SessionUsageReport(
                session_id=session_id,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                uncached_input_tokens=0,
                total_cost_usd=0.0,
                has_unknown_usage=False,
                turn_count=0,
                elapsed_seconds=0.0,
                since_sequence=since_sequence,
                until_sequence=until_sequence,
                through_sequence=s.last_event_sequence,
            )

        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_uncached_input = 0
        total_cost = 0.0
        total_elapsed_ms = 0
        unknown_input = False
        unknown_output = False
        unknown_cache = False
        unknown_uncached = False
        unknown_cost = False
        unknown_elapsed = False
        turn_count = 0

        for disp_seq in dispatches_in_window:
            comp_ev = completions_by_dispatch.get(disp_seq)
            if comp_ev is None or comp_ev.event_type == "kernel.provider_interrupted":
                unknown_input = True
                unknown_output = True
                unknown_cache = True
                unknown_uncached = True
                unknown_cost = True
                if comp_ev is not None:
                    ms = comp_ev.payload.get("elapsed_ms")
                    if ms is not None and isinstance(ms, (int, float)):
                        total_elapsed_ms += int(ms)
                    else:
                        unknown_elapsed = True
                else:
                    unknown_elapsed = True
            elif comp_ev.event_type == "kernel.model_turn":
                turn_count += 1
                turn = decode_provider_result(_wire_object(comp_ev.payload["result"], "model turn"))
                u = turn.usage
                if u is not None and u.input_tokens is not None:
                    total_input += u.input_tokens
                else:
                    unknown_input = True

                if u is not None and u.output_tokens is not None:
                    total_output += u.output_tokens
                else:
                    unknown_output = True

                if u is not None and u.cache_read_tokens is not None:
                    total_cache_read += u.cache_read_tokens
                else:
                    unknown_cache = True

                if u is not None and u.uncached_input_tokens is not None:
                    total_uncached_input += u.uncached_input_tokens
                else:
                    unknown_uncached = True

                # One canonical usage DTO carries cost; the event envelope may also
                # carry it, and either source is truthful for this dispatch.
                cost = comp_ev.payload.get("cost_usd")
                if cost is None and u is not None and u.cost_usd is not None:
                    cost = u.cost_usd
                if cost is not None and isinstance(cost, (int, float)):
                    total_cost += float(cost)
                else:
                    unknown_cost = True

                ms = comp_ev.payload.get("elapsed_ms")
                if ms is not None and isinstance(ms, (int, float)):
                    total_elapsed_ms += int(ms)
                else:
                    unknown_elapsed = True

        through_seq = s.last_event_sequence
        return SessionUsageReport(
            session_id=session_id,
            input_tokens=None if unknown_input else total_input,
            output_tokens=None if unknown_output else total_output,
            cache_read_tokens=None if unknown_cache else total_cache_read,
            uncached_input_tokens=None if unknown_uncached else total_uncached_input,
            total_cost_usd=None if unknown_cost else total_cost,
            has_unknown_usage=unknown_input or unknown_output,
            turn_count=turn_count,
            elapsed_seconds=None if unknown_elapsed else (total_elapsed_ms / 1000.0),
            since_sequence=since_sequence,
            until_sequence=until_sequence,
            through_sequence=through_seq,
        )

    def _get_owner_budget(self, session_id: str, owner: str) -> PluginBudget | None:
        return self.store.owner_budget(session_id, owner)

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object],
        *,
        client_capabilities: frozenset[str] = frozenset(),
    ) -> CommandResult:
        """Execute a command, handling core commands, policy checks, confirmation, and CAS commit."""
        self._ensure_open()
        s = self.session(session_id)

        # 1. Dispatch core reserved commands through the same typed contract the
        # catalog advertises: the session id is outer authority, never an argument.
        if name in _CORE_COMMAND_NAMES:
            core_schema = next(spec[2] for spec in _CORE_COMMAND_SPECS if spec[0] == name)
            try:
                jsonschema.Draft202012Validator(json_wire_object(core_schema)).validate(json_wire_object(arguments))
            except jsonschema.ValidationError as val_err:
                return CommandResult(status="error", error=f"Command arguments failed schema: {val_err.message}")
            if name == "run":
                prompt = arguments["prompt"]
                if not isinstance(prompt, str):
                    return CommandResult(status="error", error="Command arguments failed schema: prompt must be a string")
                return self._core_result(self.run(prompt, session_id=session_id))
            if name == "resume":
                request_id = arguments.get("approval_request_id")
                decision_raw = arguments.get("decision")
                decision: Literal["allow", "deny"] | None = None
                if decision_raw is not None:
                    if decision_raw not in ("allow", "deny"):
                        return CommandResult(status="error", error="Command arguments failed schema: decision must be allow or deny")
                    decision = "allow" if decision_raw == "allow" else "deny"
                return self._core_result(
                    self.resume(
                        session_id,
                        approval_request_id=str(request_id) if request_id is not None else None,
                        decision=decision,
                    )
                )
            if name == "show":
                data = self.show(session_id, events=bool(arguments.get("events", False)))
                return CommandResult(status="ok", data=data)
            if name == "status":
                return CommandResult(status="ok", data=self.show(session_id, events=False))
            return CommandResult(status="ok", data={"commands": [c.as_payload() for c in self.command_catalog()]})

        # 2. Plugin command lookup
        cmd_desc = self.host._command_map.get(name)
        if cmd_desc is None:
            raise ValueError(f"Unknown command: {name}")

        # 3. Busy gate check
        if cmd_desc.busy_gate and s.status not in {"idle", "completed", "failed"}:
            raise ValueError(f"Session {session_id} is busy and command '{name}' requires an idle session")

        # 4. Client capabilities check
        req_client_caps = frozenset(cmd_desc.client_capabilities)
        if not req_client_caps.issubset(client_capabilities):
            missing = req_client_caps - client_capabilities
            raise PermissionError(f"Client lacks required capabilities for command '{name}': missing {set(missing)}")

        # 5. Core read-only policy check: external mutation only, so reads and
        # internal SESSION state operations stay available in every mode.
        self._check_session(s)
        if self.config.policy.read_only and has_external_mutations(frozenset(cmd_desc.effects)):
            return CommandResult(status="error", error="read-only policy denies command write effects")

        # 6. Schema validation of arguments
        if cmd_desc.argument_schema:
            try:
                jsonschema.Draft202012Validator(json_wire_object(cmd_desc.argument_schema)).validate(json_wire_object(arguments))
            except jsonschema.ValidationError as val_err:
                return CommandResult(status="error", error=f"Command arguments failed schema: {val_err.message}")

        # 8. Execution
        run_id = s.run_id or uuid4().hex
        inv_id = uuid4().hex
        command_caps = frozenset(cmd_desc.capabilities)
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=inv_id,
            revision_id=self.revision_id,
            actor="client",
            capabilities=command_caps,
            logical_turn=uuid4().hex,
        )
        latest_sess = self.store.session(session_id)
        intent_event = EventDraft(
            event_type="kernel.command_intent",
            source="client",
            actor=context.actor,
            payload={
                "name": name,
                "arguments": json_wire_object(arguments),
                "invocation_id": inv_id,
                "owner": cmd_desc.owner,
            },
        )
        self.store.commit_staged(
            session_id,
            expected_sequence=latest_sess.last_event_sequence,
            expected_run_id=latest_sess.run_id,
            run_id=latest_sess.run_id or run_id,
            revision_id=self.revision_id,
            events=(intent_event,),
            status=latest_sess.status,
            checkpoint_kind=None,
            checkpoint_payload=None,
        )

        cmd_result, staged_state, staged_events, staged_continuations, staged_cancellations, staged_budget = self.host.invoke_command(
            session_id,
            name,
            arguments,
            context=context,
            client_capabilities=client_capabilities,
        )

        if cmd_result.status == "pending_confirmation" and cmd_result.confirmation is not None:
            req = cmd_result.confirmation
            self.store.create_confirmation(
                request_id=req.request_id,
                session_id=session_id,
                owner=cmd_desc.owner,
                command_name=name,
                arguments=arguments,
                revision_id=self.revision_id,
                ui_type=req.ui_type,
                ui_data=req.ui_data,
                expected_state_version=req.expected_state_version,
            )
            return cmd_result

        # Handle budget update
        staged_budget_updates: list[StagedBudgetUpdate] = []
        if staged_budget == "clear":
            staged_budget_updates.append(StagedBudgetUpdate(owner=cmd_desc.owner, action="clear"))
        elif isinstance(staged_budget, PluginBudget):
            staged_budget_updates.append(StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget))

        # Commit staged state/events if ok OR intentional error (e.g. goal blocked)
        if cmd_result.status in ("ok", "error"):
            latest_session = self.store.session(session_id)
            completion_event = EventDraft(
                event_type="kernel.command_completed",
                source=f"plugin.{cmd_desc.owner}",
                actor=context.actor,
                payload={"name": name, "result": cmd_result.as_payload()},
            )
            all_events = tuple(staged_events) + (completion_event,)
            self.store.commit_staged(
                session_id,
                expected_sequence=latest_session.last_event_sequence,
                expected_run_id=latest_session.run_id,
                run_id=latest_session.run_id or run_id,
                revision_id=self.revision_id,
                events=all_events,
                status=latest_session.status,
                checkpoint_kind=None,
                checkpoint_payload=None,
                staged_state=staged_state,
                staged_continuations=staged_continuations,
                staged_cancellations=staged_cancellations,
                staged_budgets=tuple(staged_budget_updates),
            )
            if self.auto_drain and staged_continuations:
                latest_sess = self.store.session(session_id)
                if self._admits_fresh_work(latest_sess):
                    self.drain_continuations(session_id)
            if staged_cancellations:
                self._owner_cancel_claimed_work(
                    session_id,
                    staged_cancellations,
                    f"owner {cmd_desc.owner} cancelled its durable work",
                )

        return cmd_result

    def confirm_action(
        self,
        session_id: str,
        request_id: str,
        decision: Literal["allow", "deny"],
        *,
        client_capabilities: frozenset[str] = frozenset(),
    ) -> CommandResult:
        """Resolve a pending confirmation request by exact ID and state version."""
        self._ensure_open()
        s = self.session(session_id)
        rec = self.store.confirmation(request_id)
        if rec is None or rec.session_id != session_id:
            raise ValueError(f"Confirmation request {request_id!r} not found for session {session_id!r}")
        if rec.status != "pending":
            raise ValueError(f"Confirmation request {request_id!r} is not pending (status: {rec.status})")
        if rec.revision_id != self.revision_id:
            raise ValueError("Confirmation request belongs to another revision")

        if rec.expected_state_version is not None:
            _, curr_ver = self.store.plugin_state(rec.owner, "session", session_id)
            if curr_ver != rec.expected_state_version:
                self.store.decide_confirmation(request_id, "stale")
                raise ValueError(f"Confirmation request is stale: expected state version {rec.expected_state_version}, got {curr_ver}")

        if decision == "deny":
            self.store.decide_confirmation(request_id, "denied")
            return CommandResult(status="ok", output="Action denied by user")

        self.store.decide_confirmation(request_id, "allowed")
        cmd_desc = self.host._command_map.get(rec.command_name)
        if cmd_desc is None:
            raise ValueError(f"Command '{rec.command_name}' is not admitted")
        if rec.owner != cmd_desc.owner:
            raise ValueError("Confirmation request owner does not match the admitted command owner")

        self._check_session(s)
        run_id = s.run_id or uuid4().hex
        inv_id = uuid4().hex
        command_caps = frozenset(cmd_desc.capabilities)
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=inv_id,
            revision_id=self.revision_id,
            actor="user",
            capabilities=command_caps,
            logical_turn=uuid4().hex,
        )

        cmd_result, staged_state, staged_events, staged_continuations, staged_cancellations, staged_budget = self.host.invoke_command(
            session_id,
            rec.command_name,
            rec.arguments,
            context=context,
            client_capabilities=client_capabilities,
        )

        staged_budget_updates = []
        if staged_budget == "clear":
            staged_budget_updates.append(StagedBudgetUpdate(owner=cmd_desc.owner, action="clear"))
        elif isinstance(staged_budget, PluginBudget):
            staged_budget_updates.append(StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget))

        if cmd_result.status in ("ok", "error"):
            latest_session = self.store.session(session_id)
            completion_event = EventDraft(
                event_type="kernel.command_completed",
                source=f"plugin.{cmd_desc.owner}",
                actor="user",
                payload={"name": rec.command_name, "result": cmd_result.as_payload()},
            )
            all_events = tuple(staged_events) + (completion_event,)
            self.store.commit_staged(
                session_id,
                expected_sequence=latest_session.last_event_sequence,
                expected_run_id=latest_session.run_id,
                run_id=latest_session.run_id or run_id,
                revision_id=self.revision_id,
                events=all_events,
                status=latest_session.status,
                checkpoint_kind=None,
                checkpoint_payload=None,
                staged_state=staged_state,
                staged_continuations=staged_continuations,
                staged_cancellations=staged_cancellations,
                staged_budgets=tuple(staged_budget_updates),
            )
            if self.auto_drain and staged_continuations:
                latest_sess = self.store.session(session_id)
                if self._admits_fresh_work(latest_sess):
                    self.drain_continuations(session_id)
            if staged_cancellations:
                self._owner_cancel_claimed_work(
                    session_id,
                    staged_cancellations,
                    f"owner {cmd_desc.owner} cancelled its durable work",
                )

        return cmd_result

    def plugin_views(
        self,
        session_id: str,
        *,
        client_capabilities: frozenset[str] = frozenset(),
    ) -> tuple[PluginViewDescriptor, ...]:
        """Render declarative views contributed by admitted plugins for the session."""
        self._ensure_open()
        session = self.session(session_id)
        views: list[PluginViewDescriptor] = []
        for view_name in self.host.views:
            try:
                v = self.host.render_view(session_id, view_name, run_id=session.run_id or "", client_capabilities=client_capabilities)
                views.append(v)
            except Exception as exc:
                owner = self.host.view_owner(view_name)
                views.append(
                    PluginViewDescriptor(
                        owner=owner,
                        name=view_name,
                        title=f"{view_name} (error)",
                        view_type="notice",
                        data={"message": f"View {view_name} failed: {type(exc).__name__}", "level": "error"},
                    )
                )
        return tuple(views)

    def drain_continuations(
        self,
        session_id: str,
        *,
        limit: int = 10,
        max_steps: int = 32,
    ) -> tuple[ContinuationResult, ...]:
        """Claim and execute pending continuations as governed runs with plugin provenance."""
        self._validate_steps(max_steps)
        with self._run_lock:
            self._ensure_open()
            self._abort.set_cancelled(False)
            s = self.session(session_id)
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = abort

            results: list[ContinuationResult] = []
            try:
                for _ in range(limit):
                    run_id = uuid4().hex
                    logical_turn = uuid4().hex
                    claimed = self.store.claimed_continuations(session_id, limit=1)
                    recovering = bool(claimed)
                    if recovering:
                        cont_record = claimed[0]
                        run_id = cont_record.run_id
                    else:
                        if s.status in ("running", "waiting"):
                            break
                        try:
                            cont_record, session_record = self.store.claim_and_start_continuation(
                                session_id,
                                run_id,
                                logical_turn,
                                revision_id=self.revision_id,
                                chain_step=0,
                                action_budget_remaining=max_steps,
                            )
                        except StoreConflict:
                            break

                    if recovering:
                        checkpoint = self.store.checkpoint(session_id)
                        if checkpoint is None or checkpoint.kind == "terminal":
                            self.store.complete_continuation(cont_record.continuation_id, status="failed")
                            results.append(
                                ContinuationResult(
                                    continuation_id=cont_record.continuation_id,
                                    owner=cont_record.owner,
                                    session_id=session_id,
                                    status="failed",
                                    payload=cont_record.payload,
                                    run_status="failed",
                                    run_id=run_id,
                                    error="claimed continuation has no resumable checkpoint",
                                    stop_cause="error",
                                )
                            )
                            break
                        cp = checkpoint.payload if isinstance(checkpoint.payload, Mapping) else {}
                        cursor = _Cursor.model_validate(json_wire_object(cp))
                        if not cursor.root_id:
                            cursor.root_id = cont_record.root_id or cont_record.continuation_id
                        self.store.ensure_root_budget(cursor.root_id, max_steps)
                        run_id = checkpoint.run_id
                        session_record = self.store.session(session_id)
                    else:
                        cur_session = self.store.session(session_id)
                        root_id = cont_record.root_id or cont_record.continuation_id
                        self.store.ensure_root_budget(root_id, max_steps)
                        cursor = _Cursor(
                            continuation_id=cont_record.continuation_id,
                            root_id=root_id,
                            logical_turn=logical_turn,
                            chain_step=0,
                            action_budget_remaining=max_steps,
                        )
                        session_record = cur_session

                    execution = _Execution(session_record, cursor, list(self.store.events(session_id)), run_id)
                    root_state = self.store.root_budget_state(cursor.root_id) if cursor.root_id else None
                    if root_state is not None and root_state[1] >= root_state[0]:
                        run_res = self._stop(execution, "root action budget exhausted", stop_cause="limit_reached")
                    else:
                        per_claim = max_steps
                        if root_state is not None:
                            per_claim = max(1, min(max_steps, root_state[0] - root_state[1]))
                        run_res = self._drive(execution, max_steps=per_claim)

                    status_str: Literal["pending", "claimed", "completed", "failed", "cancelled"] = "completed"
                    if run_res.status in ("completed", "idle"):
                        status_str = "completed"
                    elif run_res.status == "failed":
                        status_str = "failed"
                    elif run_res.status == "waiting":
                        status_str = "claimed"
                    elif run_res.status == "interrupted":
                        if run_res.stop_cause in ("user_stop", "owner_cancel"):
                            status_str = "cancelled"
                        else:
                            status_str = "claimed"

                    if status_str != "claimed":
                        self.store.complete_continuation(cont_record.continuation_id, status=status_str)

                    results.append(
                        ContinuationResult(
                            continuation_id=cont_record.continuation_id,
                            owner=cont_record.owner,
                            session_id=session_id,
                            status=status_str,
                            payload=cont_record.payload,
                            run_status=run_res.status,
                            run_id=run_res.run_id,
                            output=run_res.output,
                            error=run_res.error,
                            stop_cause=run_res.stop_cause,
                        )
                    )
                    if run_res.status in ("interrupted", "failed", "waiting"):
                        break
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)

            return tuple(results)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("kernel is closed")

    def _check_session(self, session: SessionRecord) -> None:
        if session.workspace != str(self.workspace) or session.active_revision_id != self.revision_id:
            raise ValueError("session workspace/revision differs; implicit capability upgrades are forbidden")

    @staticmethod
    def _event(execution: _Execution, sequence: int, kind: str) -> StoredEvent:
        if sequence < 1 or sequence > len(execution.events):
            raise ValueError("checkpoint references a missing event")
        event = execution.events[sequence - 1]
        if event.sequence != sequence or event.event_type != kind:
            raise ValueError("checkpoint references the wrong event kind")
        return event

    def _settlement_candidates(self, session_id: str) -> list[StoredEvent]:
        """Events needing observer delivery: terminal facts plus unsettled dispatches.

        A dispatch intent with no completion event means the process died after
        requesting a turn; it must settle as an unknown-cost usage fact exactly
        once (receipts dedupe it) instead of leaving consumers with stale totals.
        """
        events = self.store.events(session_id)
        completed_dispatches: set[int] = set()
        for event in events:
            if event.event_type in ("kernel.model_turn", "kernel.provider_interrupted"):
                dispatch_seq = event.payload.get("dispatch_sequence")
                if isinstance(dispatch_seq, (int, float)):
                    completed_dispatches.add(int(dispatch_seq))
        pending: list[StoredEvent] = []
        for event in events:
            if event.event_type == "kernel.provider_intent":
                if event.sequence not in completed_dispatches:
                    pending.append(event)
            elif event.event_type in {
                "kernel.model_turn",
                "kernel.provider_interrupted",
                "kernel.tool_completed",
                "kernel.tool_interrupted",
                "kernel.run_completed",
                "kernel.run_failed",
                "kernel.run_interrupted",
            }:
                pending.append(event)
        return pending

    def _replay_pending_observers(self) -> None:
        """Deliver facts whose observer receipts never committed before accepting new work.

        Only this kernel's workspace and active revision are touched, and only
        owners missing a receipt for that exact source event are notified, so a
        replay can never double-fire an observer or fire it under another
        workspace's identity.
        """
        for session in self.store.sessions():
            if session.workspace != str(self.workspace) or session.active_revision_id != self.revision_id:
                continue
            pending = self._settlement_candidates(session.session_id)
            if not pending:
                continue
            checkpoint = self.store.checkpoint(session.session_id)
            cursor = (
                _Cursor.model_validate(json_wire_object(checkpoint.payload))
                if checkpoint is not None and isinstance(checkpoint.payload, Mapping)
                else _Cursor()
            )
            execution = _Execution(session, cursor, list(self.store.events(session.session_id)), session.run_id or uuid4().hex)
            for ev in pending:
                self._deliver_fact_for_event(execution, ev)

    def _deliver_fact_for_event(self, execution: _Execution, ev: StoredEvent) -> None:
        """Notify exactly the subscribed owners still missing a receipt, then persist those receipts."""
        fact = self._fact_for(execution, ev)
        if fact is None:
            return
        fact_type, fact_payload = fact
        session_id = ev.session_id
        subscribed = self.host._observer_owners.get(fact_type, ())
        active = [o for o in subscribed if not self.store.has_observer_receipt(session_id, ev.sequence, o, 1)]
        if not active:
            return
        logical_turn = str(fact_payload.get("logical_turn", ""))
        obs_res = self.host.notify_fact(fact_type, fact_payload, owners=tuple(active), logical_turn=logical_turn)
        receipts = tuple(ObserverReceiptRecord(event_sequence=ev.sequence, owner=o, generation=1) for o in active)
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
                        "kernel.observer_error",
                        source=f"plugin.{err.get('owner', 'observer')}",
                        actor="plugin",
                        payload={"owner": err.get("owner"), "error": err.get("error")},
                    )
                )
        self._commit_staged_rebased(
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

    def _settle_unsettled_dispatches(self, execution: _Execution) -> None:
        """Settle dispatches of this run that never produced a completion event."""
        completed: set[int] = set()
        for event in execution.events:
            if event.event_type in ("kernel.model_turn", "kernel.provider_interrupted"):
                dispatch_seq = event.payload.get("dispatch_sequence")
                if isinstance(dispatch_seq, (int, float)):
                    completed.add(int(dispatch_seq))
        for event in execution.events:
            if event.event_type == "kernel.provider_intent" and event.sequence not in completed:
                self._deliver_fact_for_event(execution, event)

    def _commit_staged_rebased(
        self,
        execution: _Execution,
        *,
        status: str,
        kind: str | None,
        events: Sequence[EventDraft] = (),
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: Literal["completed", "failed", "cancelled"] = "completed",
        reserve_root: bool = False,
        start_turn: bool = False,
        checkpoint_bindings: Sequence[tuple[str, str]] = (),
    ) -> tuple[StoredEvent, ...]:
        """Commit against the fresh session tip, validating any intervening parked events first.

        Continuation drafts enqueued during this run inherit its root action chain,
        an optional root reservation happens inside the same transaction as the
        intent event it pays for, and bound checkpoint references adopt the actual
        appended sequences before any observer sees the new state.
        """
        previous = execution.session
        fresh = self.store.session(previous.session_id)
        if fresh.last_event_sequence > previous.last_event_sequence:
            intervening = self.store.events(previous.session_id, after_sequence=previous.last_event_sequence)
            for ev in intervening:
                if not _is_parked_intervening(ev):
                    raise ValueError(f"intervening event {ev.event_type} violates the parked checkpoint prefix")
            execution.events.extend(intervening)
            previous = fresh
        root_id = execution.cursor.root_id
        root_reservation: tuple[str, int] | None = None
        if reserve_root and root_id:
            root_budget = execution.cursor.action_budget_remaining
            root_reservation = (root_id, max(0, root_budget if root_budget is not None else 32))
        stamped_continuations = tuple(
            draft if draft.root_id is not None or not root_id else replace(draft, root_id=root_id) for draft in staged_continuations
        )
        execution.session = self.store.commit_staged(
            previous.session_id,
            expected_sequence=previous.last_event_sequence,
            expected_run_id=previous.run_id,
            run_id=execution.run_id,
            revision_id=self.revision_id,
            events=tuple(events),
            status=status,
            checkpoint_kind=kind,
            checkpoint_payload=execution.cursor.model_dump() if kind is not None else None,
            root_reservation=root_reservation,
            staged_state=staged_state,
            staged_continuations=stamped_continuations,
            staged_cancellations=staged_cancellations,
            staged_budgets=staged_budgets,
            observer_receipts=observer_receipts,
            completed_continuation_id=completed_continuation_id,
            completed_continuation_status=completed_continuation_status,
            start_turn=start_turn,
            checkpoint_bindings=checkpoint_bindings,
        )
        new_events = self.store.events(previous.session_id, after_sequence=previous.last_event_sequence)
        execution.events.extend(new_events)
        self._bind_checkpoint_refs(execution.cursor, new_events, checkpoint_bindings)
        return new_events

    @staticmethod
    def _bind_checkpoint_refs(
        cursor: _Cursor,
        events: Sequence[StoredEvent],
        bindings: Sequence[tuple[str, str]],
    ) -> None:
        """Adopt the durable event references this commit bound into the checkpoint.

        The store wrote exactly these sequences inside its transaction; the
        in-memory cursor must agree before any observer callback reads it.
        """
        for bound_field, bound_event_type in bindings:
            sequence = next((ev.sequence for ev in events if ev.event_type == bound_event_type), None)
            if sequence is None:
                raise ValueError(f"checkpoint binding {bound_field!r} has no committed {bound_event_type!r} event")
            setattr(cursor, bound_field, sequence)

    def _fact_for(self, execution: _Execution, ev: StoredEvent) -> tuple[str, dict[str, object]] | None:
        if ev.event_type == "kernel.provider_intent":
            # Never-settled dispatch: report an unknown-usage fact for exactly
            # this intent so consumers see unknown, not a stale known total.
            provider_name = ev.payload.get("provider")
            model_name = ev.payload.get("model")
            if not isinstance(provider_name, str) or not provider_name or not isinstance(model_name, str) or not model_name:
                raise ValueError(f"provider intent at {ev.sequence} lacks an authentic provider/model identity")
            # A dispatch with no completion event has no measured provider
            # duration: elapsed stays unknown rather than counting dead-process
            # downtime as observed provider seconds.
            return "usage", {
                "session_id": ev.session_id,
                "run_id": ev.run_id,
                "revision_id": ev.revision_id,
                "logical_turn": str(ev.payload.get("logical_turn", "")),
                "event_sequence": ev.sequence,
                "dispatch_sequence": ev.sequence,
                "provider_name": provider_name,
                "model_name": model_name,
                "usage": None,
                "elapsed_seconds": None,
                "cost_usd": None,
            }
        if ev.event_type in ("kernel.model_turn", "kernel.provider_interrupted"):
            d_seq = ev.payload.get("dispatch_sequence")
            disp_seq_val = int(d_seq) if isinstance(d_seq, (int, float)) else None
            if disp_seq_val is None:
                raise ValueError(f"usage fact at sequence {ev.sequence} has no dispatch sequence")
            intent_ev = next(
                (e for e in execution.events if e.sequence == disp_seq_val and e.event_type == "kernel.provider_intent"),
                None,
            )
            if intent_ev is None:
                raise ValueError(f"usage fact at sequence {ev.sequence} has no provider intent at {disp_seq_val}")
            provider_name = intent_ev.payload.get("provider")
            model_name = intent_ev.payload.get("model")
            logical_turn = intent_ev.payload.get("logical_turn")
            if not isinstance(provider_name, str) or not provider_name or not isinstance(model_name, str) or not model_name:
                raise ValueError(f"provider intent at {disp_seq_val} lacks an authentic provider/model identity")
            ms = ev.payload.get("elapsed_ms")
            elapsed_sec = (ms / 1000.0) if ms is not None and isinstance(ms, (int, float)) else None
            usage_payload: Mapping[str, object] | None = None
            cost_usd: float | None = None
            if ev.event_type == "kernel.model_turn":
                turn = decode_provider_result(_wire_object(ev.payload["result"], "model turn"))
                usage_payload = turn.usage.metadata_payload() if turn.usage else None
                cost_usd = turn.usage.cost_usd if turn.usage is not None else None
            return "usage", {
                "session_id": ev.session_id,
                "run_id": ev.run_id,
                "revision_id": ev.revision_id,
                "logical_turn": str(logical_turn) if isinstance(logical_turn, str) else "",
                "event_sequence": ev.sequence,
                "dispatch_sequence": disp_seq_val,
                "provider_name": provider_name,
                "model_name": model_name,
                "usage": usage_payload,
                "elapsed_seconds": elapsed_sec,
                "cost_usd": cost_usd,
            }
        if ev.event_type in ("kernel.tool_completed", "kernel.tool_interrupted"):
            call_data = ev.payload.get("call")
            call_dict = call_data if isinstance(call_data, Mapping) else {}
            res_data = ev.payload.get("result")
            res_dict = res_data if isinstance(res_data, Mapping) else {}
            tool_status = "error" if (ev.event_type == "kernel.tool_interrupted" or res_dict.get("status") == "error") else "ok"
            return "invocation_finished", {
                "session_id": ev.session_id,
                "run_id": ev.run_id,
                "revision_id": ev.revision_id,
                "logical_turn": str(ev.payload.get("logical_turn", "")),
                "event_sequence": ev.sequence,
                "tool_name": str(call_dict.get("tool_name", "")),
                "tool_call_id": call_dict.get("tool_call_id"),
                "status": tool_status,
                "error": str(res_dict["error"]) if "error" in res_dict and res_dict["error"] is not None else None,
            }
        if ev.event_type in ("kernel.run_completed", "kernel.run_failed", "kernel.run_interrupted"):
            if ev.event_type == "kernel.run_interrupted":
                stop_cause = ev.payload.get("stop_cause")
                if stop_cause not in get_args(StopCause):
                    raise ValueError(f"interrupted run event at {ev.sequence} lacks an authentic stop cause")
            elif ev.event_type == "kernel.run_failed":
                stop_cause = "error"
            else:
                stop_cause = "completed"
            # The originating process measured this duration and persisted it with
            # the event; replaying a historical run must never re-measure it.
            measured_ms = ev.payload.get("elapsed_ms")
            elapsed_seconds = measured_ms / 1000.0 if isinstance(measured_ms, (int, float)) and not isinstance(measured_ms, bool) else None
            return "run_finished", {
                "session_id": ev.session_id,
                "run_id": ev.run_id,
                "revision_id": ev.revision_id,
                "logical_turn": str(ev.payload.get("logical_turn", "")),
                "event_sequence": ev.sequence,
                "status": ev.event_type.replace("kernel.run_", ""),
                "stop_cause": stop_cause,
                "elapsed_seconds": elapsed_seconds,
            }
        return None

    def _measured_run_elapsed_fields(self, execution: _Execution) -> dict[str, int]:
        """Stamp the duration this process actually measured for the run, if it dispatched at all."""
        starts: list[float] = []
        for event in execution.events:
            if event.run_id != execution.run_id or event.event_type != "kernel.provider_intent":
                continue
            start_ms = event.payload.get("start_time_ms")
            if isinstance(start_ms, (int, float)) and not isinstance(start_ms, bool):
                starts.append(float(start_ms))
        if not starts:
            return {}
        return {"elapsed_ms": max(0, int(time.time() * 1000 - min(starts)))}

    def _commit(
        self,
        execution: _Execution,
        status: str,
        kind: str,
        *events: EventDraft,
        start_turn: bool = False,
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: Literal["completed", "failed", "cancelled"] = "completed",
        reserve_root: bool = False,
        checkpoint_bindings: Sequence[tuple[str, str]] = (),
    ) -> tuple[StoredEvent, ...]:
        new_events = self._commit_staged_rebased(
            execution,
            status=status,
            kind=kind,
            events=events,
            reserve_root=reserve_root,
            checkpoint_bindings=checkpoint_bindings,
            staged_state=staged_state,
            staged_continuations=staged_continuations,
            staged_cancellations=staged_cancellations,
            staged_budgets=staged_budgets,
            completed_continuation_id=completed_continuation_id,
            completed_continuation_status=completed_continuation_status,
            start_turn=start_turn,
        )
        # Each observed fact commits its own owner changes and receipts atomically,
        # so the next fact callback reads the state its predecessor committed.
        for ev in new_events:
            if ev.event_type == "kernel.provider_intent":
                # Intents settle only if they never complete; completions carry the fact.
                continue
            fact = self._fact_for(execution, ev)
            if fact is None:
                continue
            fact_type, fact_payload = fact
            subscribed = self.host._observer_owners.get(fact_type, ())
            active_owners = [o for o in subscribed if not self.store.has_observer_receipt(ev.session_id, ev.sequence, o, 1)]
            if not active_owners:
                continue
            obs_res = self.host.notify_fact(
                fact_type,
                fact_payload,
                owners=tuple(active_owners),
                logical_turn=str(fact_payload.get("logical_turn", "")),
            )
            receipts = tuple(ObserverReceiptRecord(event_sequence=ev.sequence, owner=o, generation=1) for o in active_owners)
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
                            "kernel.observer_error",
                            source=f"plugin.{err.get('owner', 'observer')}",
                            actor="plugin",
                            payload={"owner": err.get("owner"), "error": err.get("error")},
                        )
                    )
                try:
                    self._commit_staged_rebased(
                        execution,
                        status=execution.session.status,
                        kind=None,
                        events=tuple(staged_events),
                        staged_state=obs_res.staged_state,
                        staged_continuations=obs_res.staged_continuations,
                        staged_cancellations=obs_res.staged_cancellations,
                        staged_budgets=tuple(staged_budget_updates),
                        observer_receipts=receipts,
                    )
                except StoreConflict as exc:
                    # The source fact (and any bill) is already durable; a losing
                    # observer proposal must surface as an owned diagnostic rather
                    # than failing the run or silently dropping the fact.
                    try:
                        self._commit_staged_rebased(
                            execution,
                            status=execution.session.status,
                            kind=None,
                            events=(
                                EventDraft(
                                    "kernel.observer_error",
                                    source="plugin.observer",
                                    actor="plugin",
                                    payload={"owner": None, "error": f"observer proposals rejected: {exc}"},
                                ),
                            ),
                            observer_receipts=(),
                        )
                    except StoreConflict, ValueError:
                        pass
            else:
                self._commit_staged_rebased(
                    execution,
                    status=execution.session.status,
                    kind=None,
                    observer_receipts=receipts,
                )
        return new_events

    def _budget_admission(self, execution: _Execution) -> KernelResult | None:
        """Refuse dispatch when an active owner budget is already satisfied.

        Each metric is checked independently with its own unknown flag: a known
        input total with an unknown output total must still obey an input cap,
        and only the caps whose metric is genuinely unknown fail visibly.
        """
        for b in self.store.session_budgets(execution.session.session_id):
            u_report = self._get_session_usage(
                execution.session.session_id,
                since_sequence=b.since_sequence,
                session=execution.session,
            )
            if b.max_input_tokens is not None:
                if u_report.input_tokens is None:
                    return self._fail(execution, f"budget limit reached: input tokens unknown for owner {b.owner}")
                if u_report.input_tokens >= b.max_input_tokens:
                    return self._stop(
                        execution,
                        f"budget limit exceeded: input tokens {u_report.input_tokens} >= {b.max_input_tokens} for owner {b.owner}",
                        stop_cause="limit_reached",
                    )
            if b.max_output_tokens is not None:
                if u_report.output_tokens is None:
                    return self._fail(execution, f"budget limit reached: output tokens unknown for owner {b.owner}")
                if u_report.output_tokens >= b.max_output_tokens:
                    return self._stop(
                        execution,
                        f"budget limit exceeded: output tokens {u_report.output_tokens} >= {b.max_output_tokens} for owner {b.owner}",
                        stop_cause="limit_reached",
                    )
            if b.max_total_tokens is not None:
                if u_report.input_tokens is None or u_report.output_tokens is None:
                    return self._fail(execution, f"budget limit reached: total tokens unknown for owner {b.owner}")
                tot = (u_report.input_tokens or 0) + (u_report.output_tokens or 0)
                if tot >= b.max_total_tokens:
                    return self._stop(
                        execution,
                        f"budget limit exceeded: total tokens {tot} >= {b.max_total_tokens} for owner {b.owner}",
                        stop_cause="limit_reached",
                    )
            if b.max_elapsed_seconds is not None:
                if u_report.elapsed_seconds is None:
                    return self._fail(execution, f"budget limit reached: elapsed time unknown for owner {b.owner}")
                if u_report.elapsed_seconds >= b.max_elapsed_seconds:
                    return self._stop(
                        execution,
                        f"budget limit exceeded: elapsed time {u_report.elapsed_seconds}s >= {b.max_elapsed_seconds}s for owner {b.owner}",
                        stop_cause="limit_reached",
                    )
            if b.max_cost_usd is not None:
                if u_report.total_cost_usd is None:
                    return self._fail(execution, f"budget limit reached: cost unknown for owner {b.owner}")
                if u_report.total_cost_usd >= b.max_cost_usd:
                    return self._stop(
                        execution,
                        f"budget limit exceeded: cost ${u_report.total_cost_usd} >= ${b.max_cost_usd} for owner {b.owner}",
                        stop_cause="limit_reached",
                    )
        return None

    def _abort_stop(self, execution: _Execution, session_abort: Cancellation | None) -> KernelResult | None:
        """Stop the run when the kernel or its session was cancelled."""
        if session_abort is not None and session_abort.cancelled:
            return self._stop(execution, session_abort.reason or "cancelled", stop_cause=session_abort.stop_cause or "user_stop")
        if self._abort.cancelled:
            return self._stop(execution, self._abort.reason or "cancelled", stop_cause=self._abort.stop_cause or "shutdown")
        return None

    def _pending(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]:
        cursor = execution.cursor
        if cursor.batch_sequence is None:
            raise ValueError("checkpoint has no pending batch")
        event = self._event(execution, cursor.batch_sequence, "kernel.model_turn")
        turn = decode_provider_result(_wire_object(event.payload["result"], "model turn"))
        if cursor.next_call >= len(turn.tool_calls):
            raise ValueError("checkpoint is outside its native batch")
        return event, turn.tool_calls[cursor.next_call]

    def _denial(self, call: ToolCall, definition: ToolDefinition) -> str | None:
        try:
            self._validators[definition.name].validate(json_wire_object(call.arguments))
        except jsonschema.ValidationError:
            return "tool arguments failed the admitted JSON schema"
        # read_only denies EXTERNAL mutation only: reads and internal state
        # operations (SESSION) stay available in every mode.
        if self.config.policy.read_only and has_external_mutations(definition.effects):
            return "read-only policy denies the declared tool effects"
        return None

    def _needs_approval(self, definition: ToolDefinition) -> bool:
        effects = definition.effects
        if not has_external_mutations(effects):
            # Reads and internal state operations (SESSION) mutate nothing
            # external, so no mode gates them behind a human decision.
            return False
        mode = self.config.policy.approval_mode
        if mode == "yolo":
            return False
        return not (mode == "write" and effects and effects <= {ToolEffect.READ, ToolEffect.WRITE})

    @staticmethod
    def _validate_steps(max_steps: int) -> None:
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")

    def _drive(self, execution: _Execution, *, max_steps: int) -> KernelResult:
        while True:
            session_abort = None
            with self._aborts_lock:
                session_abort = self._session_aborts.get(execution.session.session_id)
            if (session_abort and session_abort.cancelled) or self._abort.cancelled:
                cause = (session_abort.stop_cause if session_abort and session_abort.cancelled else "shutdown") or "user_stop"
                reason = (session_abort.reason if session_abort and session_abort.cancelled else self._abort.reason) or "cancelled"
                return self._stop(execution, reason, stop_cause=cause)
            cursor = execution.cursor
            if cursor.batch_sequence is None:
                if cursor.step >= max_steps:
                    return self._stop(execution, "provider turn budget exhausted", stop_cause="limit_reached")

                budget_stop = self._budget_admission(execution)
                if budget_stop is not None:
                    return budget_stop

                model = self.config.model.model
                if model is None:
                    return self._fail(execution, "no model configured")
                provider_name, separator, model_name = model.partition("/")
                entry = self.config.providers.get(provider_name)
                if not separator or not model_name or entry is None:
                    return self._fail(execution, "model must name a configured provider/model")
                # Settle any earlier dispatch of this run that never completed BEFORE
                # context assembly: observers see unknown usage exactly once, and any
                # budget they set (even a zero cap) or cancellation they raise is
                # already durable when the admission checks below read it.
                self._settle_unsettled_dispatches(execution)
                try:
                    messages = self._messages(execution)
                except ValueError as exc:
                    return self._fail(execution, str(exc))
                except (RuntimeError, TimeoutError) as exc:
                    # Critical context/admission failure: the run finishes durably
                    # failed before any intent is committed or provider is billed.
                    return self._fail(execution, f"context assembly failed: {type(exc).__name__}: {exc}")

                effective_abort = session_abort or self._abort
                request = ProviderTurnRequest(
                    messages=messages,
                    available_tools=self.host.tools,
                    raw_model=model,
                    provider_name=provider_name,
                    model_name=model_name,
                    reasoning_effort=self.config.model.reasoning_effort,
                    abort_signal=effective_abort,
                )

                dispatch_id = uuid4().hex
                start_time_ms = int(time.time() * 1000)
                # Final admission: context assembly may have taken a long RPC, so
                # re-check cancellation and budgets immediately before the native
                # call that reserves the root step and bills the provider.
                late_abort = self._abort_stop(execution, session_abort)
                if late_abort is not None:
                    return late_abort
                late_budget = self._budget_admission(execution)
                if late_budget is not None:
                    return late_budget
                # A fresh provider dispatch is a new logical turn. The current
                # native batch keeps this identity (checkpointed), so approvals,
                # tool reports, and resumes all attribute to the same turn.
                cursor.logical_turn = uuid4().hex
                try:
                    intent_events = self._commit(
                        execution,
                        "running",
                        "ready",
                        EventDraft(
                            "kernel.provider_intent",
                            "kernel",
                            "kernel",
                            {
                                "attempt": cursor.step,
                                "provider": provider_name,
                                "model": model_name,
                                "dispatch_id": dispatch_id,
                                "start_time_ms": start_time_ms,
                                "logical_turn": execution.cursor.logical_turn,
                            },
                        ),
                        reserve_root=True,
                    )
                except StoreConflict:
                    return self._stop(execution, "root action budget exhausted", stop_cause="limit_reached")
                intent_seq = next(ev.sequence for ev in intent_events if ev.event_type == "kernel.provider_intent")

                t0 = time.monotonic()
                try:
                    turn = self.host.propose_turn(request, provider=entry.wire)
                    elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                except (HostUnavailable, HostTimeout, InvocationCancelled) as exc:
                    elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                    self._commit(
                        execution,
                        "interrupted",
                        "interrupted",
                        EventDraft(
                            "kernel.provider_interrupted",
                            "kernel",
                            "kernel",
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
                    # Only a real cancellation may report a user decision; a host
                    # timeout or kill is a service failure, never a user stop.
                    if effective_abort.cancelled:
                        return self._stop(
                            execution,
                            str(exc),
                            stop_cause=effective_abort.stop_cause or "user_stop",
                        )
                    return self._stop(
                        execution,
                        str(exc),
                        stop_cause="host_loss" if isinstance(exc, HostUnavailable) else "error",
                    )
                except Exception as exc:
                    elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                    self._commit(
                        execution,
                        "interrupted",
                        "interrupted",
                        EventDraft(
                            "kernel.provider_interrupted",
                            "kernel",
                            "kernel",
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
                    return self._fail(execution, f"provider failed: {type(exc).__name__}: {exc}")

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
                if has_duplicate_or_missing_ids:
                    self._commit(
                        execution,
                        "failed",
                        "terminal",
                        EventDraft("kernel.model_turn", "provider", "model", model_turn_payload),
                        completed_continuation_id=execution.cursor.continuation_id,
                        completed_continuation_status="failed",
                    )
                    return self._fail(execution, "provider returned missing/duplicate native call identities")

                if invalid_done_reason:
                    self._commit(
                        execution,
                        "failed",
                        "terminal",
                        EventDraft("kernel.model_turn", "provider", "model", model_turn_payload),
                        completed_continuation_id=execution.cursor.continuation_id,
                        completed_continuation_status="failed",
                    )
                    return self._fail(execution, f"provider did not complete its turn: {turn.done_reason}")

                if turn.tool_calls:
                    # A native batch starts at its first member, and the checkpoint
                    # references this exact appended turn in the same transaction:
                    # a crash can never leave a batch event without its checkpoint ref.
                    cursor.next_call = 0
                    self._commit(
                        execution,
                        "running",
                        "ready",
                        EventDraft("kernel.model_turn", "provider", "model", model_turn_payload),
                        checkpoint_bindings=(("batch_sequence", "kernel.model_turn"),),
                    )
                else:
                    self._commit(
                        execution,
                        "completed",
                        "terminal",
                        EventDraft("kernel.model_turn", "provider", "model", model_turn_payload),
                        EventDraft(
                            "kernel.run_completed",
                            "kernel",
                            "kernel",
                            {"logical_turn": execution.cursor.logical_turn, **self._measured_run_elapsed_fields(execution)},
                        ),
                        completed_continuation_id=execution.cursor.continuation_id,
                        completed_continuation_status="completed",
                    )
                    return self._result(execution, output=turn.output, stop_cause="completed")
                if self._abort.cancelled or (session_abort and session_abort.cancelled):
                    cause = (session_abort.stop_cause if session_abort and session_abort.cancelled else "shutdown") or "user_stop"
                    reason = (session_abort.reason if session_abort and session_abort.cancelled else self._abort.reason) or "cancelled"
                    return self._stop(execution, reason, stop_cause=cause)
            batch, call = self._pending(execution)
            definition = self._definitions.get(call.tool_name)
            if definition is None:
                self._report(execution, call, ToolFailure(call.tool_name, "tool is not in the active capability revision"))
                continue
            denial = self._denial(call, definition)
            if denial:
                self._report(execution, call, ToolFailure(call.tool_name, denial))
                continue
            if cursor.intent_sequence is None and self._needs_approval(definition):
                payload = {
                    "request_id": uuid4().hex,
                    "batch_sequence": batch.sequence,
                    "call": encode_tool_call(call),
                    "owner": self.host.owner(call.tool_name),
                }
                self._commit(
                    execution,
                    "waiting",
                    "approval_wait",
                    EventDraft("kernel.approval_requested", "kernel", "model", payload),
                    checkpoint_bindings=(("approval_sequence", "kernel.approval_requested"),),
                )
                return self._result(execution, pending_approval=payload)
            stopped = self._invoke(execution, call, definition)
            if stopped is not None:
                return stopped

    def _invoke(self, execution: _Execution, call: ToolCall, definition: ToolDefinition) -> KernelResult | None:
        cursor = execution.cursor
        if cursor.intent_sequence is None:
            invocation_id = uuid4().hex
            self._commit(
                execution,
                "running",
                "ready",
                EventDraft(
                    "kernel.tool_intent",
                    "kernel",
                    "model",
                    {
                        "invocation_id": invocation_id,
                        "batch_sequence": cursor.batch_sequence,
                        "call": encode_tool_call(call),
                        "owner": self.host.owner(call.tool_name),
                        "effects": sorted(effect.value for effect in definition.effects),
                        "replay_policy": definition.replay_policy_for(call.arguments),
                        "logical_turn": execution.cursor.logical_turn,
                    },
                ),
                checkpoint_bindings=(("intent_sequence", "kernel.tool_intent"),),
            )
        else:
            intent = self._event(execution, cursor.intent_sequence, "kernel.tool_intent")
            invocation_id = str(intent.payload["invocation_id"])

        session_abort = None
        with self._aborts_lock:
            session_abort = self._session_aborts.get(execution.session.session_id)
        effective_abort = session_abort or self._abort

        context = ToolContext(
            workspace=self.workspace,
            session_id=execution.session.session_id,
            run_id=execution.run_id,
            invocation_id=invocation_id,
            revision_id=self.revision_id,
            actor="model",
            capabilities=self.host.capabilities(call.tool_name),
            logical_turn=execution.cursor.logical_turn,
            abort_signal=effective_abort,
        )
        try:
            outcome = self.host.invoke(call, context=context, return_outcome=True)
            if not isinstance(outcome, ToolInvocationOutcome):
                raise TypeError("plugin host returned a bare tool result for an outcome request")
            result = outcome.result
            staged_state = outcome.staged_state
            staged_events = outcome.staged_events
            staged_conts = outcome.staged_continuations
            staged_cancels = outcome.staged_cancellations
            staged_budget = outcome.staged_budget
        except (HostUnavailable, HostTimeout, InvocationCancelled) as exc:
            if effective_abort.cancelled:
                return self._stop(execution, str(exc), stop_cause=effective_abort.stop_cause or "user_stop")
            return self._stop(execution, str(exc), stop_cause="host_loss" if isinstance(exc, HostUnavailable) else "error")
        except Exception as exc:
            result = ToolFailure(call.tool_name, f"tool failed: {type(exc).__name__}: {exc}")
            staged_state = ()
            staged_events = ()
            staged_conts = ()
            staged_cancels = ()
            staged_budget = None

        if result.tool_name != call.tool_name:
            return self._stop(execution, "tool result identity differs from the dispatched native call", stop_cause="error")

        self._report(
            execution,
            call,
            result,
            staged_state=staged_state,
            staged_events=staged_events,
            staged_conts=staged_conts,
            staged_cancels=staged_cancels,
            staged_budget=staged_budget,
        )
        claim_id = execution.cursor.continuation_id
        claim_owner = self.store.continuation_owner(claim_id) if claim_id is not None else None
        if any(self._cancels_claimed(cancellation, claim_id, claim_owner) for cancellation in staged_cancels):
            # The tool cancelled its own claimed work: the effect and audit above are
            # durable, and this chain stops here with the owner cause it stated.
            return self._stop(execution, "tool cancelled its own durable work", stop_cause="owner_cancel")
        return None

    def _report(
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
    ) -> None:
        cursor = execution.cursor
        batch_sequence = cursor.batch_sequence
        cursor.next_call += 1
        cursor.intent_sequence = None
        cursor.approval_sequence = None
        turn = decode_provider_result(_wire_object(self._event(execution, batch_sequence or 0, "kernel.model_turn").payload["result"], "model turn"))
        if cursor.next_call == len(turn.tool_calls):
            cursor.batch_sequence = None
            cursor.next_call = 0
            cursor.abandon_batch = False
        payload = {
            "batch_sequence": batch_sequence,
            "call": encode_tool_call(call),
            "result": encode_tool_result(result),
            "logical_turn": execution.cursor.logical_turn,
        }
        report_event = EventDraft(
            "kernel.tool_interrupted" if interrupted else "kernel.tool_completed",
            "kernel" if interrupted else "tool",
            "model",
            payload,
        )
        all_events = (report_event,) + staged_events

        staged_budget_updates: list[StagedBudgetUpdate] = []
        if staged_budget == "clear":
            owner = self.host.owner(call.tool_name)
            staged_budget_updates.append(StagedBudgetUpdate(owner=owner, action="clear"))
        elif isinstance(staged_budget, PluginBudget):
            staged_budget_updates.append(StagedBudgetUpdate(owner=staged_budget.owner, action="set", budget=staged_budget))

        self._commit(
            execution,
            "running",
            "ready",
            *all_events,
            staged_state=staged_state,
            staged_continuations=staged_conts,
            staged_cancellations=staged_cancels,
            staged_budgets=tuple(staged_budget_updates),
        )

    def _interrupt_batch(self, execution: _Execution) -> None:
        first = execution.cursor.intent_sequence is not None
        execution.cursor.abandon_batch = True
        while execution.cursor.batch_sequence is not None:
            _, call = self._pending(execution)
            message = (
                "execution was interrupted; external effects are unknown; not replayed"
                if first
                else "not executed: prior batch member was interrupted"
            )
            self._report(execution, call, ToolFailure(call.tool_name, message), interrupted=True)
            first = False

    def _messages(self, execution: _Execution, *, allow_pending: bool = False) -> tuple[ProviderMessage, ...]:
        messages: list[ProviderMessage] = []
        pending_calls: tuple[ToolCall, ...] = ()
        pending_index = 0
        batch_sequence: int | None = None
        for event in execution.events:
            if event.event_type == "kernel.user_message":
                if pending_calls:
                    raise ValueError("user message crossed an unfinished native batch")
                messages.append(ProviderMessage("user", str(event.payload["content"])))
            elif event.event_type == "kernel.continuation_started":
                if pending_calls:
                    raise ValueError("continuation crossed an unfinished native batch")
                p_payload = event.payload.get("payload")
                p_map = p_payload if isinstance(p_payload, Mapping) else {}
                payload_str = json.dumps(json_wire_object(p_map), ensure_ascii=False)
                messages.append(ProviderMessage("system", f"[Durable continuation from {event.source}]\n{payload_str}"))
            elif event.event_type == "kernel.model_turn":
                if pending_calls:
                    raise ValueError("provider turn crossed an unfinished native batch")
                turn = decode_provider_result(_wire_object(event.payload["result"], "model turn"))
                messages.append(
                    ProviderMessage("assistant", turn.output, reasoning=turn.reasoning, tool_calls=turn.tool_calls, metadata=turn.metadata or {})
                )
                if turn.tool_calls:
                    pending_calls = turn.tool_calls
                    pending_index = 0
                    batch_sequence = event.sequence
            elif event.event_type in {"kernel.tool_completed", "kernel.tool_interrupted"}:
                if not pending_calls or event.payload["batch_sequence"] != batch_sequence:
                    raise ValueError("reported tool result crossed an unfinished native batch")
                call = pending_calls[pending_index]
                if _wire_object(event.payload["call"], "tool call") != json_wire_object(encode_tool_call(call)):
                    raise ValueError("reported tool result does not match the active batch call")
                result = decode_tool_result(_wire_object(event.payload["result"], "tool result"))
                output = result.output
                text = output.text if isinstance(output, TextOutput) else output.presentation
                if result.body is not None:
                    body = json.dumps(result.body.as_payload(), ensure_ascii=False, allow_nan=False)
                    text = body if text is None else f"{body}\n{text}"
                if isinstance(result, ToolFailure):
                    text = result.error if text is None else f"{result.error}\n{text}"
                if text is not None and len(text.encode("utf-8")) > 50 * 1024:
                    text = text.encode("utf-8")[: 50 * 1024].decode("utf-8", errors="ignore")
                    text += "\n[model projection truncated; durable result unchanged]"
                messages.append(
                    ProviderMessage("tool", text, tool_call_id=call.tool_call_id, tool_name=call.tool_name, is_error=isinstance(result, ToolFailure))
                )
                pending_index += 1
                if pending_index == len(pending_calls):
                    pending_calls = ()
        if pending_calls:
            if not allow_pending:
                raise ValueError("cannot call the provider before completing the pending native batch")
            if execution.cursor.batch_sequence != batch_sequence or execution.cursor.next_call != pending_index:
                raise ValueError("checkpoint is not the authentic completed batch prefix")
        elif execution.cursor.batch_sequence is not None or execution.cursor.next_call != 0:
            raise ValueError("checkpoint references an already completed batch")

        # Contributed context from plugins only when not allow_pending. A context
        # failure is never suppressed or re-wrapped here: the drive boundary
        # finishes the run failed before any provider charge.
        if not allow_pending:
            contributions = self.host.contribute_context(
                execution.session.session_id,
                run_id=execution.run_id,
                logical_turn=execution.cursor.logical_turn,
            )
            for c in contributions:
                if c.get("type") == "text":
                    owner = str(c.get("owner", "plugin"))
                    content = str(c.get("content", ""))
                    messages.append(ProviderMessage("system", f"[{owner} context]\n{content}"))

            # Hard bound check strictly AFTER contributions
            if sum(len(json.dumps(encode_message(message), allow_nan=False).encode("utf-8")) for message in messages) > 256 * 1024:
                raise ValueError("context exceeded the kernel's hard bound; explicit compaction is required")

        return tuple(messages)

    def _stop(self, execution: _Execution, message: str, *, stop_cause: StopCause = "user_stop") -> KernelResult:
        """Stop a run. A deliberate user/owner stop is terminal for the session (no replay of the
        abandoned native batch); crashes, shutdown, and host loss keep their recoverable checkpoint."""
        execution.cursor.stop_cause = stop_cause
        deliberate = stop_cause in ("user_stop", "owner_cancel")
        checkpoint_kind = "terminal" if deliberate else "interrupted"
        stop_event = EventDraft(
            "kernel.run_interrupted",
            "kernel",
            "kernel",
            {
                "reason": message,
                "stop_cause": stop_cause,
                "logical_turn": execution.cursor.logical_turn,
                **self._measured_run_elapsed_fields(execution),
            },
        )
        if deliberate and execution.cursor.continuation_id is not None:
            self._commit(
                execution,
                "interrupted",
                checkpoint_kind,
                stop_event,
                completed_continuation_id=execution.cursor.continuation_id,
                completed_continuation_status="cancelled",
            )
            return self._result(execution, error=message, stop_cause=stop_cause)
        self._commit(execution, "interrupted", checkpoint_kind, stop_event)
        return self._result(execution, error=message, stop_cause=stop_cause)

    def _fail(self, execution: _Execution, message: str) -> KernelResult:
        execution.cursor.stop_cause = "error"
        self._commit(
            execution,
            "failed",
            "terminal",
            EventDraft(
                "kernel.run_failed",
                "kernel",
                "kernel",
                {"error": message, "logical_turn": execution.cursor.logical_turn, **self._measured_run_elapsed_fields(execution)},
            ),
            completed_continuation_id=execution.cursor.continuation_id,
            completed_continuation_status="failed",
        )
        return self._result(execution, error=message, stop_cause="error")

    def _result(
        self,
        execution: _Execution,
        *,
        output: str | None = None,
        error: str | None = None,
        pending_approval: Mapping[str, object] | None = None,
        stop_cause: StopCause | None = None,
    ) -> KernelResult:
        return KernelResult(
            execution.session.session_id,
            execution.run_id,
            self.revision_id,
            execution.session.status,
            output=output,
            error=error,
            pending_approval=pending_approval,
            stop_cause=stop_cause,
        )
