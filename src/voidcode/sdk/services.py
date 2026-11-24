"""Scoped runtime services, continuation results, and usage models."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, NoReturn, Protocol, runtime_checkable

from pydantic import TypeAdapter, ValidationError

from .json_values import json_wire_object, own_json_object

if TYPE_CHECKING:
    from .ask import AskRequestDraft
    from .foreground import CompositionDraft
    from .hooks import FactPage, StopCause
    from .invocation import ToolContext
    from .observed_effects import ObservedEffects
    from .provider import ProviderDoneReason
    from .resources import ResourcePage, ResourceReadRequest
    from .tools import ExecutionObservation, ToolDefinition, ToolResult
    from .transcript import ContextHistory, TranscriptEntry, TranscriptPage


#: Rendered-text budget for one history page. Defined here because the SDK
#: service default and core's renderer must agree, and the SDK cannot import core.
PAGE_BYTES: Final[int] = 40 * 1024

type ProcessStream = Literal["stdout", "stderr"]


@dataclass(frozen=True, slots=True)
class StructuredQueryParseError:
    """Why one structured model reply produced no valid data.

    ``invalid_json`` covers empty, unparseable, and non-object top-level JSON
    (a refusal is unparseable text, not data). ``schema_violation`` covers
    well-formed JSON the result schema rejects. ``path`` is the failing JSON
    path of a schema violation, or ``None`` when nothing parsed.
    """

    kind: Literal["invalid_json", "schema_violation"]
    message: str
    path: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("invalid_json", "schema_violation"):
            raise ValueError(f"unknown structured query parse error kind {self.kind!r}")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("structured query parse error requires a non-empty message")

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"kind": self.kind, "message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True, slots=True)
class StructuredModelResult:
    """One tool-free provider turn parsed against a caller-supplied result schema.

    ``raw_text`` is the verbatim model reply, always kept. Exactly one of
    ``data`` (owned, schema-validated JSON object) and ``parse_error`` is set:
    an invalid reply is a callable failure, never silent success and never
    data. ``done_reason`` is the provider's own terminal marker, passed through
    unchanged. Cancellation, budget exhaustion, and provider errors propagate
    as exceptions — exactly as the bare ``model_query`` — and never surface here.
    """

    raw_text: str
    data: Mapping[str, object] | None = None
    parse_error: StructuredQueryParseError | None = None
    done_reason: ProviderDoneReason = "completed"

    def __post_init__(self) -> None:
        if self.data is not None:
            object.__setattr__(self, "data", own_json_object(self.data))
        if (self.data is None) == (self.parse_error is None):
            raise ValueError("structured model result requires exactly one of data or parse_error")

    def as_payload(self) -> dict[str, object]:
        return {
            "raw_text": self.raw_text,
            "data": None if self.data is None else json_wire_object(self.data),
            "parse_error": None if self.parse_error is None else self.parse_error.as_payload(),
            "done_reason": self.done_reason,
        }


@dataclass(frozen=True, slots=True)
class StructuredChildResult:
    """One delegated child run read against the parent's declared result schema.

    ``raw_text`` is the child's full terminal text, always kept. Exactly one of
    ``data`` (owned, schema-validated JSON object) and ``parse_error`` is set:
    an invalid delivery is a callable failure, never silent success and never
    data. ``output_uri`` names the immutable full-output artifact when the run
    spilled one; ``None`` means the run completed inline (use ``raw_text``),
    never \"no output\". Non-completed runs have no structured reading at all:
    the caller reports status, never this shape.
    """

    child_session_id: str
    run_id: str | None
    raw_text: str
    data: Mapping[str, object] | None = None
    parse_error: StructuredQueryParseError | None = None
    output_uri: str | None = None
    output_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.data is not None:
            object.__setattr__(self, "data", own_json_object(self.data))
        if (self.data is None) == (self.parse_error is None):
            raise ValueError("structured child result requires exactly one of data or parse_error")

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "child_session_id": self.child_session_id,
            "run_id": self.run_id,
            "raw_text": self.raw_text,
            "data": None if self.data is None else json_wire_object(self.data),
            "parse_error": None if self.parse_error is None else self.parse_error.as_payload(),
        }
        if self.output_uri is not None:
            payload["output_uri"] = self.output_uri
        if self.output_bytes is not None:
            payload["output_bytes"] = self.output_bytes
        return payload


class StateConflict(RuntimeError):
    """One owner-state optimistic-concurrency check lost; the transaction committed nothing.

    Raised both when a callback stages with a stale version and when the commit's row
    version no longer matches. A caller that can re-read and re-apply a pure state
    mutation may retry; a caller must never treat it as a completed write.
    """


class QueryUnavailable(ValueError):
    """No admitted plugin in this runtime generation answers the named query."""


class QueryContractError(ValueError):
    """Raised when a query answer violates its expected contract schema."""


class ContextContributionFailed(RuntimeError):
    """A registered plugin context provider failed while assembling context."""


class ModelQueryError(Exception):
    """Base type for governed model-query failures; classification never uses message text."""


class ModelQueryCancelled(RuntimeError, ModelQueryError):
    """A governed model query was cancelled before it produced a usable answer."""


class ModelQueryToolCalls(RuntimeError, ModelQueryError):
    """A governed model query returned native tool calls although none were offered."""


class ModelQueryBudgetExhausted(RuntimeError, ModelQueryError):
    """An active session/plugin budget refused a governed model query before dispatch."""


class ModelQueryContextFailed(ContextContributionFailed, ModelQueryError):
    """A context provider failed while a governed model query assembled its context."""


class ModelQueryUnavailable(RuntimeError, ModelQueryError):
    """This runtime has no governed model-query callback."""


class ModelQueryModelUnavailable(ValueError, ModelQueryError):
    """No configured model, or a model name that is not an admitted provider/model."""


class ModelQueryPromptTooLong(ValueError, ModelQueryError):
    """The model-query prompt exceeds the core's UTF-8 prompt ceiling."""


type ProcessFailureKind = Literal["start_failed", "capture_failed", "sandbox_failed", "process_failed"]


