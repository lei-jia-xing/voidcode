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
    decode_provider_result,
    decode_tool_result,
    encode_message,
    encode_provider_result,
    encode_tool_call,
    encode_tool_result,
)
from ..sdk.config import KernelConfig
from ..sdk.invocation import ToolContext
from ..sdk.json_values import json_wire_object
from ..sdk.plugin import HostPlugin
from ..sdk.provider import ProviderMessage, ProviderTurnRequest
from ..sdk.tools import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult
from .host import HostClient, HostTimeout, HostUnavailable, InvocationCancelled
from .storage.store import EventDraft, SessionRecord, SqliteStore, StoredEvent


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
            self.host = HostClient(plugins, workspace=self.workspace, revision_id=self.revision_id, lease_fd=lease)
            resources.callback(self.host.close)
            self._definitions = {tool.name: tool for tool in self.host.tools}
            self._validators = {tool.name: jsonschema.Draft202012Validator(json_wire_object(tool.input_schema)) for tool in self.host.tools}
            self._resources = resources.pop_all()

    def __enter__(self) -> Kernel:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def cancel(self, reason: str = "user cancellation") -> None:
        self._abort.set_cancelled(True, reason=reason)

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
                or checkpoint.last_event_sequence != session.last_event_sequence
            ):
                raise ValueError("checkpoint identity does not match the durable session")
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
        execution.events.extend(self.store.events(previous.session_id, after_sequence=previous.last_event_sequence))

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
                try:
                    turn = self.host.propose_turn(request, provider=entry.wire)
                except (HostUnavailable, HostTimeout, InvocationCancelled) as exc:
                    return self._stop(execution, str(exc))
                except Exception as exc:
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
