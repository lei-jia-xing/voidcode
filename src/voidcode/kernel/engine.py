"""Governed turns over immutable revisions and durable native tool batches."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock
from types import MappingProxyType
from typing import Any, Literal, cast, get_args
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
    CommandCatalog,
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
from ..sdk.tools import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult, ToolSuccess, has_external_mutations
from ..sdk.views import PluginViewDescriptor, ViewsSnapshot
from .activation import (
    Admission,
    StateCopyPlan,
    config_from_snapshot,
    load_private_snapshot,
    plan_state_copies,
    restore_admission,
    sdk_source_hash,
    write_private_snapshot,
)
from .host import HostClient, HostTimeout, HostUnavailable, InvocationCancelled, ToolInvocationOutcome
from .storage.store import (
    Checkpoint,
    EventDraft,
    ObserverReceiptRecord,
    SessionRecord,
    SqliteStore,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedConfirmation,
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


_PARKED_INTERVENING_TYPES = frozenset(
    {
        "kernel.command_completed",
        "kernel.command_intent",
        "kernel.confirmation_requested",
        "kernel.confirmation_decided",
        "kernel.observer_error",
        "kernel.activation_requested",
        "kernel.activation_committed",
        "kernel.activation_failed",
        "kernel.activation_blocked",
    }
)


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

#: Tools the kernel itself dispatches. A plugin can never shadow these names.
_CORE_TOOL_SPECS: tuple[tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...]], ...] = (
    (
        "kernel_reload",
        "Request that the configured plugins be re-admitted as a new capability revision. This only stages the "
        "request: the switch happens after the current tool batch finishes, and it needs user approval first.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string", "maxLength": 500}},
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE,),
    ),
)
CORE_TOOL_NAMES = frozenset(name for name, _d, _s, _e in _CORE_TOOL_SPECS)

#: Reserved core command that activates the configured sources on explicit request.
RELOAD_COMMAND_NAME = "kernel.reload"
_RELOAD_COMMAND_SPEC: tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], bool] = (
    RELOAD_COMMAND_NAME,
    "Re-admit the configured plugin sources and switch this workspace to the new capability revision.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
    True,
)


def _core_command_descriptors(*, with_reload: bool) -> tuple[CommandDescriptor, ...]:
    """Reserved core command descriptors, captured together with the revision that admits them."""
    specs = (*_CORE_COMMAND_SPECS, _RELOAD_COMMAND_SPEC) if with_reload else _CORE_COMMAND_SPECS
    return tuple(
        CommandDescriptor(
            name=name,
            owner="core",
            description=description,
            argument_schema=dict(schema),
            effects=effects,
            busy_gate=busy_gate,
            client_capabilities=(),
        )
        for name, description, schema, effects, busy_gate in specs
    )


def _core_tool_definitions() -> dict[str, ToolDefinition]:
    return {
        name: ToolDefinition(name=name, description=description, input_schema=dict(schema), effects=frozenset(effects))
        for name, description, schema, effects in _CORE_TOOL_SPECS
    }


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """One immutable capability generation. Every dispatch reads this single pointer."""

    revision_id: str
    sdk_sha256: str
    config: KernelConfig
    admission: Admission | None
    host: HostClient
    tools: tuple[ToolDefinition, ...]
    definitions: Mapping[str, ToolDefinition]
    validators: Mapping[str, Any]
    commands: tuple[CommandDescriptor, ...]
    native_tools: Mapping[str, ToolDefinition]

    @staticmethod
    def build(
        *,
        revision_id: str,
        sdk_sha256: str,
        config: KernelConfig,
        admission: Admission | None,
        host: HostClient,
        with_reload: bool,
    ) -> _Snapshot:
        native = MappingProxyType(_core_tool_definitions()) if with_reload else MappingProxyType({})
        host_tools = tuple(host.tools)
        for tool in host_tools:
            if tool.name in native:
                raise ValueError(f"plugin tool {tool.name!r} collides with a reserved kernel tool")
        definitions: dict[str, ToolDefinition] = {tool.name: tool for tool in host_tools}
        definitions.update(native)
        return _Snapshot(
            revision_id=revision_id,
            sdk_sha256=sdk_sha256,
            config=config,
            admission=admission,
            host=host,
            tools=host_tools + tuple(native.values()),
            definitions=MappingProxyType(definitions),
            validators=MappingProxyType(
                {name: jsonschema.Draft202012Validator(json_wire_object(definition.input_schema)) for name, definition in definitions.items()}
            ),
            commands=_core_command_descriptors(with_reload=with_reload) + host.commands,
            native_tools=native,
        )

    def owner_of(self, tool_name: str) -> str:
        return "kernel" if tool_name in self.native_tools else self.host.owner(tool_name)

    def capabilities_of(self, tool_name: str) -> frozenset[str]:
        return frozenset() if tool_name in self.native_tools else self.host.capabilities(tool_name)


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
    #: Activation request that owes this session one automatic continuation after a switch.
    resume_intent: str = ""


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


@dataclass(frozen=True, slots=True)
class _PreparedActivation:
    """A validated candidate generation, its state copies, and the generation it was prepared against."""

    admission: Admission
    host: HostClient
    copies: StateCopyPlan
    source: str
    prepared_against: str


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
        admission: Admission | None = None,
        admits: Callable[[frozenset[str]], Admission] | None = None,
    ) -> None:
        self.auto_drain = auto_drain
        self.workspace = workspace.resolve(strict=True)
        self._abort = Cancellation()
        self._closed = False
        # ponytail: serialize workspace runs; per-session scheduling when concurrent runs are needed.
        self._run_lock = Lock()
        self._session_aborts: dict[str, Cancellation] = {}
        self._aborts_lock = Lock()
        # One pointer per capability generation, plus short-critical-section call accounting.
        # The activation fence rejects new dispatches and reports foreign in-flight calls as
        # busy instead of blocking any run, and no lock is ever held across an RPC.
        self._calls_lock = threading.Condition()
        self._active_calls: dict[str, str] = {}
        self._activating_thread: int | None = None
        # ponytail: bounded in-process cache of per-revision observed_facts; a large
        # multi-revision workspace could grow it, then key it by revision only.
        self._observer_eligibility: dict[tuple[str, str], tuple[str, ...]] = {}
        self._state_dir = state_dir
        self._admits = admits
        self._authorized_paths = frozenset() if admission is None else admission.authorized_paths
        with ExitStack() as resources:
            # Lock the directory inode, independent of the selected database path.
            # Opening/locking it does not create or rewrite workspace files.
            lease = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY)
            resources.callback(os.close, lease)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another kernel owns this workspace") from exc
            self._lease_fd = lease
            state_dir.mkdir(parents=True, exist_ok=True)
            self.store = SqliteStore(state_dir / "kernel-v1.sqlite3")
            resources.callback(self.store.close)
            revision_id = self.store.put_revision(revision_payload)
            if admission is not None and admission.revision_id != revision_id:
                raise ValueError("admission does not describe the revision payload it was opened with")
            host = HostClient(
                plugins,
                workspace=self.workspace,
                revision_id=revision_id,
                store=self.store,
                usage_callback=self._get_session_usage,
                budget_callback=self._get_owner_budget,
                lease_fd=lease,
            )
            # A host whose registry cannot be published is closed here: nothing may outlive a
            # failed construction, and the late-bound cleanup callback is registered only after
            # the generation that owns this host is complete.
            try:
                self._snapshot = _Snapshot.build(
                    revision_id=revision_id,
                    sdk_sha256=str(revision_payload.get("sdk_sha256", "")),
                    config=config,
                    admission=admission,
                    host=host,
                    with_reload=admits is not None,
                )
            except BaseException:
                host.close()
                raise
            # Late-bound so the callback closes whatever generation is current at shutdown.
            resources.callback(self._close_host)
            # Initialization work still owns the resources: a failure here unwinds the stack
            # (host, store, workspace lease) instead of leaking an already-detached kernel.
            self._replay_pending_observers()
            self._driver: threading.Thread | None = None
            if self.auto_drain:
                # A crash between an activation commit and its automatic continuation leaves a
                # durable resume intent; this driver is what actually finishes that work.
                self._driver = threading.Thread(target=self._drive_pending_activations, name="KernelActivationDriver", daemon=True)
                self._driver.start()
            # Adopt the generation LAST: a failed cold start must leave the workspace unadopted, so
            # fixing the same authorized source and retrying works without deleting any database.
            self.store.record_workspace_revision(str(self.workspace), revision_id)
            self._resources = resources.pop_all()

    def _close_host(self) -> None:
        self._snapshot.host.close()

    @property
    def host(self) -> HostClient:
        return self._snapshot.host

    @property
    def config(self) -> KernelConfig:
        return self._snapshot.config

    @property
    def admission(self) -> Admission | None:
        return self._snapshot.admission

    @property
    def revision_id(self) -> str:
        return self._snapshot.revision_id

    @property
    def _definitions(self) -> Mapping[str, ToolDefinition]:
        return self._snapshot.definitions

    @property
    def _validators(self) -> Mapping[str, Any]:
        return self._snapshot.validators

    @contextmanager
    def _dispatch(self, description: str) -> Iterator[_Snapshot]:
        """Capture the current generation and hold a call slot until the caller's result commits.

        The slot is what makes an activation safe: a switch is refused while any foreign
        dispatch is in flight, and refused-for-new once the fence is set, so no result can be
        committed under a generation other than the one that produced it.
        """
        call_id = uuid4().hex
        with self._calls_lock:
            if self._activating_thread is not None and self._activating_thread != threading.get_ident():
                raise HostUnavailable("capability activation is in progress; retry")
            snapshot = self._snapshot
            self._active_calls[call_id] = description
        try:
            yield snapshot
        finally:
            with self._calls_lock:
                self._active_calls.pop(call_id, None)
                self._calls_lock.notify_all()

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
        driver: threading.Thread | None = None
        with self._run_lock:
            if not self._closed:
                self._closed = True
                driver = self._driver
                self._resources.close()
        if driver is not None:
            driver.join(timeout=5)

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
                    session = self._create_session_row(session_id)
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

    def command_catalog(self) -> CommandCatalog:
        """Command catalog captured together with the revision that admits it.

        Descriptor list and revision come from ONE immutable snapshot, so a client can
        bind a later action to exactly the generation it displayed.
        """
        self._ensure_open()
        snapshot = self._snapshot
        return CommandCatalog(revision_id=snapshot.revision_id, commands=snapshot.commands)

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
        return self._create_session_row(sid)

    def _create_session_row(self, session_id: str) -> SessionRecord:
        """Create a session on the workspace's recorded revision, retrying if it moves first."""
        for _attempt in range(3):
            recorded = self.store.workspace_current(str(self.workspace)) or self.revision_id
            try:
                return self.store.create_session(session_id, str(self.workspace), recorded)
            except StoreConflict:
                continue
        raise StoreConflict(f"could not create session {session_id!r}: the workspace revision kept moving")

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
        expected_revision: str,
        client_capabilities: frozenset[str] = frozenset(),
    ) -> CommandResult:
        """Execute a command, handling core commands, policy checks, confirmation, and CAS commit.

        `expected_revision` is required and must be the revision the client displayed: a
        command advertised by one generation is never silently executed by another.
        """
        self._ensure_open()
        self._check_expected_revision(expected_revision)
        s = self.session(session_id)

        # 1. Dispatch core reserved commands through the same typed contract the
        # catalog advertises: the session id is outer authority, never an argument.
        if name == RELOAD_COMMAND_NAME:
            return self._invoke_reload_command(session_id, arguments)
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
            return CommandResult(status="ok", data=dict(self.command_catalog().as_payload()))

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
        with self._dispatch(f"command:{name}"):
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
                latest = self.store.session(session_id)
                self.store.commit_staged(
                    session_id,
                    expected_sequence=latest.last_event_sequence,
                    expected_run_id=latest.run_id,
                    run_id=latest.run_id or run_id,
                    revision_id=self.revision_id,
                    events=(
                        EventDraft(
                            "kernel.confirmation_requested",
                            f"plugin.{cmd_desc.owner}",
                            "client",
                            req.as_payload(),
                        ),
                    ),
                    status=latest.status,
                    checkpoint_kind=None,
                    checkpoint_payload=None,
                    staged_confirmations=(
                        StagedConfirmation(
                            request_id=req.request_id,
                            owner=cmd_desc.owner,
                            command_name=name,
                            arguments=req.arguments,
                            ui_type=req.ui_type,
                            ui_data=req.ui_data,
                            expected_state_version=req.expected_state_version,
                        ),
                    ),
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
        expected_revision: str,
        client_capabilities: frozenset[str] = frozenset(),
    ) -> CommandResult:
        """Resolve a pending confirmation request by exact ID and state version."""
        self._ensure_open()
        self._check_expected_revision(expected_revision)
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
                self._commit_confirmation_decision(session_id, request_id, rec.command_name, "stale")
                raise ValueError(f"Confirmation request is stale: expected state version {rec.expected_state_version}, got {curr_ver}")

        if decision == "deny":
            self.store.decide_confirmation(request_id, "denied")
            self._commit_confirmation_decision(session_id, request_id, rec.command_name, "deny")
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

        with self._dispatch(f"command:{rec.command_name}"):
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
                all_events = tuple(staged_events) + (
                    EventDraft(
                        "kernel.confirmation_decided",
                        f"plugin.{cmd_desc.owner}",
                        "user",
                        {"request_id": request_id, "command_name": rec.command_name, "decision": "allow"},
                    ),
                    completion_event,
                )
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

    def _commit_confirmation_decision(self, session_id: str, request_id: str, command_name: str, decision: str) -> None:
        """Record one decided confirmation durably, so clients can reduce a pending request."""
        session = self.store.session(session_id)
        try:
            self.store.commit_staged(
                session_id,
                expected_sequence=session.last_event_sequence,
                expected_run_id=session.run_id,
                run_id=session.run_id or request_id,
                revision_id=self.revision_id,
                events=(
                    EventDraft(
                        "kernel.confirmation_decided",
                        "kernel",
                        "user",
                        {"request_id": request_id, "command_name": command_name, "decision": decision},
                    ),
                ),
                status=session.status,
                checkpoint_kind=None,
                checkpoint_payload=None,
            )
        except StoreConflict:
            # The decision itself is durable; a concurrent writer only costs the audit fact.
            return

    def plugin_views(
        self,
        session_id: str,
        *,
        client_capabilities: frozenset[str] = frozenset(),
    ) -> ViewsSnapshot:
        """Render declarative views contributed by admitted plugins for the session.

        The whole set renders under ONE captured generation and carries that revision,
        so a late view from an older revision is never relabelled with the new one.
        """
        self._ensure_open()
        session = self.session(session_id)
        with self._dispatch("views") as snapshot:
            views: list[PluginViewDescriptor] = []
            for view_name in snapshot.host.views:
                try:
                    v = snapshot.host.render_view(session_id, view_name, run_id=session.run_id or "", client_capabilities=client_capabilities)
                    views.append(v)
                except Exception as exc:
                    owner = snapshot.host.view_owner(view_name)
                    views.append(
                        PluginViewDescriptor(
                            owner=owner,
                            name=view_name,
                            title=f"{view_name} (error)",
                            view_type="notice",
                            data={"message": f"View {view_name} failed: {type(exc).__name__}", "level": "error"},
                        )
                    )
            return ViewsSnapshot(revision_id=snapshot.revision_id, views=tuple(views))

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

    def _check_expected_revision(self, expected_revision: str) -> None:
        """Bind a client action to the catalog generation it displayed."""
        if not isinstance(expected_revision, str) or not expected_revision.strip():
            raise ValueError("expected_revision must be the revision the client displayed")
        if expected_revision != self.revision_id:
            raise ValueError(f"displayed catalog revision {expected_revision} is stale; refresh the catalog")

    def _invoke_reload_command(self, session_id: str, arguments: Mapping[str, object]) -> CommandResult:
        """Reserved core command: activate the configured sources on explicit user request."""
        validator = jsonschema.Draft202012Validator(json_wire_object(_RELOAD_COMMAND_SPEC[2]))
        try:
            validator.validate(json_wire_object(arguments))
        except jsonschema.ValidationError as err:
            return CommandResult(status="error", error=f"Command arguments failed schema: {err.message}")
        if self._admits is None:
            return CommandResult(status="error", error="capability activation is unavailable in this kernel")
        if self.config.policy.read_only:
            return CommandResult(status="error", error="read-only policy denies capability activation")
        session = self.session(session_id)
        if session.status not in {"idle", "completed", "failed", "interrupted"}:
            return CommandResult(status="error", error=f"session {session_id} is busy ({session.status}); activation requires an idle session")
        outcome = self.activate(session_id=session_id, source="client")
        if not outcome["activated"]:
            return CommandResult(status="error", error=str(outcome["error"]))
        return CommandResult(status="ok", data={key: value for key, value in outcome.items() if key != "error"})

    def activate(self, *, session_id: str, source: str, request_id: str = "", target_revision_id: str = "") -> dict[str, object]:
        """Run one capability activation for an already-safe boundary.

        With `target_revision_id` the activation uses exactly that pinned candidate (the one an
        approval was bound to); otherwise the live configuration is pinned now. Returns a
        JSON-safe outcome and never raises for a refused activation: the refusal is recorded
        durably on the session and the active revision stays untouched.
        """
        self._ensure_open()
        snapshot = self._snapshot
        session = self.session(session_id)
        request_id = request_id or uuid4().hex
        blockers = self.store.activation_blockers(str(self.workspace), session_id)
        if blockers:
            error = f"activation blocked: {'; '.join(blockers)}"
            self._record_activation_refusal(session_id, request_id, source, error, blockers)
            return {"activated": False, "error": error, "request_id": request_id}
        try:
            if target_revision_id:
                admission = self._pinned_target(target_revision_id)
            else:
                target_revision_id, admission = self._prepare_target()
            prepared = self._prepare_activation(snapshot, admission=admission, source=source)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            blocked = self.store.activation_blockers(str(self.workspace), session_id)
            self._record_activation_refusal(session_id, request_id, source, error, blocked)
            return {"activated": False, "error": error, "request_id": request_id}
        try:
            self._commit_activation(snapshot, session, request_id=request_id, source=source, prepared=prepared)
        except Exception as exc:
            self._clear_fence()
            prepared.host.close()
            if not isinstance(exc, StoreConflict):
                raise
            self._record_activation_refusal(session_id, request_id, source, str(exc), ())
            return {"activated": False, "error": str(exc), "request_id": request_id}
        return {
            "activated": True,
            "request_id": request_id,
            "from_revision_id": snapshot.revision_id,
            "to_revision_id": prepared.admission.revision_id,
            "migrated_owners": list(prepared.copies.migrated_owners),
            "deactivated_owners": list(prepared.copies.deactivated_owners),
        }

    def _bound_target(self, execution: _Execution, call: ToolCall) -> str | None:
        """The pinned candidate an approval was bound to, if one exists for this exact native call."""
        encoded = encode_tool_call(call)
        target: str | None = None
        for event in execution.events:
            if event.event_type != "kernel.approval_requested":
                continue
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            bound = payload.get("target_revision_id")
            bound_call = payload.get("call")
            if isinstance(bound, str) and isinstance(bound_call, Mapping) and json_wire_object(bound_call) == encoded:
                target = bound
        return target

    def _pending_activation_request(self, execution: _Execution) -> Mapping[str, object] | None:
        """The newest staged activation request of this run that no answer has consumed yet."""
        request: Mapping[str, object] | None = None
        answered: set[str] = set()
        for event in execution.events:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                continue
            if event.event_type == "kernel.activation_requested":
                request = payload
            elif event.event_type in {"kernel.activation_committed", "kernel.activation_failed", "kernel.activation_blocked"}:
                answered.add(request_id)
        if request is None:
            return None
        return None if str(request.get("request_id", "")) in answered else request

    def _clear_fence(self) -> None:
        with self._calls_lock:
            self._activating_thread = None
            self._calls_lock.notify_all()

    def _record_activation_refusal(
        self,
        session_id: str,
        request_id: str,
        source: str,
        error: str,
        blockers: Sequence[str],
    ) -> None:
        """Record why an activation did not happen, on the acting session, without consuming the request."""
        session = self.store.session(session_id)
        cursor = _Cursor()
        checkpoint = self.store.checkpoint(session_id)
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        execution = _Execution(session, cursor, list(self.store.events(session_id)), session.run_id or request_id)
        kind = "kernel.activation_blocked" if blockers else "kernel.activation_failed"
        try:
            self._commit(
                execution,
                session.status,
                checkpoint.kind if checkpoint is not None else "ready",
                EventDraft(
                    kind,
                    "kernel",
                    "kernel",
                    {
                        "request_id": request_id,
                        "source": source,
                        "error": error,
                        "blockers": list(blockers),
                    },
                ),
            )
        except StoreConflict, ValueError:
            # The refusal is a diagnostic, never a reason to lose the durable request.
            pass

    def _prepare_target(self) -> tuple[str, Admission]:
        """Pin the candidate the live configuration currently describes; imports no user code.

        Called before an approval is requested, so what the user is asked about is exactly the
        immutable artifact and private configuration that a later activation will use.
        """
        admits = self._admits
        if admits is None:
            raise ValueError("capability activation is unavailable in this kernel")
        admission = admits(self._authorized_paths)
        self.store.put_revision(admission.revision_payload)
        write_private_snapshot(self._state_dir, admission.revision_id, admission.private_snapshot)
        return admission.revision_id, admission

    def _pinned_target(self, target_revision_id: str) -> Admission:
        """Rebuild a previously pinned target from durable records and re-verify its bytes."""
        payload = self.store.revision(target_revision_id)
        snapshot = load_private_snapshot(self._state_dir, target_revision_id)
        return restore_admission(payload, snapshot, state_dir=self._state_dir, config=config_from_snapshot(snapshot))

    def _prepare_activation(self, snapshot: _Snapshot, *, admission: Admission, source: str) -> _PreparedActivation:
        """Build, verify and migrate a candidate generation under the activation fence."""
        thread_id = threading.get_ident()
        with self._calls_lock:
            if self._activating_thread is not None:
                raise HostUnavailable("another capability activation is already in progress")
            if self._active_calls:
                busy = ", ".join(sorted(self._active_calls.values()))
                raise HostUnavailable(f"capability activation is blocked by in-flight work: {busy}")
            self._activating_thread = thread_id
        try:
            return self._prepare_activation_locked(snapshot, admission=admission, source=source)
        except Exception:
            self._clear_fence()
            raise

    def _prepare_activation_locked(self, snapshot: _Snapshot, *, admission: Admission, source: str) -> _PreparedActivation:
        # A candidate host must load the same SDK source the active revision pinned; a
        # mutated SDK is a controlled kernel upgrade (P5), never a silent hot reload.
        if not snapshot.sdk_sha256:
            raise ValueError("the active revision does not pin an SDK source hash; refusing to load candidate code")
        installed_sdk = sdk_source_hash(self._sdk_root())
        if installed_sdk != snapshot.sdk_sha256:
            raise ValueError("installed SDK source no longer matches the pinned revision; run a controlled kernel upgrade")
        if admission.revision_payload.get("sdk_sha256") != installed_sdk:
            raise ValueError("the prepared candidate was pinned against a different SDK source; prepare it again")
        target_revision_id = admission.revision_id
        if target_revision_id == snapshot.revision_id:
            raise ValueError("the configured sources already produce the active revision")
        candidate_host: HostClient | None = None
        try:
            candidate_host = HostClient(
                admission.plugins,
                workspace=self.workspace,
                revision_id=target_revision_id,
                store=self.store,
                usage_callback=self._get_session_usage,
                budget_callback=self._get_owner_budget,
                lease_fd=self._lease_fd,
            )
            # Flush facts that belong to the OLD generation before its hosts and subscriptions go away.
            self._replay_pending_observers(require_complete=True)
            session_ids = tuple(record.session_id for record in self.store.sessions(workspace=str(self.workspace)))
            old_manifests = {plugin.manifest.plugin_id: plugin.manifest for plugin in snapshot.host.loaded_plugins}
            copies = plan_state_copies(
                store=self.store,
                workspace=str(self.workspace),
                session_ids=session_ids,
                old_manifests=old_manifests,
                candidate_manifests=admission.manifests,
                migrate=lambda owner, payload, from_version, to_version: self._migrate_state(
                    candidate_host, owner, payload, from_version, to_version
                ),
                deactivate=lambda owner, payload, scope, scope_key, tip: self._deactivate_state(snapshot.host, owner, payload, scope, scope_key, tip),
            )
        except Exception:
            if candidate_host is not None:
                candidate_host.close()
            self._clear_fence()
            raise
        return _PreparedActivation(
            admission=admission,
            host=candidate_host,
            copies=copies,
            source=source,
            prepared_against=snapshot.revision_id,
        )

    def _sdk_root(self) -> Path:
        return Path(__file__).resolve().parents[1] / "sdk"

    def _deactivated_owners(self, snapshot: _Snapshot, admission: Admission) -> frozenset[str]:
        """Owners loaded before that the candidate does not admit: disabled, removed, or missing."""
        loaded = {plugin.manifest.plugin_id for plugin in snapshot.host.loaded_plugins}
        return frozenset(loaded - admission.plugin_ids)

    def _migrate_state(
        self,
        candidate_host: HostClient,
        owner: str,
        payload: Mapping[str, object],
        from_version: int,
        to_version: int,
    ) -> Mapping[str, object]:
        try:
            return candidate_host.migrate_owner_state(owner, payload, from_version=from_version, to_version=to_version)
        except RuntimeError as exc:
            raise ValueError(f"state migration for {owner} failed: {exc}") from exc

    def _deactivate_state(
        self,
        host: HostClient,
        owner: str,
        payload: Mapping[str, object],
        scope: str,
        scope_key: str,
        tip_sequence: int | None,
    ) -> Mapping[str, object]:
        try:
            return host.prepare_deactivate_owner_state(
                owner,
                payload,
                scope=scope,
                scope_key=scope_key,
                tip_sequence=tip_sequence,
            )
        except RuntimeError as exc:
            raise ValueError(f"state deactivation for {owner} failed: {exc}") from exc

    def _commit_activation(
        self,
        snapshot: _Snapshot,
        session: SessionRecord,
        *,
        request_id: str,
        source: str,
        prepared: _PreparedActivation,
    ) -> None:
        """One CAS transaction, then publish one new pointer and revoke the old generation."""
        admission = prepared.admission
        target_revision_id = admission.revision_id
        checkpoint = self.store.checkpoint(session.session_id)
        checkpoint_payload: Mapping[str, object] | None = None
        checkpoint_kind: Literal["ready", "approval_wait", "terminal", "interrupted"] | None = None
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            checkpoint_payload = self._activation_checkpoint(
                json_wire_object(checkpoint.payload),
                source=source,
                request_id=request_id,
            )
            checkpoint_kind = cast("Literal['ready', 'approval_wait', 'terminal', 'interrupted']", checkpoint.kind)
        disabled_owners = self._deactivated_owners(snapshot, admission)
        # The model's request was already recorded durably by its own tool commit; this
        # transaction only answers it (a client-initiated switch answers no staged request).
        events = (
            EventDraft(
                "kernel.activation_committed",
                "kernel",
                "kernel",
                {
                    "request_id": request_id,
                    "source": source,
                    "from_revision_id": snapshot.revision_id,
                    "to_revision_id": target_revision_id,
                    "migrated_owners": list(prepared.copies.migrated_owners),
                    "deactivated_owners": list(prepared.copies.deactivated_owners),
                },
            ),
        )
        self.store.commit_activation(
            workspace=str(self.workspace),
            session_id=session.session_id,
            expected_sequence=session.last_event_sequence,
            expected_run_id=session.run_id,
            expected_revision_id=snapshot.revision_id,
            target_revision_id=target_revision_id,
            target_payload=admission.revision_payload,
            # Control facts carry the authentic operation identity, never an invented run.
            run_id=session.run_id or request_id,
            status=session.status,
            events=events,
            checkpoint_kind=checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
            state_updates=prepared.copies.updates,
            cancelled_owners=tuple(sorted(disabled_owners)),
            cleared_budget_owners=tuple(sorted(disabled_owners)),
        )
        # Publication: one pointer swap, then revoke the old generation. Nothing can be
        # dispatched to the old host afterwards (it is closed and never respawns), and the
        # candidate host was already validated and migrated before the commit.
        with self._calls_lock:
            self._snapshot = _Snapshot.build(
                revision_id=target_revision_id,
                sdk_sha256=snapshot.sdk_sha256,
                config=admission.config,
                admission=admission,
                host=prepared.host,
                with_reload=self._admits is not None,
            )
            self._authorized_paths = admission.authorized_paths
            self._activating_thread = None
            self._calls_lock.notify_all()
        snapshot.host.close()

    def _activation_checkpoint(
        self,
        checkpoint_payload: Mapping[str, object] | None,
        *,
        source: str,
        request_id: str,
    ) -> Mapping[str, object]:
        """Carry the session's own cursor across the switch, plus a real resume intent where owed.

        The intent is written only for a model-requested switch inside a live run: that run owes
        one automatic continuation, and a crash before the in-process continuation persists it
        lets a restart finish the job exactly once. A user-issued switch owes no such turn.
        """
        cursor = _Cursor()
        if checkpoint_payload is not None:
            cursor = _Cursor.model_validate(json_wire_object(checkpoint_payload))
        if source == "model":
            cursor.resume_intent = request_id
        return json_wire_object(cursor.model_dump())

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

    def _replay_pending_observers(self, *, require_complete: bool = False) -> None:
        """Deliver facts whose observer receipts never committed before accepting new work.

        Eligibility comes from each source event's OWN revision (its manifest declarations),
        not from whoever is subscribed now, so a reload can neither re-deliver an old fact to
        a new subscriber nor drop one an eligible owner already received. With
        `require_complete`, an eligible owner that is no longer registered aborts instead of
        being papered over: an activation must never mask an undelivered fact.
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
                self._deliver_fact_for_event(execution, ev, require_complete=require_complete)

    def _eligible_observers(self, event: StoredEvent, fact_type: str) -> tuple[str, ...]:
        """Owners that registered this fact type in the revision that produced the event.

        For the generation that is current right now this is its live registration; for an
        older event it comes from that revision's stored manifest declarations, so a reload
        can never hand an old fact to a subscriber that did not exist back then.
        """
        if event.revision_id == self._snapshot.revision_id:
            return self._snapshot.host._observer_owners.get(fact_type, ())
        cache_key = (event.revision_id, fact_type)
        cached = self._observer_eligibility.get(cache_key)
        if cached is not None:
            return cached
        payload = self.store.revision(event.revision_id)
        plugins = payload.get("plugins")
        eligible: list[str] = []
        if isinstance(plugins, Mapping):
            for owner, identity in plugins.items():
                if not isinstance(owner, str) or not isinstance(identity, Mapping):
                    continue
                manifest = identity.get("manifest")
                if not isinstance(manifest, Mapping):
                    continue
                facts = manifest.get("observed_facts")
                if isinstance(facts, list) and fact_type in facts:
                    eligible.append(owner)
        result = tuple(sorted(eligible))
        self._observer_eligibility[cache_key] = result
        return result

    def _observer_gap_event(self, event: StoredEvent, missing: Sequence[str]) -> EventDraft:
        """A durable diagnostic for a fact that its own revision's owner can no longer receive."""
        return EventDraft(
            "kernel.observer_error",
            source="kernel",
            actor="kernel",
            payload={
                "owner": ", ".join(missing),
                "error": f"event {event.sequence} was produced by revision {event.revision_id} but its registered owner is not loaded",
                "event_sequence": event.sequence,
            },
        )

    def _deliver_fact_for_event(self, execution: _Execution, ev: StoredEvent, *, require_complete: bool = False) -> None:
        """Notify exactly the owners this event's revision registered, then persist those receipts."""
        fact = self._fact_for(execution, ev)
        if fact is None:
            return
        fact_type, fact_payload = fact
        session_id = ev.session_id
        with self._dispatch(f"observer:{fact_type}") as snapshot:
            eligible = self._eligible_observers(ev, fact_type)
            registered = snapshot.host._observer_owners.get(fact_type, ())
            missing = [owner for owner in eligible if owner not in registered]
            if missing:
                if require_complete:
                    raise ValueError(f"cannot activate: event {ev.sequence} of session {session_id} is still undelivered for {', '.join(missing)}")
                self._commit_staged_rebased(
                    execution,
                    status=execution.session.status,
                    kind=None,
                    events=(self._observer_gap_event(ev, missing),),
                )
            active = [o for o in eligible if o in registered and not self.store.has_observer_receipt(session_id, ev.sequence, o)]
            if not active:
                return
            logical_turn = str(fact_payload.get("logical_turn", ""))
            obs_res = snapshot.host.notify_fact(fact_type, fact_payload, owners=tuple(active), logical_turn=logical_turn)
            receipts = tuple(ObserverReceiptRecord(event_sequence=ev.sequence, owner=o) for o in active)
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
            eligible = self._eligible_observers(ev, fact_type)
            registered = self.host._observer_owners.get(fact_type, ())
            missing = [owner for owner in eligible if owner not in registered]
            if missing:
                self._commit_staged_rebased(
                    execution,
                    status=execution.session.status,
                    kind=None,
                    events=(self._observer_gap_event(ev, missing),),
                )
            active_owners = [o for o in eligible if o in registered and not self.store.has_observer_receipt(ev.session_id, ev.sequence, o)]
            if not active_owners:
                continue
            with self._dispatch(f"observer:{fact_type}") as snapshot:
                obs_res = snapshot.host.notify_fact(
                    fact_type,
                    fact_payload,
                    owners=tuple(active_owners),
                    logical_turn=str(fact_payload.get("logical_turn", "")),
                )
                receipts = tuple(ObserverReceiptRecord(event_sequence=ev.sequence, owner=o) for o in active_owners)
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
            snapshot = self._snapshot
            cursor = execution.cursor
            if cursor.batch_sequence is None:
                # A staged activation applies only here: the whole previous native batch has been
                # reported and committed, so no result can mix generations, and the request is
                # consumed durably in the same transaction as the switch.
                pending_request = self._pending_activation_request(execution)
                if pending_request is not None:
                    outcome = self.activate(
                        session_id=execution.session.session_id,
                        source="model",
                        request_id=str(pending_request.get("request_id", "")),
                        target_revision_id=str(pending_request.get("target_revision_id", "")),
                    )
                    if outcome["activated"]:
                        # This run is the automatic continuation the activation implied; the
                        # next commit persists the consumed intent.
                        execution.cursor.resume_intent = ""
                    execution.session = self.store.session(execution.session.session_id)
                    execution.events = list(self.store.events(execution.session.session_id))
                    cursor = execution.cursor
                    snapshot = self._snapshot
                if cursor.step >= max_steps:
                    return self._stop(execution, "provider turn budget exhausted", stop_cause="limit_reached")

                budget_stop = self._budget_admission(execution)
                if budget_stop is not None:
                    return budget_stop

                with self._dispatch("provider") as snapshot:
                    model = snapshot.config.model.model
                    if model is None:
                        return self._fail(execution, "no model configured")
                    provider_name, separator, model_name = model.partition("/")
                    entry = snapshot.config.providers.get(provider_name)
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
                        available_tools=snapshot.tools,
                        raw_model=model,
                        provider_name=provider_name,
                        model_name=model_name,
                        reasoning_effort=snapshot.config.model.reasoning_effort,
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
                        turn = snapshot.host.propose_turn(request, provider=entry.wire)
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
                    "owner": snapshot.owner_of(call.tool_name),
                }
                if call.tool_name in snapshot.native_tools:
                    # Pin the exact candidate this approval is about, before the user is asked:
                    # admission imports nothing, and after approval the already-pinned artifact
                    # is activated, so editing the workspace source in between cannot change it.
                    target_revision_id, _admission = self._prepare_target()
                    payload["target_revision_id"] = target_revision_id
                    payload["activation_pending"] = True
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
        with self._dispatch(f"tool:{call.tool_name}") as snapshot:
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
                            "owner": snapshot.owner_of(call.tool_name),
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

            if call.tool_name in self._snapshot.native_tools:
                # A kernel-owned tool only stages a durable, approved request; the switch itself
                # happens at the next batch boundary, never inside this callback's stack.
                request_id = uuid4().hex
                target = str(self._bound_target(execution, call) or "")
                result: ToolResult = ToolSuccess(
                    call.tool_name,
                    TextOutput("activation request recorded; it is applied after this tool batch finishes"),
                )
                self._report(
                    execution,
                    call,
                    result,
                    staged_events=(
                        EventDraft(
                            "kernel.activation_requested",
                            "kernel",
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
            context = ToolContext(
                workspace=self.workspace,
                session_id=execution.session.session_id,
                run_id=execution.run_id,
                invocation_id=invocation_id,
                revision_id=snapshot.revision_id,
                actor="model",
                capabilities=snapshot.capabilities_of(call.tool_name),
                logical_turn=execution.cursor.logical_turn,
                abort_signal=effective_abort,
            )
            try:
                outcome = snapshot.host.invoke(call, context=context, return_outcome=True)
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
            owner = self._snapshot.owner_of(call.tool_name)
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
            with self._dispatch("context") as snapshot:
                contributions = snapshot.host.contribute_context(
                    execution.session.session_id,
                    run_id=execution.run_id,
                    logical_turn=execution.cursor.logical_turn,
                )
                for c in contributions:
                    if c.get("type") == "text":
                        owner = str(c.get("owner", "plugin"))
                        content = str(c.get("content", ""))
                        messages.append(ProviderMessage("system", f"[{owner} context]\n{content}"))

            activation_note = self._activation_outcome_note(execution)
            if activation_note is not None:
                messages.append(ProviderMessage("system", activation_note))

            # Hard bound check strictly AFTER contributions
            if sum(len(json.dumps(encode_message(message), allow_nan=False).encode("utf-8")) for message in messages) > 256 * 1024:
                raise ValueError("context exceeded the kernel's hard bound; explicit compaction is required")

        return tuple(messages)

    def _drive_pending_activations(self) -> None:
        """Finish automatic continuations a crash interrupted right after an activation commit."""
        try:
            for session in self.store.sessions(workspace=str(self.workspace)):
                if self._closed:
                    return
                checkpoint = self.store.checkpoint(session.session_id)
                if checkpoint is None or not isinstance(checkpoint.payload, Mapping):
                    continue
                cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
                if not cursor.resume_intent:
                    continue
                try:
                    self._continue_after_activation(session.session_id)
                except Exception:
                    # The intent stays durable, so the next process retries it; never crash the kernel.
                    continue
        except Exception:
            return

    def _continue_after_activation(self, session_id: str) -> None:
        """Claim and drive one owed continuation, exactly once, on its own captured generation."""
        with self._run_lock:
            self._ensure_open()
            with self._aborts_lock:
                if session_id in self._session_aborts:
                    return
            checkpoint = self.store.checkpoint(session_id)
            if checkpoint is None or not isinstance(checkpoint.payload, Mapping):
                return
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
            intent = cursor.resume_intent
            if not intent:
                return
            session = self.store.session(session_id)
            if checkpoint.revision_id != self.revision_id or session.active_revision_id != self.revision_id:
                return
            self._abort.set_cancelled(False)
            cursor.resume_intent = ""
            execution = _Execution(session, cursor, list(self.store.events(session_id)), session.run_id or intent)
            try:
                self._commit(
                    execution,
                    "running",
                    "ready",
                    EventDraft("kernel.activation_continued", "kernel", "kernel", {"request_id": intent}),
                )
            except StoreConflict:
                return
            budget = 32
            root_state = self.store.root_budget_state(cursor.root_id) if cursor.root_id else None
            if root_state is not None:
                remaining = root_state[0] - root_state[1]
                if remaining <= 0:
                    self._stop(execution, "root action budget exhausted", stop_cause="limit_reached")
                    return
                budget = max(1, min(32, remaining))
            abort = Cancellation()
            with self._aborts_lock:
                self._session_aborts[session_id] = abort
            try:
                self._drive(execution, max_steps=budget)
            finally:
                with self._aborts_lock:
                    self._session_aborts.pop(session_id, None)

    def _activation_outcome_note(self, execution: _Execution) -> str | None:
        """Tell the next prompt what happened to a staged activation this run asked for."""
        request_id: str | None = None
        outcome: tuple[str, str] | None = None
        for event in execution.events:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            candidate = payload.get("request_id")
            if not isinstance(candidate, str) or not candidate:
                continue
            if event.run_id != execution.run_id:
                # Only the run that asked for the switch is told about it; later turns are not
                # re-fed an old decision as if it were news.
                continue
            if event.event_type == "kernel.activation_requested":
                request_id = candidate
                outcome = None
            elif event.event_type == "kernel.activation_committed" and candidate == request_id:
                outcome = ("committed", str(payload.get("to_revision_id", "")))
            elif outcome is None and candidate == request_id and event.event_type in {"kernel.activation_failed", "kernel.activation_blocked"}:
                outcome = ("refused", str(payload.get("error", "")))
        if request_id is None or outcome is None:
            return None
        kind, detail = outcome
        if kind == "committed":
            return f"[kernel] capability activation {request_id} committed the revision {detail}."
        return f"[kernel] capability activation {request_id} was refused and the previous revision is still active: {detail}"

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
