"""Child, event, and session projections shared across the service surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..json_values import json_wire_object, own_json_object
from .budgets import BudgetExhaustion, PluginBudget

if TYPE_CHECKING:
    from ..hooks import StopCause


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


type TerminalCaptureKind = Literal[
    "inline",
    "artifact",
    "preview_only",
    "empty",
    "capture_failed",
    "not_completed",
]


@dataclass(frozen=True, slots=True)
class TerminalRunCapture:
    """One descendant's latest committed terminal fact and its output capture diagnosis.

    Identity is exact: ``run_id`` and ``terminal_sequence`` name one committed
    ``core.child_finished`` fact in the parent stream, independent of the latest
    *eligible* output. ``capture`` is a closed classification of why this run's
    output is or is not eligible; ``eligible`` is the same rule the snapshot's
    ``latest_outcome`` applies. Output fields describe THIS run only and never a
    prior publication: an artifact reference or bounded preview when captured,
    and the committed capture causes when full output/history bytes are absent.
    """

    run_id: str
    terminal_sequence: int
    status: Literal["completed", "failed", "interrupted", "cancelled"]
    capture: TerminalCaptureKind
    stop_cause: StopCause | None = None
    output_sha256: str | None = None
    output_bytes: int | None = None
    output_preview: str | None = None
    output_error: str | None = None
    history_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("terminal run capture requires a non-empty run_id")
        if type(self.terminal_sequence) is not int or self.terminal_sequence < 1:
            raise ValueError("terminal run capture requires a positive terminal_sequence")
        if self.status not in ("completed", "failed", "interrupted", "cancelled"):
            raise ValueError(f"unknown terminal run status {self.status!r}")
        if self.capture not in ("inline", "artifact", "preview_only", "empty", "capture_failed", "not_completed"):
            raise ValueError(f"unknown terminal capture kind {self.capture!r}")
        if self.capture == "not_completed":
            if self.status == "completed":
                raise ValueError("not_completed capture requires a non-completed terminal status")
        elif self.status != "completed":
            raise ValueError("completed capture kinds require a completed terminal status")
        if self.capture == "artifact" and (not isinstance(self.output_sha256, str) or not self.output_sha256):
            raise ValueError("artifact capture requires a non-empty output_sha256 reference")
        if self.output_bytes is not None and (type(self.output_bytes) is not int or self.output_bytes < 0):
            raise ValueError("terminal run capture output_bytes must be a non-negative integer")

    @property
    def eligible(self) -> bool:
        """Whether this run's captured output satisfies the latest-output eligibility rule."""
        return self.capture in ("inline", "artifact")

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "run_id": self.run_id,
            "terminal_sequence": self.terminal_sequence,
            "status": self.status,
            "capture": self.capture,
            "eligible": self.eligible,
            "stop_cause": self.stop_cause,
        }
        if self.output_sha256 is not None:
            payload["output_sha256"] = self.output_sha256
        if self.output_bytes is not None:
            payload["output_bytes"] = self.output_bytes
        if self.output_preview is not None:
            payload["output_preview"] = self.output_preview
        if self.output_error is not None:
            payload["output_error"] = self.output_error
        if self.history_error is not None:
            payload["history_error"] = self.history_error
        return payload


@dataclass(frozen=True, slots=True)
class ChildSessionSnapshot:
    """One caller-scoped descendant record, its latest eligible output, and exact terminal diagnostics.

    ``latest_outcome`` is the newest completed publication with a full captured
    output; ``terminal_run`` is the newest committed terminal fact regardless of
    eligibility. They may name different runs, and the terminal diagnostics are
    never attached to the prior eligible output.
    """

    descendant: DescendantNode
    latest_outcome: ChildOutcome | None = None
    terminal_run: TerminalRunCapture | None = None

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
