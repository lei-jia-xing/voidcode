"""Governed turns over immutable revisions and durable native tool batches."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Literal
from uuid import uuid4

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from ..sdk.codec import (
    decode_budget,
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
from ..sdk.invocation import ToolContext
from ..sdk.json_values import json_wire_object
from ..sdk.plugin import HostPlugin
from ..sdk.provider import ProviderMessage, ProviderTurnRequest
from ..sdk.services import (
    ContinuationResult,
    PluginBudget,
    SessionUsageReport,
)
from ..sdk.tools import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult
from ..sdk.views import PluginViewDescriptor
from .host import HostClient, HostTimeout, HostUnavailable, InvocationCancelled
from .storage.store import (
    EventDraft,
    SessionRecord,
    SqliteStore,
    StagedContinuationDraft,
    StagedStateUpdate,
    StoredEvent,
)


class Cancellation:
    def __init__(self) -> None:
        self._event = Event()
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._reason = reason if value else None
        if value:
            self._event.set()
        else:
            self._event.clear()


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


@dataclass(frozen=True, slots=True)
class KernelResult:
    session_id: str
    run_id: str
    revision_id: str
    status: str
    output: str | None = None
    error: str | None = None
    pending_approval: Mapping[str, object] | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "pending_approval": None if self.pending_approval is None else json_wire_object(self.pending_approval),
        }


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
    ) -> None:
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

    def __enter__(self) -> Kernel:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def cancel_session(self, session_id: str, reason: str = "user cancellation") -> bool:
        """Signal cancellation only to the currently executing run of a specific session."""
        self._ensure_open()
        with self._aborts_lock:
            abort = self._session_aborts.get(session_id)
        if abort is not None and not abort.cancelled:
            abort.set_cancelled(True, reason=reason)
            return True
        return False

    def cancel(self, reason: str = "user cancellation") -> None:
        self._abort.set_cancelled(True, reason=reason)
        with self._aborts_lock:
            for ab in self._session_aborts.values():
                ab.set_cancelled(True, reason=reason)

    def close(self) -> None:
        self.cancel("kernel shutdown")
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
            try:
                session = self.store.session(session_id)
            except KeyError:
                session = self.store.create_session(session_id, str(self.workspace), self.revision_id)
            if session.status not in {"idle", "completed", "failed"}:
                raise ValueError("unfinished session requires explicit resume")
            self._check_session(session)
            execution = _Execution(session, _Cursor(), list(self.store.events(session_id)), uuid4().hex)
            self._commit(execution, "running", "ready", EventDraft("kernel.user_message", "client", "user", {"content": prompt}), start_turn=True)
            return self._drive(execution, max_steps=max_steps)

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
                if ev.event_type not in {"kernel.command_completed", "kernel.observer_error"} and not ev.event_type.startswith("plugin."):
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
            return self._drive(execution, max_steps=max_steps)

    def command_catalog(self) -> tuple[CommandDescriptor, ...]:
        """Catalog of admitted plugin commands and reserved core commands."""
        self._ensure_open()
        core_commands = (
            CommandDescriptor(
                name="run",
                owner="core",
                description="Run an agent turn with a user prompt",
                argument_schema={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
                effects=(ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
                busy_gate=True,
                client_capabilities=(),
            ),
            CommandDescriptor(
                name="resume",
                owner="core",
                description="Resume a paused session (approval or interrupted)",
                argument_schema={"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
                effects=(ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
                busy_gate=True,
                client_capabilities=(),
            ),
            CommandDescriptor(
                name="show",
                owner="core",
                description="Show session state, checkpoint, and events",
                argument_schema={"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
                effects=(ToolEffect.READ,),
                busy_gate=False,
                client_capabilities=(),
            ),
            CommandDescriptor(
                name="status",
                owner="core",
                description="Show kernel and workspace status",
                argument_schema={"type": "object"},
                effects=(ToolEffect.READ,),
                busy_gate=False,
                client_capabilities=(),
            ),
            CommandDescriptor(
                name="help",
                owner="core",
                description="Show available commands and documentation",
                argument_schema={"type": "object"},
                effects=(ToolEffect.READ,),
                busy_gate=False,
                client_capabilities=(),
            ),
        )
        return core_commands + self.host.commands

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
        intent_times: dict[int, int] = {}
        completions_by_dispatch: dict[int, StoredEvent] = {}

        for ev in all_events:
            if ev.event_type == "kernel.provider_intent":
                seq = ev.sequence
                start_ms = ev.payload.get("start_time_ms")
                intent_times[seq] = int(start_ms) if isinstance(start_ms, (int, float)) else 0
                if seq > since_sequence and (until_sequence is None or seq <= until_sequence):
                    dispatches_in_window.append(seq)
            elif ev.event_type in ("kernel.model_turn", "kernel.provider_interrupted"):
                disp_seq = ev.payload.get("dispatch_sequence")
                disp_seq_val = int(disp_seq) if isinstance(disp_seq, (int, float)) else ev.sequence - 1
                completions_by_dispatch[disp_seq_val] = ev

        input_tokens: int | None = None
        output_tokens: int | None = None
        cache_read_tokens: int | None = None
        uncached_input_tokens: int | None = None
        has_unknown = False
        turn_count = 0
        total_elapsed_ms: int | None = None

        for disp_seq in dispatches_in_window:
            comp_ev = completions_by_dispatch.get(disp_seq)
            if comp_ev is None:
                has_unknown = True
            elif comp_ev.event_type == "kernel.provider_interrupted":
                has_unknown = True
            elif comp_ev.event_type == "kernel.model_turn":
                turn_count += 1
                turn = decode_provider_result(json_wire_object(comp_ev.payload))
                u = turn.usage
                if u is not None and u.input_tokens is not None and u.output_tokens is not None:
                    input_tokens = (input_tokens or 0) + u.input_tokens
                    output_tokens = (output_tokens or 0) + u.output_tokens
                    if u.cache_read_tokens is not None:
                        cache_read_tokens = (cache_read_tokens or 0) + u.cache_read_tokens
                    if u.uncached_input_tokens is not None:
                        uncached_input_tokens = (uncached_input_tokens or 0) + u.uncached_input_tokens
                    ms = comp_ev.payload.get("elapsed_ms")
                    if ms is not None and isinstance(ms, (int, float)):
                        total_elapsed_ms = (total_elapsed_ms or 0) + int(ms)
                else:
                    has_unknown = True

        through_seq = until_sequence if until_sequence is not None else s.last_event_sequence
        elapsed_sec = (total_elapsed_ms / 1000.0) if total_elapsed_ms is not None else None

        return SessionUsageReport(
            session_id=session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            uncached_input_tokens=uncached_input_tokens,
            has_unknown_usage=has_unknown,
            turn_count=turn_count,
            elapsed_seconds=elapsed_sec,
            since_sequence=since_sequence,
            until_sequence=until_sequence,
            through_sequence=through_seq,
        )

    def _get_owner_budget(self, session_id: str, owner: str) -> PluginBudget | None:
        state, _ = self.store.plugin_state(f"_budget_{owner}", "session", session_id)
        if state is not None and isinstance(state, Mapping) and state:
            try:
                return decode_budget(state)
            except Exception as exc:
                raise ValueError(f"Corrupt budget state for owner {owner!r}: {exc}") from exc
        return None

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

        # 1. Dispatch core reserved commands
        if name in ("run", "resume", "show", "status", "help"):
            if name == "run":
                prompt = str(arguments.get("prompt", ""))
                res = self.run(prompt, session_id=session_id)
                return CommandResult(
                    status="ok" if res.status in ("completed", "idle") else "error",
                    output=res.output,
                    error=res.error,
                    data=res.as_payload(),
                )
            if name == "resume":
                res = self.resume(session_id=session_id)
                return CommandResult(
                    status="ok" if res.status in ("completed", "idle") else "error",
                    output=res.output,
                    error=res.error,
                    data=res.as_payload(),
                )
            if name == "show":
                data = self.show(session_id, events=bool(arguments.get("events", False)))
                return CommandResult(status="ok", data=data)
            if name == "status":
                data = self.show(session_id, events=False)
                return CommandResult(status="ok", data=data)
            if name == "help":
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

        # 5. Core read-only policy check
        has_mutating_effects = any(e in (ToolEffect.WRITE, ToolEffect.EXECUTE, ToolEffect.SPAWN) for e in cmd_desc.effects)
        if self.config.policy.read_only and has_mutating_effects:
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
        )

        cmd_result, staged_state, staged_events, staged_continuations, staged_budget = self.host.invoke_command(
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
                owner=req.owner,
                command_name=req.command_name,
                arguments=req.arguments,
                revision_id=self.revision_id,
                ui_type=req.ui_type,
                ui_data=req.ui_data,
                expected_state_version=req.expected_state_version,
            )
            return cmd_result

        # Handle budget update
        if staged_budget == "clear":
            staged_state = tuple(staged_state) + (
                StagedStateUpdate(
                    owner=f"_budget_{cmd_desc.owner}",
                    scope="session",
                    scope_key=session_id,
                    payload={},
                    expected_version=0,
                ),
            )
        elif isinstance(staged_budget, PluginBudget):
            staged_state = tuple(staged_state) + (
                StagedStateUpdate(
                    owner=f"_budget_{cmd_desc.owner}",
                    scope="session",
                    scope_key=session_id,
                    payload=staged_budget.as_payload(),
                    expected_version=0,
                ),
            )

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
        )

        cmd_result, staged_state, staged_events, staged_continuations, staged_budget = self.host.invoke_command(
            session_id,
            rec.command_name,
            rec.arguments,
            context=context,
            client_capabilities=client_capabilities,
        )

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
        self.session(session_id)
        views: list[PluginViewDescriptor] = []
        for view_name in self.host.views:
            try:
                v = self.host.render_view(session_id, view_name, client_capabilities=client_capabilities)
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
            if s.status == "running":
                raise ValueError(f"Session {session_id} is currently running")

            claimed = self.store.claimed_continuations(session_id, limit)
            if not claimed:
                run_id = uuid4().hex
                claimed = self.store.claim_continuations(session_id, limit, run_id)

            results: list[ContinuationResult] = []
            for cont in claimed:
                run_id = cont.run_id or uuid4().hex
                logical_turn = uuid4().hex
                dispatch_event = EventDraft(
                    event_type="kernel.continuation_started",
                    source=f"plugin.{cont.owner}",
                    actor="plugin",
                    payload={"continuation_id": cont.continuation_id, "payload": cont.payload},
                )
                self.store.commit(
                    session_id,
                    expected_sequence=self.store.session(session_id).last_event_sequence,
                    expected_run_id=None,
                    run_id=run_id,
                    revision_id=self.revision_id,
                    events=(dispatch_event,),
                    status="running",
                    checkpoint_kind="ready",
                    checkpoint_payload={"step": 0, "continuation_id": cont.continuation_id, "logical_turn": logical_turn},
                    start_turn=True,
                )

                cur_session = self.store.session(session_id)
                cursor = _Cursor(continuation_id=cont.continuation_id, logical_turn=logical_turn)
                execution = _Execution(cur_session, cursor, list(self.store.events(session_id)), run_id)
                run_res = self._drive(execution, max_steps=max_steps)

                status_str: Literal["claimed", "completed"] = "completed"
                if run_res.status in ("completed", "idle", "failed"):
                    self.store.complete_continuation(cont.continuation_id)
                    status_str = "completed"
                elif run_res.status == "interrupted":
                    status_str = "claimed"

                results.append(
                    ContinuationResult(
                        continuation_id=cont.continuation_id,
                        owner=cont.owner,
                        session_id=session_id,
                        status=status_str,
                        payload=cont.payload,
                    )
                )
                if run_res.status == "interrupted":
                    break

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

    def _commit(self, execution: _Execution, status: str, kind: str, *events: EventDraft, start_turn: bool = False) -> None:
        previous = execution.session
        execution.session = self.store.commit(
            previous.session_id,
            expected_sequence=previous.last_event_sequence,
            expected_run_id=previous.run_id,
            run_id=execution.run_id,
            revision_id=self.revision_id,
            events=tuple(events),
            status=status,
            checkpoint_kind=kind,
            checkpoint_payload=execution.cursor.model_dump(),
            start_turn=start_turn,
        )
        new_events = self.store.events(previous.session_id, after_sequence=previous.last_event_sequence)
        execution.events.extend(new_events)
        all_staged_updates: list[StagedStateUpdate] = []
        all_staged_events: list[EventDraft] = []
        all_staged_conts: list[StagedContinuationDraft] = []
        for ev in new_events:
            obs_res = None
            if ev.event_type == "kernel.model_turn":
                turn = decode_provider_result(json_wire_object(ev.payload))
                d_seq = ev.payload.get("dispatch_sequence")
                disp_seq_val = int(d_seq) if isinstance(d_seq, (int, float)) else ev.sequence - 1
                intent_ev = next((e for e in execution.events if e.sequence == disp_seq_val and e.event_type == "kernel.provider_intent"), None)
                provider_name = str(intent_ev.payload.get("provider", "openai-chat")) if intent_ev else str(ev.payload.get("provider", "openai-chat"))
                model_name = str(intent_ev.payload.get("model", "unknown")) if intent_ev else str(ev.payload.get("raw_model", "unknown"))
                obs_res = self.host.notify_fact(
                    "usage",
                    {
                        "session_id": ev.session_id,
                        "run_id": ev.run_id,
                        "revision_id": ev.revision_id,
                        "event_sequence": ev.sequence,
                        "dispatch_sequence": disp_seq_val,
                        "provider_name": provider_name,
                        "model_name": model_name,
                        "usage": turn.usage.metadata_payload() if turn.usage else None,
                    },
                    logical_turn=execution.cursor.logical_turn,
                )
            elif ev.event_type in ("kernel.tool_completed", "kernel.tool_interrupted"):
                call_data = ev.payload.get("call")
                call_dict = call_data if isinstance(call_data, Mapping) else {}
                res_data = ev.payload.get("result")
                res_dict = res_data if isinstance(res_data, Mapping) else {}
                obs_res = self.host.notify_fact(
                    "invocation_finished",
                    {
                        "session_id": ev.session_id,
                        "run_id": ev.run_id,
                        "revision_id": ev.revision_id,
                        "event_sequence": ev.sequence,
                        "tool_name": str(call_dict.get("tool_name", "")),
                        "tool_call_id": call_dict.get("tool_call_id"),
                        "status": "ok" if ev.event_type == "kernel.tool_completed" else "error",
                        "error": str(res_dict["error"]) if "error" in res_dict and res_dict["error"] is not None else None,
                    },
                    logical_turn=execution.cursor.logical_turn,
                )
            elif ev.event_type in ("kernel.run_completed", "kernel.run_failed", "kernel.run_interrupted"):
                obs_res = self.host.notify_fact(
                    "run_finished",
                    {
                        "session_id": ev.session_id,
                        "run_id": ev.run_id,
                        "revision_id": ev.revision_id,
                        "event_sequence": ev.sequence,
                        "status": ev.event_type.replace("kernel.run_", ""),
                    },
                    logical_turn=execution.cursor.logical_turn,
                )

            if obs_res:
                staged_updates, staged_evs, staged_continuations, obs_errors = obs_res
                all_staged_updates.extend(staged_updates)
                all_staged_events.extend(staged_evs)
                all_staged_conts.extend(staged_continuations)
                for err in obs_errors:
                    all_staged_events.append(
                        EventDraft(
                            "kernel.observer_error",
                            source=f"plugin.{err.get('owner', 'observer')}",
                            actor="plugin",
                            payload={"owner": err.get("owner"), "error": err.get("error")},
                        )
                    )

        if all_staged_updates or all_staged_events or all_staged_conts:
            cur_sess = execution.session
            execution.session = self.store.commit_staged(
                cur_sess.session_id,
                expected_sequence=cur_sess.last_event_sequence,
                expected_run_id=cur_sess.run_id,
                run_id=execution.run_id,
                revision_id=self.revision_id,
                status=cur_sess.status,
                events=tuple(all_staged_events),
                staged_state=tuple(all_staged_updates),
                staged_continuations=tuple(all_staged_conts),
            )
            obs_new_events = self.store.events(cur_sess.session_id, after_sequence=cur_sess.last_event_sequence)
            execution.events.extend(obs_new_events)

    def _pending(self, execution: _Execution) -> tuple[StoredEvent, ToolCall]:
        cursor = execution.cursor
        if cursor.batch_sequence is None:
            raise ValueError("checkpoint has no pending batch")
        event = self._event(execution, cursor.batch_sequence, "kernel.model_turn")
        turn = decode_provider_result(json_wire_object(event.payload))
        if cursor.next_call >= len(turn.tool_calls):
            raise ValueError("checkpoint is outside its native batch")
        return event, turn.tool_calls[cursor.next_call]

    def _denial(self, call: ToolCall, definition: ToolDefinition) -> str | None:
        try:
            self._validators[definition.name].validate(json_wire_object(call.arguments))
        except jsonschema.ValidationError:
            return "tool arguments failed the admitted JSON schema"
        effects = definition.effects
        if self.config.policy.read_only and effects != {ToolEffect.READ}:
            return "read-only policy denies the declared tool effects"
        return None

    def _needs_approval(self, definition: ToolDefinition) -> bool:
        effects = definition.effects
        if effects == {ToolEffect.READ}:
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
            if self._abort.cancelled:
                return self._stop(execution, self._abort.reason or "cancelled")
            cursor = execution.cursor
            if cursor.batch_sequence is None:
                if cursor.step >= max_steps:
                    return self._stop(execution, "provider turn budget exhausted")
                current_usage = self._get_session_usage(execution.session.session_id, session=execution.session)
                if current_usage.has_unknown_usage:
                    return self._stop(execution, "cannot dispatch provider: session has unknown token usage")
                model = self.config.model.model
                if model is None:
                    return self._fail(execution, "no model configured")
                provider_name, separator, model_name = model.partition("/")
                entry = self.config.providers.get(provider_name)
                if not separator or not model_name or entry is None:
                    return self._fail(execution, "model must name a configured provider/model")
                try:
                    messages = self._messages(execution)
                except ValueError as exc:
                    return self._fail(execution, str(exc))
                request = ProviderTurnRequest(
                    messages=messages,
                    available_tools=self.host.tools,
                    raw_model=model,
                    provider_name=provider_name,
                    model_name=model_name,
                    reasoning_effort=self.config.model.reasoning_effort,
                    abort_signal=self._abort,
                )
                self._commit(
                    execution,
                    "running",
                    "ready",
                    EventDraft(
                        "kernel.provider_intent",
                        "kernel",
                        "kernel",
                        {"attempt": cursor.step, "provider": provider_name, "model": model_name},
                    ),
                )
                try:
                    turn = self.host.propose_turn(request, provider=entry.wire)
                except (HostUnavailable, HostTimeout, InvocationCancelled) as exc:
                    self._commit(
                        execution,
                        "interrupted",
                        "interrupted",
                        EventDraft("kernel.provider_interrupted", "kernel", "kernel", {"error": str(exc), "unknown_usage": True}),
                    )
                    return self._stop(execution, str(exc))
                except Exception as exc:
                    self._commit(
                        execution,
                        "interrupted",
                        "interrupted",
                        EventDraft("kernel.provider_interrupted", "kernel", "kernel", {"error": str(exc), "unknown_usage": True}),
                    )
                    return self._fail(execution, f"provider failed: {type(exc).__name__}: {exc}")
                cursor.step += 1
                ids = [call.tool_call_id for call in turn.tool_calls]
                if any(not identity for identity in ids) or len(ids) != len(set(ids)):
                    return self._fail(execution, "provider returned missing/duplicate native call identities")
                if turn.done_reason not in {"stop", "tool_calls", "completed"}:
                    return self._fail(execution, f"provider did not complete its turn: {turn.done_reason}")
                if turn.tool_calls:
                    cursor.batch_sequence = execution.session.last_event_sequence + 1
                    cursor.next_call = 0
                    self._commit(execution, "running", "ready", EventDraft("kernel.model_turn", "provider", "model", encode_provider_result(turn)))
                else:
                    self._commit(
                        execution,
                        "completed",
                        "terminal",
                        EventDraft("kernel.model_turn", "provider", "model", encode_provider_result(turn)),
                        EventDraft("kernel.run_completed", "kernel", "kernel", {}),
                    )
                    return self._result(execution, output=turn.output)
                if self._abort.cancelled:
                    return self._stop(execution, self._abort.reason or "cancelled")
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
                cursor.approval_sequence = execution.session.last_event_sequence + 1
                self._commit(execution, "waiting", "approval_wait", EventDraft("kernel.approval_requested", "kernel", "model", payload))
                return self._result(execution, pending_approval=payload)
            stopped = self._invoke(execution, call, definition)
            if stopped is not None:
                return stopped

    def _invoke(self, execution: _Execution, call: ToolCall, definition: ToolDefinition) -> KernelResult | None:
        cursor = execution.cursor
        if cursor.intent_sequence is None:
            invocation_id = uuid4().hex
            cursor.intent_sequence = execution.session.last_event_sequence + 1
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
                    },
                ),
            )
        else:
            intent = self._event(execution, cursor.intent_sequence, "kernel.tool_intent")
            invocation_id = str(intent.payload["invocation_id"])
        context = ToolContext(
            workspace=self.workspace,
            session_id=execution.session.session_id,
            run_id=execution.run_id,
            invocation_id=invocation_id,
            revision_id=self.revision_id,
            actor="model",
            capabilities=self.host.capabilities(call.tool_name),
            abort_signal=self._abort,
        )
        try:
            result = self.host.invoke(call, context=context)
        except (HostUnavailable, HostTimeout, InvocationCancelled) as exc:
            # Intent deliberately remains pending; recovery cannot mistake a lost
            # response for proof that no external mutation happened.
            return self._stop(execution, str(exc))
        except Exception as exc:
            result = ToolFailure(call.tool_name, f"tool failed: {type(exc).__name__}: {exc}")
        if result.tool_name != call.tool_name:
            return self._stop(execution, "tool result identity differs from the dispatched native call")
        self._report(execution, call, result)
        return None

    def _report(self, execution: _Execution, call: ToolCall, result: ToolResult, *, interrupted: bool = False) -> None:
        cursor = execution.cursor
        batch_sequence = cursor.batch_sequence
        cursor.next_call += 1
        cursor.intent_sequence = None
        cursor.approval_sequence = None
        turn = decode_provider_result(json_wire_object(self._event(execution, batch_sequence or 0, "kernel.model_turn").payload))
        if cursor.next_call == len(turn.tool_calls):
            cursor.batch_sequence = None
            cursor.next_call = 0
            cursor.abandon_batch = False
        payload = {"batch_sequence": batch_sequence, "call": encode_tool_call(call), "result": encode_tool_result(result)}
        self._commit(
            execution,
            "running",
            "ready",
            EventDraft("kernel.tool_interrupted" if interrupted else "kernel.tool_completed", "kernel" if interrupted else "tool", "model", payload),
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
                turn = decode_provider_result(json_wire_object(event.payload))
                messages.append(
                    ProviderMessage("assistant", turn.output, reasoning=turn.reasoning, tool_calls=turn.tool_calls, metadata=turn.metadata or {})
                )
                pending_calls = turn.tool_calls
                pending_index = 0
                batch_sequence = event.sequence
            elif event.event_type in {"kernel.tool_completed", "kernel.tool_interrupted"}:
                if not pending_calls or event.payload["batch_sequence"] != batch_sequence:
                    raise ValueError("tool result has no matching native batch")
                call = pending_calls[pending_index]
                if json_wire_object(event.payload)["call"] != encode_tool_call(call):
                    raise ValueError("tool results are not the authentic completed batch prefix")
                raw_result = event.payload["result"]
                if not isinstance(raw_result, Mapping):
                    raise ValueError("tool result event is not an object")
                result = decode_tool_result(json_wire_object(raw_result))
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
        if not allow_pending and sum(len(json.dumps(encode_message(message), allow_nan=False).encode("utf-8")) for message in messages) > 256 * 1024:
            raise ValueError("context exceeded the kernel's hard bound; explicit compaction is required")
        # Contributed context from plugins
        try:
            contributions = self.host.contribute_context(execution.session.session_id, logical_turn=execution.cursor.logical_turn)
            for c in contributions:
                if c.get("type") == "text":
                    owner = str(c.get("owner", "plugin"))
                    content = str(c.get("content", ""))
                    messages.append(ProviderMessage("system", f"[{owner} context]\n{content}"))
        except Exception as exc:
            raise RuntimeError(f"Context provider failed: {exc}") from exc

        return tuple(messages)

    def _stop(self, execution: _Execution, message: str) -> KernelResult:
        self._commit(execution, "interrupted", "interrupted", EventDraft("kernel.run_interrupted", "kernel", "kernel", {"reason": message}))
        return self._result(execution, error=message)

    def _fail(self, execution: _Execution, message: str) -> KernelResult:
        self._commit(execution, "failed", "terminal", EventDraft("kernel.run_failed", "kernel", "kernel", {"error": message}))
        return self._result(execution, error=message)

    def _result(
        self,
        execution: _Execution,
        *,
        output: str | None = None,
        error: str | None = None,
        pending_approval: Mapping[str, object] | None = None,
    ) -> KernelResult:
        return KernelResult(
            execution.session.session_id,
            execution.run_id,
            self.revision_id,
            execution.session.status,
            output=output,
            error=error,
            pending_approval=pending_approval,
        )
