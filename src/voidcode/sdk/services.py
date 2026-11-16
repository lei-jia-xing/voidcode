"""Scoped runtime services, continuation results, and usage models."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable

from pydantic import TypeAdapter

from .history import PAGE_BYTES
from .json_values import json_wire_object, own_json_object

if TYPE_CHECKING:
    from .history import HistoryPage
    from .hooks import FactPage, StopCause
    from .invocation import ToolContext
    from .observed_effects import ObservedEffects
    from .resources import ResourcePage, ResourceReadRequest
    from .tools import ExecutionObservation, ToolDefinition, ToolResult
    from .transcript import ContextHistory, TranscriptPage


class QueryUnavailable(ValueError):
    """No admitted plugin in this runtime generation answers the named query."""


class QueryContractError(ValueError):
    """A query answered, but its answer violated the agreed contract.

    Raised by the *consumer's* answer decoder (e.g. ``workspace_queries``), never by the
    transport: ``services.query`` raises ``QueryUnavailable`` only when no admitted plugin
    answers the name, returns ``None`` when the provider knows no matching resource, and
    lets any other provider failure surface as its own exception. A typed contract's three-way
    split is therefore: catch ``QueryUnavailable`` for absent providers (take the
    documented optional path), accept ``None`` as "no such resource", and report
    ``QueryContractError`` as a provider bug — never as ordinary absence.
    """


@dataclass(frozen=True, slots=True)
class BudgetExhaustion:
    """One finite execution cap that stopped a run without claiming success."""

    dimension: Literal["provider_turns", "root_actions"]
    source: Literal["max_steps", "root_action_budget"]
    limit: int
    consumed: int
    remaining: int
    continue_with: Literal["resume_with_max_steps", "extend_root_actions"]

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
        """Parse a committed typed cap fact; absent legacy data remains unknown."""
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("budget exhaustion must be an object or null")
        if set(value) != {"dimension", "source", "limit", "consumed", "remaining", "continue_with"}:
            raise ValueError("budget exhaustion has missing or unknown fields")
        dimension = value["dimension"]
        source = value["source"]
        continue_with = value["continue_with"]
        counters = (value["limit"], value["consumed"], value["remaining"])
        if any(type(counter) is not int or counter < 0 for counter in counters):
            raise ValueError("budget exhaustion counters must be non-negative integers")
        if (dimension, source, continue_with) not in (
            ("provider_turns", "max_steps", "resume_with_max_steps"),
            ("root_actions", "root_action_budget", "extend_root_actions"),
        ):
            raise ValueError("budget exhaustion has an unknown dimension, source, or continuation")
        return cls(
            dimension=cast(Literal["provider_turns", "root_actions"], dimension),
            source=cast(Literal["max_steps", "root_action_budget"], source),
            limit=counters[0],
            consumed=counters[1],
            remaining=counters[2],
            continue_with=cast(Literal["resume_with_max_steps", "extend_root_actions"], continue_with),
        )


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
    """Outcome of ONE delegated child run, as observed by its parent.

    ``run_id`` identifies the logical turn this outcome belongs to; a follow-up run is a new
    ``run_id`` in the same child session, so a caller can never mistake an older terminal fact
    for the result of newer work. ``history_sha256`` names the immutable snapshot of the child's
    readable history at that terminal point (``artifact://<sha256>``); the live, paged history
    stays ``transcript://<child_session_id>``. The plugin-provided ``agent://<child_session_id>``
    resource is distinct: it projects current descendant status and the latest eligible nonempty
    completed publication without consuming notices. VoidCode has no separate accepted-yield
    marker, so this selection is not a claim of exact OMP yield equivalence. A recoverable
    finite-cap interruption carries the cap facts committed in that run's stop event; it does
    not claim terminal completion or changed authorization.
    """

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
            payload["output_preview"] = self.output_preview
        if self.budget_exhaustion is not None:
            payload["budget_exhaustion"] = self.budget_exhaustion.as_payload()
        return payload


@dataclass(frozen=True, slots=True)
class ChildSendReceipt:
    """Receipt for one message to an owned child session.

    A receipt reports only durable acceptance: ``started`` means a run identity was claimed and
    the turn is part of that run before this method returned; ``queued`` means the message is a
    pending continuation and a later safe boundary will claim it. Neither means the child
    consumed the message or finished; read the outcome with ``join_child`` or the notice.
    """

    child_session_id: str
    delivery: Literal["started", "queued"]
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

    def as_payload(self) -> dict[str, object]:
        return {
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


@dataclass(frozen=True, slots=True)
class ChildSessionSnapshot:
    """One caller-scoped current descendant row and its latest eligible published outcome.

    Core snapshots select only committed completed outcomes with nonempty inline output or a
    full output artifact; failed, interrupted, cancelled, empty, and preview-only outcomes do
    not erase the prior result. Inline output is materialized as a deduplicated immutable
    artifact for resource paging, without changing the committed event or join result. Current
    lifecycle status and run identity remain on ``descendant`` and may identify a newer run.
    The durable child fact has no explicit accepted-yield marker; this selection is a documented
    approximation, not OMP-equivalent yield semantics.
    """

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
    stream: str
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
class ProcessResult:
    """Outcome of one lease-scoped child process.

    `stdout`/`stderr` are decoded UTF-8 (invalid bytes replaced) and bounded; when a stream was
    cut at the capture bound `truncated` is set, so a caller never mistakes a clipped capture for
    complete output. A process the runtime stopped is reported by `timed_out`/`cancelled`, which
    are separate from the real exit status.
    """

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
    status: Literal["completed", "interrupted", "cancelled", "kernel_lost", "budget_exhausted"]
    result: ToolResult | None
    terminal_sequence: int
    stop_cause: StopCause | None = None
    budget_exhaustion: BudgetExhaustion | None = None
    execution: ExecutionObservation | None = None

    def __post_init__(self) -> None:
        for name in ("request_id", "owner", "action_id", "session_id", "revision_id", "tool_name"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"tool await result requires a non-empty {name}")
        if self.status not in ("completed", "interrupted", "cancelled", "kernel_lost", "budget_exhausted"):
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
    def abort_signal(self) -> threading.Event:
        """Set when the owner cancels or the core shuts down; the driver polls it."""
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
class PluginServices(Protocol):
    """Scoped services available to a plugin handler during lease callback."""

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
    ) -> str:
        """Start a delegated child session of the bound session; returns its durable session id.

        `max_steps`, when supplied, is an explicit provider-turn cap for this child run. The child
        inherits its root action authorization from its parent; it cannot create or extend it.
        The child can only be narrower than its parent: depth, the chain's spawn budget, the
        parent's tool scope, and the parent's restrictive stance bound it. A read-only lease may
        therefore delegate read-only work, but never spawn a writable child; `readonly=True`
        records the child's stance durably on the child session (the core owns that record).

        With `notify_on_completion` the core delivers one bounded, run-scoped notice to this
        session when the child's run reaches a terminal state, even after this turn ends, and the
        child's readable history is spilled to an immutable artifact. The notice is keyed by the
        run, never by the child alone, so a follow-up run cannot be masked by an older one.

        With `idempotency_key` the identity is deterministic per parent, owner, and key: a retry
        with the same effective spec returns the existing child, while the same key with a
        different spec is refused visibly instead of spawning a second child.
        """
        ...

    def send_child(
        self,
        child_session_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
    ) -> ChildSendReceipt:
        """Send a follow-up message to ONE child this session spawned, in its own session.

        This is a SESSION verb, not a filesystem write and not user authority: the child keeps its
        tool scope, its read-only overlay, and the core's policy. Only the direct parent may
        send (the core checks lineage); siblings, parents, and unrelated sessions are refused.
        A running or approval-parked child queues the message durably and consumes it at its next
        safe boundary; an open child starts a new turn immediately. The receipt says `started` or
        `queued` and never claims consumption or completion.
        """
        ...

    def join_child(self, child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        """Wait for the child's CURRENT run to reach a terminal state.

        While the child is running or has a queued follow-up, this waits instead of returning an
        older terminal fact. The result carries the run id it belongs to.
        """
        ...

    def wait_children(self, child_session_ids: Sequence[str]) -> tuple[ChildOutcome, ...]:
        """Wait for undelivered results from these directly owned child sessions.

        Existing eligible results are returned before waiting; otherwise wait for the first
        selected child result. Every outcome retains its exact child/run identity. The Core
        stages delivery receipts and pending-notice cancellation with this invocation, so
        callers must not acknowledge outcomes returned here. An empty tuple means no eligible
        result or registered work remains; interruption, cancellation, and shutdown are not
        represented as an empty success.
        """
        ...

    def acknowledge_child_completion(self, child_session_id: str, run_id: str) -> None:
        """Stage delivery of this exact known terminal child run.

        Unknown or nonterminal runs are a no-op; a known old run remains eligible even when a
        newer run is current. The receipt is staged even if no completion notice exists (for
        example, `notify_on_completion=False`). An unstarted notice for this run is cancelled;
        claimed or delivered notices are untouched. Both effects commit with any valid result,
        including `ToolFailure`; exceptions, invalid results, abort, revocation, or shutdown do
        not consume the outcome.
        """
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

    def read_facts(self, *, after_sequence: int = 0, limit: int = 100) -> FactPage:
        """Read typed committed facts using the same decoder as live observers.

        Limits count source events (1..200). Advance next_sequence even for empty
        pages; has_more, not the fact count, determines whether to continue.
        Recorded facts must satisfy the current contract; incomplete records raise.
        """
        ...

    def read_context_history(self, *, limit: int = 600) -> ContextHistory:
        """Read a certified source prefix (1..2000 records) for compaction.

        Includes bookkeeping sizes and safe prefix boundaries. Records are whole;
        the bound counts source records, not bytes. The core revalidates any
        declared projection independently when it is submitted.
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
    ) -> HistoryPage:
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
    ) -> HistoryPage:
        """Render one bounded Markdown transcript page for the bound session or an owned descendant.

        ``page_bytes`` bounds the rendered text (1..40960); callers needing a
        smaller presentation page pass a smaller budget and follow the returned
        cursor. The default preserves the existing 40 KiB page.
        """
        ...

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

    def invoke_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        expected_revision: str,
    ) -> ToolInvocationReceipt:
        """Invoke another tool through this session's ordinary admission path.

        The core resolves the target in the active revision the caller displayed
        (`expected_revision`), applies the session's tool scope, read-only stance, pre-tool gates,
        and declared-effect approval policy exactly as it does for a model-requested call.
        Requests are staged atomically with the caller's result and execute
        only after its callback returns; this receipt never waits on execution or human consent.
        Approvable targets go through the ordinary approval boundary, never a rejection for
        needing approval. Recursion consumes the chain's action budget and carries core-counted
        depth. Hook leases cannot invoke.
        """
        ...

    def run_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[str, bytes], None] | None = None,
    ) -> ProcessResult:
        """Run one child process of this runtime, in its own process group under this lease.

        The runtime owns the process, the bytes, and the bounds, and the
        core's own governance already decided whether the calling tool may execute at all. What
        the lease adds is stance: a read-only lease and a hook lease are refused outright, because
        neither may touch the machine. `env` overrides the inherited environment; `cwd` defaults to
        the workspace. Each stream is captured up to `max_output_bytes` and the excess is dropped
        with `truncated` set. A lease cancellation kills the whole group before returning.
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
        on_output: Callable[[str, bytes], None] | None = None,
    ) -> ProcessResult:
        """Run one read-only query child process of this runtime under this lease.

        Unlike `run_process`, a read-only lease IS permitted to execute read-only query
        commands (such as code search, AST queries, linters, or non-mutating analysis tools).
        Hook leases remain purely observational and are refused outright. When sandboxed,
        the process runs under a read-only profile, and no mutable filesystem observations
        are staged.
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

    def spawn_service_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        """Start a supervised *service* process: a helper this runtime generation owns end to end.

        Same identity, stdin, log paging, and cancellation surface as `spawn_process` (`write_process`,
        `read_process_output`, `cancel_process`, `list_processes` all address it by its id), with one
        lifetime difference: a service process belongs to the runtime generation that started it. It
        is stopped when that generation retires — a reload/retire or owner shutdown — because it
        runs in its own process group, while a background process keeps its
        documented "survives a generation swap" contract. Service processes are the right shape for
        a language server or another interactive helper: they are not a user job the caller persists,
        and a stale one outliving its generation would hold the workspace.
        """
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
        """Start a supervised process that outlives this invocation, and return its identity.

        Supervised means the runtime owns the live process and its group: `cancel_process` stops the
        whole group, and the owner's shutdown stops every supervised process. What the caller owns
        is the identity — persist `ProcessHandle` (pid + start_time + log paths): after the caller's
        own restart the runtime no longer supervises that process, and only that identity can tell a
        live process from a recycled pid. Output goes to the log paths you name (created, never
        truncated); pass none and the runtime spools into its own temporary files, whose paths the
        returned handle carries. A duplicate `process_id` is refused, so a retried start cannot
        silently create a second process.
        """
        ...

    def write_process(self, process_id: str, data: bytes | str) -> int:
        """Write to a supervised process's stdin; returns the bytes accepted.

        Only for processes started with `stdin=True` in this runtime: a process that survived a
        core restart keeps running but its pipe did not, so that write is refused rather than lost.
        """
        ...

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: Literal["stdout", "stderr"] = "stdout",
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

    def model_query(self, prompt: str, *, model: str | None = None, max_output_tokens: int | None = None, include_context: bool = False) -> str:
        """One tool-free provider turn; optionally read the current context without extending it."""
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

    def find_child(self, *, idempotency_key: str) -> str | None:
        """The child this parent spawned under `idempotency_key`, if any; never spawns.

        Owner- and parent-bound: a key another owner used is not visible here. This is the
        read-only recovery path for a crash between spawn and the caller's own bookkeeping.
        """
        ...

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        """Stage cancellation of pending or claimed continuations owned by this plugin."""
        ...

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