class ProcessFailure(ValueError):
    """A process invocation failed; ``kind`` is the stable classification."""

    kind: ProcessFailureKind
    detail: str

    def __init__(self, kind: ProcessFailureKind, detail: str) -> None:
        if kind not in {"start_failed", "capture_failed", "sandbox_failed", "process_failed"}:
            raise ValueError(f"unknown process failure kind: {kind!r}")
        if not isinstance(detail, str) or not detail:
            raise ValueError("process failure detail must be non-empty text")
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class ProcessAlreadyRunningError(ValueError):
    """A spawn reuses a process id held by a live process.

    Callers branch on this type, never on the message, so wording is free.
    """

    def __init__(self, process_id: str) -> None:
        super().__init__(f"background process {process_id} is already running")
        self.process_id = process_id


type BudgetExhaustionDimension = Literal[
    "provider_turns",
    "root_actions",
    "owner_tokens",
    "owner_cost_usd",
    "owner_elapsed_seconds",
]
type BudgetExhaustionSource = Literal[
    "max_steps",
    "root_action_budget",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "max_cost_usd",
    "max_elapsed_seconds",
]
type BudgetExhaustionContinuation = Literal["resume_with_max_steps", "extend_root_actions", "raise_owner_budget"]

_ALLOWED_BUDGET_EXHAUSTION_FORMS: frozenset[tuple[BudgetExhaustionDimension, BudgetExhaustionSource, BudgetExhaustionContinuation]] = frozenset(
    {
        ("provider_turns", "max_steps", "resume_with_max_steps"),
        ("root_actions", "root_action_budget", "extend_root_actions"),
        ("owner_tokens", "max_input_tokens", "raise_owner_budget"),
        ("owner_tokens", "max_output_tokens", "raise_owner_budget"),
        ("owner_tokens", "max_total_tokens", "raise_owner_budget"),
        ("owner_cost_usd", "max_cost_usd", "raise_owner_budget"),
        ("owner_elapsed_seconds", "max_elapsed_seconds", "raise_owner_budget"),
    }
)


@dataclass(frozen=True, slots=True)
class BudgetExhaustion:
    """One finite execution cap that stopped a run without claiming success.

    Provider-turn and root-action caps keep their existing dimensions. An owner
    budget stop uses ``owner_tokens`` (with the exact ``max_input_tokens``,
    ``max_output_tokens``, or ``max_total_tokens`` source), ``owner_cost_usd``,
    or ``owner_elapsed_seconds``. Its counters are the measured value and the
    configured limit, with ``remaining`` clamped at zero; an unknown metric fails
    before a stop and never produces a guessed exhaustion.
    """

    dimension: BudgetExhaustionDimension
    source: BudgetExhaustionSource
    limit: int | float
    consumed: int | float
    remaining: int | float
    continue_with: BudgetExhaustionContinuation

    def as_payload(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "source": self.source,
            "limit": self.limit,
            "consumed": self.consumed,
            "remaining": self.remaining,
            "continue_with": self.continue_with,
        }

    @classmethod
    def from_payload(cls, value: object) -> BudgetExhaustion | None:
        """Parse a committed typed cap fact; absent stays absent."""
        if value is None:
            return None
        if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
            raise ValueError("budget exhaustion must be an object with string keys or null")
        wire_map: dict[str, object] = {k: v for k, v in value.items() if isinstance(k, str)}
        try:
            parsed = _BUDGET_EXHAUSTION_ADAPTER.validate_json(json.dumps(json_wire_object(wire_map), allow_nan=False), strict=True, extra="forbid")
        except ValidationError as exc:
            raise ValueError(f"budget exhaustion is invalid: {exc}") from exc
        if parsed.limit < 0 or parsed.consumed < 0 or parsed.remaining < 0:
            raise ValueError("budget exhaustion counters must be non-negative numbers")
        if parsed.dimension in ("provider_turns", "root_actions", "owner_tokens") and any(
            type(value) is not int for value in (parsed.limit, parsed.consumed, parsed.remaining)
        ):
            raise ValueError("budget exhaustion counters must be integers for token and turn caps")
        if (parsed.dimension, parsed.source, parsed.continue_with) not in _ALLOWED_BUDGET_EXHAUSTION_FORMS:
            raise ValueError("budget exhaustion has an unknown dimension, source, or continuation")
        return parsed


_BUDGET_EXHAUSTION_ADAPTER: TypeAdapter[BudgetExhaustion] = TypeAdapter(BudgetExhaustion)


@dataclass(frozen=True, slots=True)
class ContinuationResult:
    continuation_id: str
    owner: str
    session_id: str
    status: Literal["pending", "claimed", "completed", "failed", "cancelled"]
    payload: Mapping[str, object]
    run_status: str | None = None
    run_id: str | None = None
    output: str | None = None
    error: str | None = None
    stop_cause: str | None = None
    budget_exhaustion: BudgetExhaustion | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))

    def as_payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "continuation_id": self.continuation_id,
            "owner": self.owner,
            "session_id": self.session_id,
            "status": self.status,
            "payload": json_wire_object(self.payload),
        }
        if self.run_status is not None:
            result["run_status"] = self.run_status
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.output is not None:
            result["output"] = self.output
        if self.error is not None:
            result["error"] = self.error
        if self.stop_cause is not None:
            result["stop_cause"] = self.stop_cause
        if self.budget_exhaustion is not None:
            result["budget_exhaustion"] = self.budget_exhaustion.as_payload()
        return result

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ContinuationResult:
        if not isinstance(payload, Mapping):
            raise ValueError("continuation result payload must be an object")
        return _CONTINUATION_ADAPTER.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


_CONTINUATION_ADAPTER: TypeAdapter[ContinuationResult] = TypeAdapter(ContinuationResult)


