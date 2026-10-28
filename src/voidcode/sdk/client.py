"""Typed client contracts: envelopes, run results, and session event frames.

Contracts only: frozen dataclasses with strict ``from_payload`` ingress,
plus the ``SessionClient`` Protocol both TUI clients satisfy. No sockets,
no storage, no ``Core`` — the outer layer implements the Protocol.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .commands import CommandCatalog, CommandResult
from .hooks import StopCause
from .json_values import json_wire_object, own_json_object
from .limits import CLIENT_PROMPT_MAX_BYTES
from .services import ContinuationResult, SessionUsageReport
from .transcript import ContextHistory, TranscriptEntry, TranscriptEntryWire, TranscriptPage
from .views import ViewsSnapshot


def _decode[T](adapter: TypeAdapter[T], payload: Mapping[str, object], label: str) -> T:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} payload must be an object")
    return adapter.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


@dataclass(frozen=True, slots=True)
class SessionRow:
    """One workspace session as clients list it."""

    session_id: str
    workspace: str
    status: str
    active_revision_id: str
    run_id: str | None
    turn: int
    last_event_sequence: int

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> SessionRow:
        return _decode(_SESSION_ROW_ADAPTER, payload, "session")


_SESSION_ROW_ADAPTER: TypeAdapter[SessionRow] = TypeAdapter(SessionRow)


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Core health and identity: the stable surface clients poll."""

    status: str
    stage: str
    pid: int
    workspace: str
    revision_id: str
    launch_protocol: int
    core_version: str
    web_bundle: str | None
    generation: int
    active_upgrade: Mapping[str, object] | None

    def __post_init__(self) -> None:
        if self.active_upgrade is not None:
            object.__setattr__(self, "active_upgrade", own_json_object(self.active_upgrade))

    @property
    def fenced(self) -> bool:
        """Whether business writes are refused while an upgrade drains."""
        return self.stage != "serving"

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> HealthStatus:
        return _decode(_HEALTH_ADAPTER, payload, "health")


_HEALTH_ADAPTER: TypeAdapter[HealthStatus] = TypeAdapter(HealthStatus)


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    """One transport refusal: message plus optional machine code."""

    error: str
    code: str | None = None

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ErrorEnvelope:
        return _decode(_ERROR_ADAPTER, payload, "error")


_ERROR_ADAPTER: TypeAdapter[ErrorEnvelope] = TypeAdapter(ErrorEnvelope)


class CoreClientError(ValueError):
    """One failed client call: the decoded official refusal plus its HTTP status.

    ``str(exc)`` is the server's message verbatim; ``code`` is its machine
    code (``stale_revision``, ``invalid_params``, ``upgrade_pending``, ...),
    ``status_code`` the HTTP status. A refusal body that is not the official
    ``{"error", "code?"}`` shape raises ``ValueError`` naming the malformation
    instead of hiding it.
    """

    def __init__(self, message: str, *, status_code: int = 0, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code

    @staticmethod
    def from_refusal(payload: object, *, status_code: int) -> CoreClientError:
        """Decode one official refusal body; malformed bodies fail visibly."""
        if not isinstance(payload, Mapping):
            raise ValueError(f"refusal body must be an object, got {type(payload).__name__}")
        envelope = ErrorEnvelope.from_payload(payload)
        return CoreClientError(envelope.error, status_code=status_code, code=envelope.code)


#: Shared client ingress bounds: exactly the HTTP transport's. The byte bound
#: is the neutral sdk/limits value the core ingress guard also enforces.
MAX_PROMPT_CHARS = 65536
MAX_PROMPT_BYTES = CLIENT_PROMPT_MAX_BYTES
MAX_STEPS = 1024


def _validated_steps(max_steps: int) -> None:
    if type(max_steps) is not int or not 1 <= max_steps <= MAX_STEPS:
        raise ValueError(f"max_steps must be an integer in 1..{MAX_STEPS}")


def _validated_prompt(prompt: str) -> None:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be nonempty text")
    if len(prompt) > MAX_PROMPT_CHARS or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError(f"prompt must be within {MAX_PROMPT_CHARS} chars and {MAX_PROMPT_BYTES // 1024} KiB")


@dataclass(frozen=True, slots=True)
class RunSpec:
    """One agent-turn request."""

    prompt: str
    max_steps: int = 32

    def __post_init__(self) -> None:
        _validated_prompt(self.prompt)
        _validated_steps(self.max_steps)


@dataclass(frozen=True, slots=True)
class ResumeSpec:
    """One resume request, optionally resolving a native tool approval."""

    approval_request_id: str | None = None
    decision: Literal["allow", "deny"] | None = None
    max_steps: int = 32

    def __post_init__(self) -> None:
        if (self.approval_request_id is None) != (self.decision is None):
            raise ValueError("approval_request_id and decision travel together or not at all")
        if self.decision is not None and self.decision not in ("allow", "deny"):
            raise ValueError("decision must be 'allow' or 'deny'")
        _validated_steps(self.max_steps)


@dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of one run/resume call: terminal facts, never a live handle."""

    session_id: str
    run_id: str
    revision_id: str
    status: str
    output: str | None = None
    error: str | None = None
    pending_approval: Mapping[str, object] | None = None
    stop_cause: StopCause | None = None

    def __post_init__(self) -> None:
        if self.pending_approval is not None:
            object.__setattr__(self, "pending_approval", own_json_object(self.pending_approval))

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> RunResult:
        return _decode(_RUN_RESULT_ADAPTER, payload, "run result")

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
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


_RUN_RESULT_ADAPTER: TypeAdapter[RunResult] = TypeAdapter(RunResult)


@dataclass(frozen=True, slots=True)
class CancelReceipt:
    """One cancellation request outcome."""

    session_id: str
    cancellation_requested: bool

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> CancelReceipt:
        return _decode(_CANCEL_ADAPTER, payload, "cancel receipt")


_CANCEL_ADAPTER: TypeAdapter[CancelReceipt] = TypeAdapter(CancelReceipt)


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One stored session event as session event frames carry it.

    ``transcript`` preserves the server's conversation projection as a typed
    ``TranscriptEntry`` (``None`` for bookkeeping events); the wire shape is
    ``StreamEventWire``, always discriminated — this module knows no event vocabulary.
    """

    session_id: str
    sequence: int
    type: str
    run_id: str
    revision_id: str
    source: str
    actor: str
    payload: Mapping[str, object]
    transcript: TranscriptEntry | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> StreamEvent:
        from .transcript import decode_transcript_entry

        if not isinstance(payload, Mapping):
            raise ValueError("stream event payload must be an object")
        raw_transcript = payload.get("transcript")
        entry: TranscriptEntry | None = None
        if raw_transcript is not None:
            if not isinstance(raw_transcript, Mapping):
                raise ValueError("stream event transcript must be an object or null")
            entry = decode_transcript_entry(raw_transcript)
        body = {key: value for key, value in payload.items() if key != "transcript"}
        decoded = _decode(_STREAM_EVENT_ADAPTER, body, "stream event")
        object.__setattr__(decoded, "transcript", entry)
        return decoded


_STREAM_EVENT_ADAPTER: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)


@dataclass(frozen=True, slots=True)
class HeadFrame:
    """First frame of every stream: the session record plus replayed backlog."""

    session: SessionRow
    events: tuple[StreamEvent, ...]

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> HeadFrame:
        if not isinstance(payload, Mapping):
            raise ValueError("head frame payload must be an object")
        raw_events = payload.get("events")
        if not isinstance(raw_events, (list, tuple)):
            raise ValueError("head frame must carry an events array")
        raw_session = payload.get("session")
        if not isinstance(raw_session, Mapping):
            raise ValueError("head frame must carry a session object")
        for item in raw_events:
            if not isinstance(item, Mapping):
                raise ValueError("head frame events must be objects")
        return HeadFrame(
            session=SessionRow.from_payload(raw_session),
            events=tuple(StreamEvent.from_payload(item) for item in raw_events if isinstance(item, Mapping)),
        )


@dataclass(frozen=True, slots=True)
class EventFrame:
    """One live event on a follow stream."""

    session_id: str
    event: StreamEvent

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> EventFrame:
        if not isinstance(payload, Mapping):
            raise ValueError("event frame payload must be an object")
        raw_event = payload.get("event")
        if not isinstance(raw_event, Mapping):
            raise ValueError("event frame must carry an event object")
        raw_session = payload.get("session_id")
        if not isinstance(raw_session, str):
            raise ValueError("event frame must carry a session_id")
        return EventFrame(session_id=raw_session, event=StreamEvent.from_payload(raw_event))


@dataclass(frozen=True, slots=True)
class ErrorFrame:
    """One terminal stream failure; the stream ends after it."""

    session_id: str
    error: str

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ErrorFrame:
        return _decode(_ERROR_FRAME_ADAPTER, payload, "error frame")


_ERROR_FRAME_ADAPTER: TypeAdapter[ErrorFrame] = TypeAdapter(ErrorFrame)


@dataclass(frozen=True, slots=True)
class RestartFrame:
    """Upgrade-drain watermark; the client reconnects from its own applied cursor."""

    session_id: str
    cursor: int

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> RestartFrame:
        return _decode(_RESTART_ADAPTER, payload, "restart frame")


_RESTART_ADAPTER: TypeAdapter[RestartFrame] = TypeAdapter(RestartFrame)


type SessionFrame = HeadFrame | EventFrame | ErrorFrame | RestartFrame