@dataclass(frozen=True, slots=True)
class EventView:
    """One bounded, read-only view of a committed session event."""

    sequence: int
    event_type: str
    run_id: str
    revision_id: str
    source: str
    actor: str
    payload: Mapping[str, object] | None = None
    payload_truncated: bool = False

    def __post_init__(self) -> None:
        if self.payload is not None:
            object.__setattr__(self, "payload", own_json_object(self.payload))

    def as_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "source": self.source,
            "actor": self.actor,
            "payload": None if self.payload is None else json_wire_object(self.payload),
            "payload_truncated": self.payload_truncated,
        }


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """Outcome of one delegated child run, as observed by its parent."""

    child_session_id: str
    status: Literal["completed", "failed", "interrupted", "cancelled", "running", "waiting"]
    output: str | None = None
    error: str | None = None
    stop_cause: str | None = None
    run_id: str | None = None
    history_sha256: str | None = None
    history_bytes: int | None = None
    history_truncated: bool = False
    #: Bounded preview when the terminal output was too large to cross the runtime boundary inline.
    output_preview: str | None = None
    #: Full output artifact ref from publication or snapshot materialization for immutable resource paging.
    output_sha256: str | None = None
    output_bytes: int | None = None
    #: Committed capture cause when the exact run's large output could not be spilled; None is no committed failure.
    output_error: str | None = None
    #: Committed capture cause when the exact run's history snapshot could not be spilled; None is no committed failure.
    history_error: str | None = None
    budget_exhaustion: BudgetExhaustion | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "child_session_id": self.child_session_id,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "stop_cause": self.stop_cause,
        }
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.history_sha256 is not None:
            payload["history_sha256"] = self.history_sha256
            payload["history_bytes"] = self.history_bytes
            payload["history_truncated"] = self.history_truncated
        if self.output_sha256 is not None:
            payload["output_sha256"] = self.output_sha256
            payload["output_bytes"] = self.output_bytes
        if self.output_preview is not None:
            payload["output_preview"] = self.output_preview
        if self.output_error is not None:
            payload["output_error"] = self.output_error
        if self.history_error is not None:
            payload["history_error"] = self.history_error
        if self.budget_exhaustion is not None:
            payload["budget_exhaustion"] = self.budget_exhaustion.as_payload()
        return payload


@dataclass(frozen=True, slots=True)
class ChildSendReceipt:
    """Receipt for one message to an owned child session.

    A receipt reports only durable acceptance: ``started`` means a run identity was claimed and
    the turn is part of that run before this method returned; ``queued`` means the message is a
    pending continuation and a later safe boundary will claim it; ``parked`` means the message is
    durable but the child is interrupted/parked and will not claim it without an explicit resume
    (or cancellation). None of these values means the child consumed the message or finished;
    read the outcome with ``join_child`` or the notice.
    """

    child_session_id: str
    delivery: Literal["started", "queued", "parked"]
    child_status: str
    run_id: str | None = None
    message_id: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "child_session_id": self.child_session_id,
            "delivery": self.delivery,
            "child_status": self.child_status,
        }
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.message_id is not None:
            payload["message_id"] = self.message_id
        return payload


@dataclass(frozen=True, slots=True)
class DescendantNode:
    """One live descendant of the bound session: verbatim status, never a collapsed outcome."""

    session_id: str
    parent_session_id: str | None
    depth: int
    status: str
    run_id: str | None = None
    stop_cause: str | None = None
    label: str | None = None
    activity: str | None = None
    turn: int = 0
    last_event_sequence: int = 0
    has_queued_followup: bool = False
    awaiting_approval: bool = False
    started_at_ms: int | None = None
    updated_at_ms: int | None = None
    elapsed_ms: int | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "depth": self.depth,
            "status": self.status,
            "run_id": self.run_id,
            "stop_cause": self.stop_cause,
            "label": self.label,
            "activity": self.activity,
            "turn": self.turn,
            "last_event_sequence": self.last_event_sequence,
            "has_queued_followup": self.has_queued_followup,
            "awaiting_approval": self.awaiting_approval,
        }
        if self.started_at_ms is not None:
            payload["started_at_ms"] = self.started_at_ms
        if self.updated_at_ms is not None:
            payload["updated_at_ms"] = self.updated_at_ms
        if self.elapsed_ms is not None:
            payload["elapsed_ms"] = self.elapsed_ms
        return payload


@dataclass(frozen=True, slots=True)
class ChildSessionSnapshot:
    """One caller-scoped descendant record and its latest eligible outcome."""

    descendant: DescendantNode
    latest_outcome: ChildOutcome | None = None

    def __post_init__(self) -> None:
        if self.latest_outcome is not None and self.latest_outcome.child_session_id != self.descendant.session_id:
            raise ValueError("child snapshot outcome must belong to its descendant")