def parse_session_frame(payload: Mapping[str, object]) -> SessionFrame:
    """Decode one raw session event frame into its typed variant, failing visibly."""
    if not isinstance(payload, Mapping):
        raise ValueError("session event frame must be an object")
    if "events" in payload or "session" in payload:
        return HeadFrame.from_payload(payload)
    kind = payload.get("kind")
    body = {key: value for key, value in payload.items() if key != "kind"}
    if kind == "event":
        return EventFrame.from_payload(body)
    if kind == "error":
        return ErrorFrame.from_payload(body)
    if kind == "server_restart":
        return RestartFrame.from_payload(body)
    raise ValueError(f"unknown session event frame kind: {kind!r}")


class ClientWireModel(BaseModel):
    """Strict wire-model base for the client-visible session event/OpenAPI schemas."""

    model_config = ConfigDict(extra="forbid", strict=True)


class StreamEventWire(ClientWireModel):
    """One stored session event as session event frames carry it; payload stays dynamic."""

    session_id: str
    sequence: int
    type: str
    run_id: str
    revision_id: str
    source: str
    actor: str
    payload: dict[str, Any]
    transcript: TranscriptEntryWire | None = None


class HeadFrameWire(ClientWireModel):
    """First frame of every stream; head iff the frame carries events."""

    session: SessionRow
    events: list[StreamEventWire]


class EventFrameWire(ClientWireModel):
    kind: Literal["event"]
    session_id: str
    event: StreamEventWire


class ErrorFrameWire(ClientWireModel):
    kind: Literal["error"]
    session_id: str
    error: str


class RestartFrameWire(ClientWireModel):
    kind: Literal["server_restart"]
    session_id: str
    cursor: int


SessionFrameWire = Annotated[HeadFrameWire | EventFrameWire | ErrorFrameWire | RestartFrameWire, Field(union_mode="left_to_right")]


class StreamCancellation:
    """Cancels one owned event-stream connection from another thread.

    Neutral stdlib-only handle: the owning stream attaches its running loop +
    task structurally, and any thread (or a newer generation replacing it) may
    close it. No transport or client dependency.
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._loop is not None and self._task is not None and not self._task.done():
                if not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._task.cancel)
            self._loop = None
            self._task = None

    def attach(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task[Any]) -> None:
        """Bind the running stream; a pre-closed handle cancels it at once."""
        with self._lock:
            if self._closed:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(task.cancel)
            else:
                self._loop = loop
                self._task = task

    def detach(self) -> None:
        with self._lock:
            self._loop = None
            self._task = None


@runtime_checkable
class SessionClient(Protocol):
    """One typed client surface over a core, local or remote."""

    def close(self) -> None: ...

    def health(self) -> HealthStatus: ...

    def command_catalog(self) -> CommandCatalog: ...

    def list_sessions(self, limit: int = 100) -> list[SessionRow]: ...

    def create_session(self, session_id: str | None = None) -> SessionRow: ...

    def get_session(self, session_id: str) -> SessionRow: ...

    def show(self, session_id: str, events: bool = False) -> Mapping[str, Any]: ...

    def run(self, session_id: str, spec: RunSpec) -> RunResult: ...

    def resume(self, session_id: str, spec: ResumeSpec | None = None) -> RunResult: ...

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object] | None = None,
        expected_revision: str = "",
    ) -> CommandResult: ...

    def confirm(
        self,
        session_id: str,
        request_id: str,
        decision: Literal["allow", "deny"],
        expected_revision: str = "",
    ) -> CommandResult: ...

    def views(self, session_id: str) -> ViewsSnapshot: ...

    def transcript(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage: ...

    def context_history(self, session_id: str, *, limit: int = 100) -> ContextHistory: ...

    def drain(self, session_id: str, *, limit: int = 10) -> list[ContinuationResult]: ...

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport: ...

    def cancel(self, session_id: str) -> CancelReceipt: ...

    def events_stream(
        self,
        session_id: str,
        after_sequence: int = 0,
        *,
        follow: bool = True,
        cancel: StreamCancellation | None = None,
    ) -> AsyncGenerator[SessionFrame]: ...


__all__ = [
    "CancelReceipt",
    "ClientWireModel",
    "CoreClientError",
    "ErrorEnvelope",
    "ErrorFrame",
    "ErrorFrameWire",
    "EventFrame",
    "EventFrameWire",
    "HeadFrame",
    "HeadFrameWire",
    "HealthStatus",
    "MAX_PROMPT_BYTES",
    "MAX_PROMPT_CHARS",
    "MAX_STEPS",
    "RestartFrame",
    "RestartFrameWire",
    "ResumeSpec",
    "RunResult",
    "RunSpec",
    "SessionClient",
    "SessionRow",
    "SessionFrame",
    "SessionFrameWire",
    "StreamCancellation",
    "StreamEvent",
    "StreamEventWire",
    "parse_session_frame",
]