@dataclass(frozen=True, slots=True)
class CancelSubtreeReceipt:
    """Outcome of one bound subtree cancellation request: accepted signals plus already-terminal facts."""

    target_session_id: str
    expected_run_id: str | None
    cancelled: tuple[str, ...] = ()
    already_terminal: tuple[str, ...] = ()
    disarmed_queued: tuple[str, ...] = ()
    claimed_reserved: tuple[str, ...] = ()
    observed_newer_run: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("cancelled", "already_terminal", "disarmed_queued", "claimed_reserved", "observed_newer_run"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def as_payload(self) -> dict[str, object]:
        return {
            "target_session_id": self.target_session_id,
            "expected_run_id": self.expected_run_id,
            "cancelled": list(self.cancelled),
            "already_terminal": list(self.already_terminal),
            "disarmed_queued": list(self.disarmed_queued),
            "claimed_reserved": list(self.claimed_reserved),
            "observed_newer_run": list(self.observed_newer_run),
        }


@dataclass(frozen=True, slots=True)
class PluginBudget:
    """Owner-scoped budget constraints for an active goal or task."""

    owner: str
    since_sequence: int = 0
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    max_elapsed_seconds: float | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "since_sequence": self.since_sequence,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_total_tokens": self.max_total_tokens,
            "max_cost_usd": self.max_cost_usd,
            "max_elapsed_seconds": self.max_elapsed_seconds,
        }


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Immutable artifact identity and stored metadata; ``uri`` is derived, not persisted."""

    sha256: str
    bytes: int
    media_type: str = "application/octet-stream"

    @property
    def uri(self) -> str:
        return f"artifact://{self.sha256}"

    def as_payload(self) -> dict[str, object]:
        return {"sha256": self.sha256, "bytes": self.bytes, "media_type": self.media_type}


@dataclass(frozen=True, slots=True)
class ArtifactPage:
    """One bounded read of an artifact: `data` at `offset`, `eof` when the store ends there."""

    artifact: ArtifactReference
    offset: int
    data: bytes
    eof: bool

    @property
    def text(self) -> str:
        """The page decoded UTF-8 (invalid bytes replaced) for callers that page text."""
        return self.data.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class ProcessHandle:
    """Identity of one supervised background process, as the caller persists it.

    `pid` alone is not an identity: `start_time` is the core's own start stamp for that pid, so a
    later reuse of the number can never be mistaken for this process.
    """

    process_id: str
    pid: int
    start_time: str
    stdout_path: str
    stderr_path: str
    running: bool = True
    exit_code: int | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "pid": self.pid,
            "start_time": self.start_time,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "running": self.running,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    """One bounded page of a supervised process's log, with its liveness at read time."""

    process_id: str
    stream: ProcessStream
    offset: int
    total_bytes: int
    data: bytes
    eof: bool
    running: bool

    @property
    def text(self) -> str:
        """The page decoded UTF-8 (invalid bytes replaced) for callers that page text."""
        return self.data.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class RawStreamCapture:
    """One raw byte stream durably captured to its own artifact.

    Exactly one of ``artifact`` (stored bytes) and ``capture_error`` (the
    verbatim store/append/finish cause) is set, so a silent partial is
    unrepresentable. ``truncated`` is ``bytes_dropped > 0`` regardless of
    cause: bound truncation and post-store-failure drops both count.
    """

    stream: ProcessStream
    bytes_captured: int
    bytes_dropped: int
    artifact: ArtifactReference | None
    capture_error: str | None = None

    def __post_init__(self) -> None:
        if self.stream not in ("stdout", "stderr"):
            raise ValueError(f"raw stream capture requires 'stdout' or 'stderr', got {self.stream!r}")
        for name in ("bytes_captured", "bytes_dropped"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"raw stream capture {name} must be a non-negative integer")
        if (self.artifact is None) == (self.capture_error is None):
            raise ValueError("raw stream capture requires exactly one of artifact or capture_error")
        if self.artifact is not None and not isinstance(self.artifact, ArtifactReference):
            raise ValueError("raw stream capture artifact must be an ArtifactReference or None")
        if self.capture_error is not None and (not isinstance(self.capture_error, str) or not self.capture_error):
            raise ValueError("raw stream capture capture_error must be non-empty text or None")

    @property
    def truncated(self) -> bool:
        return self.bytes_dropped > 0

    def as_payload(self) -> dict[str, object]:
        return {
            "stream": self.stream,
            "bytes_captured": self.bytes_captured,
            "bytes_dropped": self.bytes_dropped,
            "truncated": self.truncated,
            "artifact": None if self.artifact is None else self.artifact.as_payload(),
            "capture_error": self.capture_error,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> RawStreamCapture:
        if not isinstance(payload, Mapping):
            raise ValueError("raw stream capture payload must be an object")
        allowed = {"stream", "bytes_captured", "bytes_dropped", "truncated", "artifact", "capture_error"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError("raw stream capture contains unknown field(s): " + ", ".join(unknown))
        for name in ("stream", "bytes_captured", "bytes_dropped", "artifact", "capture_error"):
            if name not in payload:
                raise ValueError(f"raw stream capture is missing {name}")
        raw_stream = payload["stream"]
        if raw_stream == "stdout":
            stream: ProcessStream = "stdout"
        elif raw_stream == "stderr":
            stream = "stderr"
        else:
            raise ValueError(f"raw stream capture requires 'stdout' or 'stderr', got {raw_stream!r}")
        captured = payload["bytes_captured"]
        dropped = payload["bytes_dropped"]
        if type(captured) is not int or captured < 0:
            raise ValueError("raw stream capture bytes_captured must be a non-negative integer")
        if type(dropped) is not int or dropped < 0:
            raise ValueError("raw stream capture bytes_dropped must be a non-negative integer")
        error = payload["capture_error"]
        if error is not None and (not isinstance(error, str) or not error):
            raise ValueError("raw stream capture capture_error must be non-empty text or None")
        if "truncated" in payload:
            truncated = payload["truncated"]
            if type(truncated) is not bool or truncated != (dropped > 0):
                raise ValueError("raw stream capture truncated flag does not match bytes_dropped")
        raw_artifact = payload["artifact"]
        reference: ArtifactReference | None
        if raw_artifact is None:
            reference = None
        else:
            if not isinstance(raw_artifact, Mapping) or set(raw_artifact) != {"sha256", "bytes", "media_type"}:
                raise ValueError("raw stream capture artifact must be an artifact reference or null")
            sha256 = raw_artifact["sha256"]
            total = raw_artifact["bytes"]
            media_type = raw_artifact["media_type"]
            if not isinstance(sha256, str) or type(total) is not int or not isinstance(media_type, str):
                raise ValueError("raw stream capture artifact fields have the wrong types")
            reference = ArtifactReference(sha256=sha256, bytes=total, media_type=media_type)
        return RawStreamCapture(
            stream=stream,
            bytes_captured=captured,
            bytes_dropped=dropped,
            artifact=reference,
            capture_error=error,
        )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Outcome of one lease-scoped child process execution."""

    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False
    #: Bounded observed-effects report when this execution ran confined; None on the
    #: unconfined/absent path. Failed/cancelled/timed-out runs still carry the prefix.
    effects: ObservedEffects | None = None
    #: Raw byte captures: empty in text mode (today); exactly (stdout, stderr) when requested.
    raw: tuple[RawStreamCapture, ...] = ()

    def __post_init__(self) -> None:
        raw = tuple(self.raw)
        object.__setattr__(self, "raw", raw)
        if any(not isinstance(entry, RawStreamCapture) for entry in raw):
            raise ValueError("process result raw captures must be RawStreamCapture entries")
        if len(raw) not in (0, 2):
            raise ValueError("process result raw must be empty or exactly its stdout/stderr pair")
        if len(raw) == 2 and (raw[0].stream != "stdout" or raw[1].stream != "stderr"):
            raise ValueError("process result raw captures must arrive in stdout/stderr order")


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Neutral facts about one session, read as one row (never a scan).

    `pending_approval` is the parked approval request this session is waiting on, if any, in the
    same shape the core reports to a client. `chain_budget`/`chain_reserved` are the caller's
    root action chain's total and spent actions; nested requests consume this same budget.
    """

    session_id: str
    status: str
    run_id: str | None
    revision_id: str
    parent_session_id: str | None
    root_session_id: str
    depth: int
    turn: int
    last_event_sequence: int
    tool_allowlist: tuple[str, ...] | None = None
    pending_approval: Mapping[str, object] | None = None
    chain_budget: int | None = None
    chain_reserved: int | None = None
    budget: PluginBudget | None = None

    def __post_init__(self) -> None:
        if self.pending_approval is not None:
            object.__setattr__(self, "pending_approval", own_json_object(self.pending_approval))
        if self.tool_allowlist is not None:
            object.__setattr__(self, "tool_allowlist", tuple(self.tool_allowlist))

    def as_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "parent_session_id": self.parent_session_id,
            "root_session_id": self.root_session_id,
            "depth": self.depth,
            "turn": self.turn,
            "last_event_sequence": self.last_event_sequence,
            "tool_allowlist": None if self.tool_allowlist is None else list(self.tool_allowlist),
            "pending_approval": None if self.pending_approval is None else json_wire_object(self.pending_approval),
            "chain_budget": self.chain_budget,
            "chain_reserved": self.chain_reserved,
            "budget": None if self.budget is None else self.budget.as_payload(),
        }


@dataclass(frozen=True, slots=True)
class ToolInvocationReceipt:
    """A same-session request staged with the caller's result, never executed inside its callback.

    The core runs it after the callback returns, through ordinary admission and approval.
    Its committed result is available in this session's events under `request_id`.
    """

    request_id: str
    tool_name: str
    depth: int

    def as_payload(self) -> dict[str, object]:
        return {"request_id": self.request_id, "tool_name": self.tool_name, "depth": self.depth}


@dataclass(frozen=True, slots=True)
class ToolAwaitResult:
    """One exact background tool request's durable terminal outcome.

    A request cancelled before execution or lost with its in-memory driver has no
    `ToolResult`; the host never invents a success/failure for work that did not run.
    """

    request_id: str
    owner: str
    action_id: str
    session_id: str
    revision_id: str
    tool_name: str
    status: Literal["completed", "interrupted", "cancelled", "core_lost", "budget_exhausted"]
    result: ToolResult | None
    terminal_sequence: int
    stop_cause: StopCause | None = None
    budget_exhaustion: BudgetExhaustion | None = None
    execution: ExecutionObservation | None = None

    def __post_init__(self) -> None:
        for name in ("request_id", "owner", "action_id", "session_id", "revision_id", "tool_name"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"tool await result requires a non-empty {name}")
        if self.status not in ("completed", "interrupted", "cancelled", "core_lost", "budget_exhausted"):
            raise ValueError(f"unknown tool await terminal status {self.status!r}")
        if type(self.terminal_sequence) is not int or self.terminal_sequence < 1:
            raise ValueError("tool await terminal sequence must be a positive integer")
        if self.status == "completed" and self.result is None:
            raise ValueError("completed tool await must carry its actual result")
        if self.status == "budget_exhausted" and self.budget_exhaustion is None:
            raise ValueError("budget-exhausted tool await must carry its typed exhaustion")

    def as_payload(self) -> dict[str, object]:
        from .codec import encode_tool_result

        return {
            "request_id": self.request_id,
            "owner": self.owner,
            "action_id": self.action_id,
            "session_id": self.session_id,
            "revision_id": self.revision_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "result": None if self.result is None else encode_tool_result(self.result),
            "terminal_sequence": self.terminal_sequence,
            "stop_cause": self.stop_cause,
            "budget_exhaustion": None
            if self.budget_exhaustion is None
            else {
                "dimension": self.budget_exhaustion.dimension,
                "source": self.budget_exhaustion.source,
                "limit": self.budget_exhaustion.limit,
                "consumed": self.budget_exhaustion.consumed,
                "remaining": self.budget_exhaustion.remaining,
                "continue_with": self.budget_exhaustion.continue_with,
            },
            "execution": None
            if self.execution is None
            else {
                "cancellation_signalled": self.execution.cancellation_signalled,
                "execution_stopped": self.execution.execution_stopped,
                "side_effect_state": self.execution.side_effect_state,
            },
        }


@dataclass(frozen=True, slots=True)
class SessionUsageReport:
    """Truthful aggregate token and cost usage computed strictly from core facts."""

    session_id: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    uncached_input_tokens: int | None
    total_cost_usd: float | None = None
    has_unknown_usage: bool = False
    turn_count: int = 0
    elapsed_seconds: float | None = None
    since_sequence: int = 0
    until_sequence: int | None = None
    through_sequence: int = 0

    def as_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "total_cost_usd": self.total_cost_usd,
            "has_unknown_usage": self.has_unknown_usage,
            "turn_count": self.turn_count,
            "elapsed_seconds": self.elapsed_seconds,
            "since_sequence": self.since_sequence,
            "until_sequence": self.until_sequence,
            "through_sequence": self.through_sequence,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> SessionUsageReport:
        if not isinstance(payload, Mapping):
            raise ValueError("usage report payload must be an object")
        return _USAGE_ADAPTER.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


_USAGE_ADAPTER: TypeAdapter[SessionUsageReport] = TypeAdapter(SessionUsageReport)


@dataclass(frozen=True, slots=True)
class BackgroundActionStatus:
    """Durable status of one owner background action: its state and terminal error, if any."""

    action_id: str
    status: str
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id:
            raise ValueError("background action status needs a non-empty action id")
        if self.status not in ("pending", "running", "interrupted", "completed", "failed", "cancelled"):
            raise ValueError(f"background action status must be a known state, got {self.status!r}")

    def as_payload(self) -> dict[str, object]:
        return {"action_id": self.action_id, "status": self.status, "error": self.error}


@runtime_checkable
class BackgroundContext(Protocol):
    """The long-lived handle a background driver keeps across its whole action."""

    @property
    def action_id(self) -> str:
        """The durable continuation identity this driver run was claimed for."""
        ...

    @property
    def payload(self) -> Mapping[str, object]:
        """The immutable JSON object enqueued with the action."""
        ...

    @property
    def session_id(self) -> str:
        """The session whose state and child work this driver acts on."""
        ...

    @property
    def abort_signal(self) -> threading.Event:
        """Set when the owner cancels or the core shuts down; the driver polls it."""
        ...

    @property
    def activation_requested(self) -> threading.Event:
        """Set while a capability activation is trying to take the dispatch fence.

        A long-running driver should stop starting new dispatches (and leave its current
        one promptly) so activation can drain; the signal clears if the attempt is refused.
        """
        ...

    def invoke[T](self, callback: Callable[[ToolContext, PluginServices], T]) -> T:
        """Run `callback` under a fresh owner/revision-bound lease and commit its staged work.

        Each invoke is one atomic commit: state, events, and enqueues land durably before it
        returns, so a crash loses at most the in-flight callback. The services object must not
        escape the callback. When the abort signal is set, invoke raises `InterruptedError`
        and commits nothing.
        """
        ...

    def wait_tool(self, receipt: ToolInvocationReceipt) -> ToolAwaitResult:
        """Wait outside a callback lease for this action's exact deferred tool request.

        The implementation releases the callback/activation guards before waiting. Calling
        this reentrantly from an `invoke` callback is refused rather than deadlocking.
        """
        ...


@runtime_checkable
class StateServices(Protocol):
    """Session and plugin state: lease identity, state CAS, event staging, window projections and overlays."""

    @property
    def owner(self) -> str:
        """The admitted plugin identity bound to this live invocation lease."""
        ...

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        """Fetch current state and version for the bound owner in the given scope."""
        ...

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        """Stage optimistic concurrency state replacement for the bound owner."""
        ...

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        """Stage a plugin-namespaced event to be committed atomically with this turn."""
        ...

    def project_window(
        self,
        *,
        covered_from_sequence: int,
        covered_to_sequence: int,
        replacement: str,
    ) -> None:
        """Declare the projection that replaces a committed prefix of this session's window.

        The core verifies the boundary (it must end a completed native batch) and measures the
        accounting itself: the covered bytes are the encoded size of the replaced events, and the
        dropped bytes are what the replacement does not carry. A projection that would grow the
        window is refused. Events are never rewritten; only assembly substitutes the replacement.
        """
        ...

    def window_projection(self) -> Mapping[str, object] | None:
        """The session's current verified projection (coverage, replacement, measured accounting)."""
        ...

    def set_overlay(self, tier: Literal["read-only"] = "read-only") -> None:
        """Tighten the bound session with an owner-owned restrictive overlay.

        Overlays can only restrict; an owner revokes exactly its own row, and the core's
        frozen policy is never affected.
        """
        ...

    def clear_overlay(self) -> bool:
        """Revoke this owner's own overlay; returns whether one was active."""
        ...

    def overlay_active(self) -> bool:
        """Whether this owner's read-only overlay is active on the bound session.

        Reads the same session-overlay truth the core's effective read-only stance uses;
        another owner's overlay and the frozen core policy are not reported here.
        """
        ...

    def session_read_only(self) -> bool:
        """Effective read-only for the bound session: the frozen core policy or any owner overlay."""
        ...


@runtime_checkable
class ChildServices(Protocol):
    """Child sessions and deferred work: spawn, send, join, wait, acknowledge, cancel, lookup, and the continuation queue."""

    def enqueue_continuation(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        """Enqueue a durable continuation that the session renders as a message of `role`.

        A `user`-role continuation with a string `text` field is rendered verbatim as a user
        message; any other payload is rendered as its JSON under the core's attribution line.
        """
        ...

    def spawn_child(
        self,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        readonly: bool = False,
        notify_on_completion: bool = False,
        idempotency_key: str | None = None,
        result_schema: Mapping[str, object] | None = None,
    ) -> str:
        """Start a delegated child session under this parent session and return its id."""
        ...

    def send_child(
        self,
        child_session_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
    ) -> ChildSendReceipt:
        """Send a follow-up prompt to a spawned child session."""
        ...

    def join_child(self, child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        """Wait for the child session's current run to reach a terminal state."""
        ...

    def wait_children(self, child_session_ids: Sequence[str]) -> tuple[ChildOutcome, ...]:
        """Wait for undelivered terminal outcomes from owned child sessions."""
        ...

    def acknowledge_child_completion(self, child_session_id: str, run_id: str) -> None:
        """Stage delivery receipt for a completed terminal child run."""
        ...

    def cancel_child(self, child_session_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel a child this session spawned, whether it is running or parked for approval."""
        ...

    def list_descendants(self) -> tuple[DescendantNode, ...]:
        """Every transitive descendant of the bound session, in stable creation order."""
        ...

    def cancel_descendant_subtree(
        self,
        target_session_id: str,
        *,
        expected_run_id: str | None,
        reason: str = "owner cancellation",
    ) -> CancelSubtreeReceipt:
        """Request cancellation of a descendant and its whole subtree under a fenced run binding.

        Refused from read-only preparation leases. The target must still carry
        ``expected_run_id`` (None only when it never started); a newer run is a
        stale-target error with zero side effects. The receipt reports which
        sessions accepted the request and which were already terminal; signals
        are not proof the work stopped.
        """
        ...

    def find_child(self, *, idempotency_key: str) -> str | None:
        """The child this parent spawned under `idempotency_key`, if any; never spawns.

        Owner- and parent-bound: a key another owner used is not visible here. This is the
        read-only recovery path for a crash between spawn and the caller's own bookkeeping.
        """
        ...

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        """Stage cancellation of pending or claimed continuations owned by this plugin."""
        ...


@runtime_checkable
class ScheduledWorkServices(Protocol):
    """Durable future work: wakeups and owner background actions."""

    def schedule_wakeup(
        self,
        payload: Mapping[str, object],
        *,
        due_at_ms: int,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        """Schedule a future turn of the bound session; the core claims it atomically.

        With an idempotency key the identity is deterministic, so re-scheduling the same future
        point never creates a second wakeup.
        """
        ...

    def cancel_wakeup(self, wakeup_id: str) -> bool:
        """Cancel a pending wakeup owned by this plugin (claimed wakeups are untouched)."""
        ...

    def stage_wakeup(
        self,
        payload: Mapping[str, object],
        *,
        due_at_ms: int,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        """Schedule a future turn atomically with the caller's tool-result state and events.

        The row is inserted in the same transaction that commits the callback's
        staged work, so a rejected/aborted commit leaves no orphan wakeup. With an
        idempotency key the identity is deterministic and an already-committed
        row is named instead of duplicated.
        """
        ...

    def stage_cancel_wakeup(self, wakeup_id: str) -> None:
        """Cancel a pending wakeup in the same transaction as the caller's staged work."""
        ...

    def enqueue_background_action(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        """Stage one owner background action, run by this plugin's driver outside any model turn.

        The row commits atomically with the caller's result. With `idempotency_key` a retry
        returns the existing action id instead of staging a second action. The driver receives
        a `BackgroundContext` (never this lease) and commits per invoke, so progress is durable
        action by action, not at the end of the run.
        """
        ...

    def cancel_background_action(self, action_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel one owner background action: pending rows are marked cancelled, a live driver is signalled.

        Returns False when the id is unknown or already terminal. A claimed-but-unattended
        (stale) row is marked cancelled without touching any live driver.
        """
        ...

    def background_action_status(self, action_id: str) -> BackgroundActionStatus | None:
        """The durable status of one owner background action, or None when unknown.

        Foreign owners' actions are never visible here. `interrupted` means the claim was lost
        (shutdown or reload); resume is an explicit new action, never an automatic replay.
        """
        ...


class HistoryPageView(Protocol):
    """Structural view of the core history page for SDK signatures.

    The SDK cannot import core (see test_sdk_direction); ``core.history_renderer``
    owns the HistoryPage dataclass and satisfies this shape. Sync is checker-
    enforced: FakeServices subclasses PluginServices, so ty verifies the
    dataclass against this protocol on every override.
    """

    @property
    def text(self) -> str: ...

    @property
    def next_after_sequence(self) -> int: ...

    @property
    def next_offset(self) -> int: ...

    @property
    def truncated(self) -> bool: ...


@runtime_checkable
class HistoryServices(Protocol):
    """Committed-history reads: facts, events, transcripts, sessions and descendants."""

    def read_facts(self, *, after_sequence: int = 0, limit: int = 100) -> FactPage:
        """Read typed committed facts using the same decoder as live observers.

        Limits count source events (1..200). Advance next_sequence even for empty
        pages; has_more, not the fact count, determines whether to continue.
        Recorded facts must satisfy the current contract; incomplete records raise.
        """
        ...

    def read_context_history(self, *, after_sequence: int = 0, limit: int = 600) -> ContextHistory:
        """Read one page of the certified source prefix (1..2000 records) for compaction.

        ``after_sequence`` is exclusive: the page starts at the first source record after
        it. Pages preserve event order and whole records; every ``safe_boundary`` certifies
        the prefix from the session's first source record through that sequence, never a
        page suffix. ``reached_end`` means the page reaches the session's current last
        record. Advance with the last record's sequence; an empty page that is not at the
        end is a contract violation. The core revalidates any declared projection
        independently when it is submitted.
        """
        ...

    def read_transcript(self, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        """Read committed conversation records, scoped to the calling session.

        ``limit`` bounds source events (1..200). The page has a soft 64 KiB
        source-payload budget: an oversized first record is returned whole on
        its own so every message remains reachable. Advance the source cursor
        even on empty pages. This does not apply the current window projection.
        """
        ...

    def latest_event(self, event_type: str, run_id: str | None = None) -> TranscriptEntry | None:
        """Newest decoded conversation record of one event type in the calling session.

        A reverse lookup for callers that need the latest record of a kind without
        paging the whole transcript. Returns None when the session has no such
        event; a stored payload that no longer decodes fails loudly like any read.
        """
        ...

    def read_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        include_payload: bool = True,
    ) -> tuple[EventView, ...]:
        """Read this session's committed events, bounded in count and payload size."""
        ...

    def session_info(self) -> SessionInfo:
        """Facts about the bound session: status, run, revision, lineage, approval, chain budget.

        One row read of this session's own record; the chain budget it reports is the caller's
        root chain, which is what bounds delegation and nested invocation.
        """
        ...

    def read_child_history(
        self,
        child_session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
    ) -> HistoryPageView:
        """Render one bounded Markdown transcript page for an owned child session."""
        ...

    def read_session_history(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
        page_bytes: int = PAGE_BYTES,
    ) -> HistoryPageView:
        """Render one bounded Markdown transcript page for the bound session or an owned descendant.

        ``page_bytes`` bounds the rendered text (1..40960); callers needing a
        smaller presentation page pass a smaller budget and follow the returned
        cursor. The default preserves the existing 40 KiB page.
        """
        ...


@runtime_checkable
class ToolServices(Protocol):
    """Capability invocation and discovery: tools, plugin queries and model turns."""

    def tool_definition(self, name: str) -> ToolDefinition | None:
        """The definition of ``name`` from the active revision's dispatch snapshot, with this
        session's visibility applied.

        Answered by the core, never copied: it is the same snapshot and the same predicate
        dispatch advertises with, so the reserved core surface, the session's profile scope, and
        the effective read-only stance are all honored without a second catalog. ``None`` covers
        both an unknown name and a name this session may not see.
        """
        ...

    def query(self, name: str, arguments: Mapping[str, object] | None = None) -> Mapping[str, object] | None:
        """Ask the runtime for a named query another admitted plugin answers.

        Queries are runtime-local, read-only conveniences between trusted plugins (see
        ``PluginQuery``): the answering handler runs in this runtime process, receives the caller's
        lease-provided session and workspace, and returns one bounded JSON object or ``None`` for
        "no answer". No core governance is involved, so a query answers questions, never
        performs work. A name nobody answers raises ``QueryUnavailable``; every other failure
        (handler bug, malformed shape, oversized answer) raises visibly and is never confused
        with "unavailable".
        """
        ...

    def invoke_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        expected_revision: str,
    ) -> ToolInvocationReceipt:
        """Stage an invocation of another tool through the session's admission pipeline."""
        ...

    def model_query(self, prompt: str, *, model: str | None = None, max_output_tokens: int | None = None, include_context: bool = False) -> str:
        """One tool-free provider turn; optionally read the current context without extending it.

        The provider's actual usage is charged once to this session's usage report
        and any active owner budget. An exhausted budget refuses the call before
        dispatch, missing usage stays unknown, and a usage fact that cannot be
        recorded fails the call. The invoking lease's abort signal cancels the
        provider request; a cancelled turn raises ``ModelQueryCancelled`` and never
        returns an answer. Typed failures also cover ``ModelQueryBudgetExhausted``,
        ``ModelQueryContextFailed``, ``ModelQueryUnavailable``,
        ``ModelQueryModelUnavailable``, ``ModelQueryPromptTooLong``, and
        ``ModelQueryToolCalls``; callers classify by type, never message text.
        """
        ...

    def model_query_structured(
        self,
        prompt: str,
        *,
        result_schema: Mapping[str, object],
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
    ) -> StructuredModelResult:
        """One tool-free provider turn parsed and validated against ``result_schema``.

        Same governance, budget, usage, and cancellation as ``model_query``: the call
        enters the same dispatch path and usage accounting. ``result_schema`` is a
        JSON Schema (Draft 2020-12) describing one JSON object; the returned
        ``StructuredModelResult`` carries either validated ``data`` or a callable
        ``parse_error`` — never silent success. Cancellation, budget exhaustion,
        and provider errors propagate as exceptions, exactly as ``model_query``.
        """
        ...


@runtime_checkable
class ArtifactServices(Protocol):
    """Artifacts and scheme resources: spill/read content-addressed blobs and dispatch registered readers."""

    def read_resource(self, request: ResourceReadRequest) -> ResourcePage | None:
        """Dispatch one ``scheme://`` URI to its registering provider's reader.

        Runs synchronously inside this invocation under a provider-owner,
        read-only sub-lease (caller's session/workspace/revision). Returns
        ``None`` when the provider knows no such resource. An unregistered
        scheme raises ``ResourceUnavailable``; a malformed page raises
        ``ResourceContractError``; handler bugs and backend failures raise
        visibly and are never confused with "unavailable" or ``None``.
        """
        ...

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        """Store bytes under their own content address and return the reference to them.

        The artifact exists (immutably) before any fact that references it commits, so a result
        carrying a reference can never point at missing bytes. Spilling touches only the core's
        own store: a read-only lease may spill output it could not otherwise carry, a hook lease
        (an observer) may not.
        """
        ...

    def spill_artifact_stream(self, chunks: Iterable[bytes], *, media_type: str = "application/octet-stream") -> ArtifactReference:
        """Store a byte stream atomically under its digest.

        The host validates each chunk, enforces its artifact-store bound while writing, and
        discards an incomplete spill on cancellation, revocation, or producer failure. No
        reference is returned until all chunks have been durably published.
        """
        ...

    def read_artifact(
        self,
        sha256: str,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> ArtifactPage:
        """Read one byte page by SHA-256 digest; URI parsing belongs to the caller.

        Raises ``FileNotFoundError`` when the digest is well-formed but not
        stored; malformed digests, bad bounds, and storage failures raise
        ``ValueError``/``OSError`` and must never be treated as absence.
        """
        ...


@runtime_checkable
class SuspendServices(Protocol):
    """Suspension intents: foreground composition and ask waits."""

    def suspend_foreground_composition(self, draft: CompositionDraft, *, idempotency_key: str | None = None) -> NoReturn:
        """Stage this composition's wait-intent on the lease and raise SuspendComposition (never returns).

        Tool-callback leases only; hook/command/background leases are refused
        visibly. With idempotency_key a retried stage returns the existing
        composition instead of a second one (spawn_child precedent). Refused
        with CompositionNestedRefused when the suspending lease is already
        composition-bound (single composition slot, no stack); suspend_for_ask
        from such a lease remains allowed.
        """
        ...

    def suspend_for_ask(self, draft: AskRequestDraft, *, idempotency_key: str | None = None) -> NoReturn:
        """Validate refs/shape on the lease, stage the ask intent, raise SuspendAsk (never returns).

        Tool-callback leases only. Media digests are shape-checked here;
        existence is resolved by the plugin via read_artifact BEFORE suspending
        (FileNotFoundError = visible stage error, no park).
        """
        ...


@runtime_checkable
class ProcessServices(Protocol):
    """Child process lifecycle: run, query, spawn, I/O, cancel and list."""

    def run_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[ProcessStream, bytes], None] | None = None,
    ) -> ProcessResult:
        """Run one child process synchronously in its own process group under this lease.

        Raw byte capture is not implemented by the runtime; ``ProcessResult.raw`` and
        ``RawStreamCapture`` remain the shape a future capture implementation must use.
        """
        ...

    def run_query_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[ProcessStream, bytes], None] | None = None,
    ) -> ProcessResult:
        """Run one read-only query child process under this lease."""
        ...

    def spawn_service_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        """Start a supervised service process tied to the lifetime of this runtime generation."""
        ...

    def spawn_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bool = False,
        stdout_path: Path | str | None = None,
        stderr_path: Path | str | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        """Start a long-lived supervised process that outlives the current invocation."""
        ...

    def write_process(self, process_id: str, data: bytes | str) -> int:
        """Write to a supervised process's stdin; returns the bytes accepted.

        Only for processes started with `stdin=True` in this runtime: a process that survived a
        core restart keeps running but its pipe did not, so that write is refused rather than lost.
        The core bounds the write and honors the lease's cancellation, so a partial count is the
        honest result of a deadline, cancellation, or a pipe the process stopped reading.
        """
        ...

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: ProcessStream = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> ProcessOutput:
        """Read one bounded page of a supervised process's log, with its liveness."""
        ...

    def cancel_process(self, process_id: str, reason: str = "owner cancellation") -> bool:
        """Stop a supervised process: SIGTERM its group, then SIGKILL; True when it is gone."""
        ...

    def list_processes(self) -> tuple[ProcessHandle, ...]:
        """Every process this runtime supervises for this session, running or already exited."""
        ...


@runtime_checkable
class BudgetServices(Protocol):
    """Metering and owner budgets: usage reports and budget constraints."""

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        """Fetch truthful aggregate token usage and elapsed time, windowed by dispatch sequence."""
        ...

    def get_budget(self) -> PluginBudget | None:
        """Fetch active budget limits for the bound owner."""
        ...

    def set_budget(
        self,
        *,
        since_sequence: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_total_tokens: int | None = None,
        max_cost_usd: float | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> None:
        """Stage an owner-scoped budget constraint bound to this session and owner from baseline."""
        ...

    def clear_budget(self) -> None:
        """Stage removal of the bound owner's budget constraint."""
        ...


@runtime_checkable
class PluginServices(
    StateServices,
    ChildServices,
    HistoryServices,
    BudgetServices,
    ProcessServices,
    ArtifactServices,
    ToolServices,
    ScheduledWorkServices,
    SuspendServices,
    Protocol,
):
    """The full scoped-services union available to a plugin handler during lease callback.

    Plugins depend on facets; only the runtime implements this composition.
    """

    ...
