"""Shared SQLite storage vocabulary: constants, typed errors, records, and canonical JSON.

This module is the bottom of the storage package: the file/schema constants, the typed
storage errors, the closed-set enums, the frozen domain records, and the canonical JSON
encoder. Row shapes/decoders live in `_rows`, connection/file lifecycle in `_database`,
and `store.SqliteStore` composes the domain mixins and re-exports these names for the
rest of the core, so callers keep importing from `storage.store`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from ...sdk.json_values import json_wire_object, json_wire_value, own_json_object
from ...sdk.plugin import StateScope
from ...sdk.services import PluginBudget
from ...sdk.services import StateConflict as StateConflictBase

#: SQLite file-header identity of a voidcode database: b'VOID' plus the schema version.
#: The store checks both PRAGMAs on its single connection before setting any write
#: pragma; foreign or legacy files are refused without a single byte written.
APPLICATION_ID = 0x564F4944
SCHEMA_VERSION = 6

#: SQLite filename owned by the store, under the injected state directory.
#: The engine and the CLI rescue read share this instead of re-spelling it.
DATABASE_FILENAME = "voidcode.db"

#: Maximum canonical (UTF-8 bytes of `canonical_json`) payload for one committed
#: session event. Storage/integrity guard: session events are append-only and read
#: back wholesale, so one unbounded row bloats every read of its session. Measured
#: against the suite: the largest canonical payload the suite commits is 204_835
#: bytes (a synthetic 200 KiB bounds-test prompt; realistic tool projections top out
#: near 52 KiB), giving this bound ~20x headroom while still refusing pathological
#: multi-MB inline rows with a visible `StoreConflict`.
MAX_COMMITTED_EVENT_BYTES = 4 * 1024 * 1024

#: Maximum canonical (UTF-8 bytes of `canonical_json`) payload for one continuation row.
#: Storage/integrity guard: `claim_and_start_continuation` re-embeds the payload verbatim
#: into its CONTINUATION_STARTED event, so an uncapped row would blow past the
#: committed-event bound at claim time. The legit worst case is exact, not sampled: a
#: child follow-up at the max child prompt doubles it (`text` plus `prompt`) to 524_462
#: bytes, while the suite's largest observed payload is 930 bytes — this bound doubles
#: the proven max while keeping every claim event near 1 MiB, far under the 4 MiB ceiling.
MAX_CONTINUATION_PAYLOAD_BYTES = 1024 * 1024

#: Maximum canonical payload for one wakeup row. Storage/integrity guard: the claim path
#: re-embeds the payload verbatim into its WAKEUP_FIRED event. A wakeup carries one
#: future turn's steering text — the same class as a client prompt — so this bound matches
#: that 64 KiB budget, ~1000x over the suite's observed 61-byte max, keeping claim events
#: far under the committed-event ceiling. Oversized schedules fail visibly, never silently.
MAX_WAKEUP_PAYLOAD_BYTES = 64 * 1024

#: Maximum UTF-8 bytes for a child-birth label. Storage/integrity guard: the label rides
#: in the CHILD_RESERVED birth row and is re-embedded into spawn facts and completion
#: notices, so one unbounded tag bloats every read of the lineage. Labels are short human
#: tags (suite max 23 bytes); 1 KiB is 44x headroom while refusing pathological rows.
MAX_CHILD_LABEL_BYTES = 1024


class StorageError(RuntimeError):
    """Base error for core storage failures."""


class StoredValueError(StorageError):
    """A persisted closed-set column holds a value outside its declared set."""

    def __init__(self, message: str, *, field: str, value: object) -> None:
        super().__init__(message)
        self.field = field
        self.value = value


def _int_value(value: object, field_name: str = "value") -> int:
    """Validate and return an int value without boolean coercion."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer, got {value!r}")
    return value


class SessionStatus(StrEnum):
    """Durable session lifecycle state; SQLite stores the wire spelling."""

    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class CheckpointKind(StrEnum):
    """Durable checkpoint position of one run."""

    READY = "ready"
    APPROVAL_WAIT = "approval_wait"
    TERMINAL = "terminal"
    INTERRUPTED = "interrupted"


class ContinuationStatus(StrEnum):
    """Durable continuation/background-action row state."""

    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConfirmationStatus(StrEnum):
    """Durable action-confirmation decision state."""

    PENDING = "pending"
    ALLOWED = "allowed"
    DENIED = "denied"
    STALE = "stale"


class ForeignDatabaseError(StorageError):
    """Raised when an existing database is foreign, empty, legacy, or corrupt."""


class StoreConflict(StorageError):
    """Raised when an optimistic concurrency check fails or state is stale."""


class SequenceConflict(StoreConflict):
    """The session advanced between the caller's snapshot and its atomic CAS.

    The transaction committed nothing, so retrying the same payload after reconciling to the
    fresh tip is safe; callers must never replay external work on this signal.
    """


class StateVersionConflict(StoreConflict, StateConflictBase):
    """One owner-state row changed before this transaction could replace it.

    Nothing committed. A caller that can re-read and re-apply its pure state mutation may
    retry the whole callback; re-submitting the same staged payload cannot succeed because
    its expected version is now stale.
    """


class RootBudgetExhausted(StoreConflict):
    """The durable root action budget refused this reservation."""

    def __init__(self, root_id: str, budget: int, reserved: int) -> None:
        super().__init__(f"root action budget exhausted for {root_id!r}")
        self.root_id = root_id
        self.budget = budget
        self.reserved = reserved


class AskAnswerConflict(StoreConflict):
    """An ask already has a terminal answer event; adoption must happen exactly once."""

    def __init__(self, session_id: str, request_id: str) -> None:
        super().__init__(f"ask {request_id!r} in session {session_id!r} already has a terminal answer")
        self.session_id = session_id
        self.request_id = request_id


class FencedSubtree(StoreConflict):
    """A child registration or follow-up enqueue landed inside a fenced subtree."""

    def __init__(self, fence: str, session_id: str) -> None:
        super().__init__(f"session {session_id!r} is inside subtree {fence!r} under cancellation")
        self.fence = fence
        self.session_id = session_id


class StaleRevision(ValueError):
    """A client acted on a displayed catalog revision that has since moved."""

    def __init__(self, message: str, expected_revision: str, actual_revision: str) -> None:
        super().__init__(message)
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class InvalidRevision(ValueError):
    """A client action carried an empty or non-string expected-revision binding."""

    def __init__(self, message: str, value: object) -> None:
        super().__init__(message)
        self.value = value


class UnknownSession(StoreConflict):
    """A session id names no known session. Still a StoreConflict: CAS handlers keep working; the transport maps it 404 by type."""

    def __init__(self, message: str, session_id: str) -> None:
        super().__init__(message)
        self.session_id = session_id


class UnknownConfirmation(StoreConflict):
    """A confirmation id is unknown or no longer pending (indistinguishable without an extra read)."""

    def __init__(self, message: str, request_id: str) -> None:
        super().__init__(message)
        self.request_id = request_id


class UnknownContinuation(StoreConflict):
    """A continuation id names no known continuation."""

    def __init__(self, message: str, continuation_id: str) -> None:
        super().__init__(message)
        self.continuation_id = continuation_id


class UnknownWorkspace(ValueError):
    """A workspace path names no existing directory."""

    def __init__(self, message: str, workspace: Path) -> None:
        super().__init__(message)
        self.workspace = workspace


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    workspace: str
    status: SessionStatus
    active_revision_id: str
    run_id: str | None
    turn: int
    last_event_sequence: int
    #: Delegated child lineage; root sessions point at themselves with depth 0.
    root_session_id: str = ""
    parent_session_id: str | None = None
    depth: int = 0
    #: Per-session tool visibility scope; None means the whole admitted catalog.
    tool_allowlist: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class EventDraft:
    event_type: str
    source: str
    actor: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class StoredEvent:
    session_id: str
    sequence: int
    run_id: str
    event_type: str
    source: str
    actor: str
    revision_id: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Checkpoint:
    kind: CheckpointKind
    revision_id: str
    run_id: str
    last_event_sequence: int
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class WaitingAskPark:
    """One waiting session's live ask binding, as the expiry sweep lists it."""

    session_id: str
    run_id: str
    revision_id: str
    ask_sequence: int
    ask_request_id: str
    checkpoint_sequence: int


@dataclass(frozen=True, slots=True)
class ActionConfirmationRecord:
    request_id: str
    session_id: str
    owner: str
    command_name: str
    arguments: Mapping[str, object]
    revision_id: str
    expected_state_version: int | None
    ui_type: str
    ui_data: Mapping[str, object]
    status: ConfirmationStatus
    created_at: int
    decided_at: int | None


#: The conversational role a message, continuation, or wakeup is rendered with.
type MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ContinuationRecord:
    continuation_id: str
    owner: str
    session_id: str
    source: str
    run_id: str
    revision_id: str
    idempotency_key: str | None
    status: ContinuationStatus
    payload: Mapping[str, object]
    created_at: int
    updated_at: int
    root_id: str | None = None
    #: Message role the core renders this continuation with ("system" by default).
    role: MessageRole = "system"


@dataclass(frozen=True, slots=True)
class PendingFollowupRecord:
    """One queued user follow-up awaiting claim at the next run boundary."""

    continuation_id: str
    prompt: str
    revision_id: str
    created_at: int
    max_steps: int | None
    image_count: int


@dataclass(frozen=True, slots=True)
class StagedStateUpdate:
    owner: str
    scope: StateScope
    scope_key: str
    payload: Mapping[str, object]
    expected_version: int
    schema_version: int
    revision_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class StagedContinuationDraft:
    continuation_id: str
    owner: str
    source: str
    payload: Mapping[str, object]
    idempotency_key: str | None = None
    ordinal: int = 0
    root_id: str | None = None
    role: MessageRole = "system"

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class WakeupRecord:
    """One durable wakeup: the owner's future turn, claimed atomically with its run."""

    wakeup_id: str
    owner: str
    workspace: str
    session_id: str
    payload: Mapping[str, object]
    root_id: str
    due_at: int
    role: MessageRole
    status: str
    claim_run_id: str | None = None

    root_action_budget: int | None = None


@dataclass(frozen=True, slots=True)
class WakeupSkip:
    """One due wakeup a claim scan could not start, with the durable reason it was left alone."""

    wakeup_id: str
    session_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class StagedWakeup:
    """A wakeup scheduled atomically with the callback that scheduled it."""

    wakeup_id: str
    owner: str
    session_id: str
    payload: Mapping[str, object]
    due_at: int
    role: MessageRole = "system"

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


#: `StagedContinuationDraft.source` value routing one staged draft to the background
#: actions table at commit. Stored rows are namespaced by table; this marker lives
#: only on the staged draft, never in a stored row.
BACKGROUND_ACTION_SOURCE = "@core/background"


@dataclass(frozen=True, slots=True)
class StagedWakeupCancel:
    wakeup_id: str
    owner: str


@dataclass(frozen=True, slots=True)
class StagedConfirmation:
    """A pending action confirmation created atomically with the event that announces it."""

    request_id: str
    owner: str
    command_name: str
    arguments: Mapping[str, object]
    ui_type: str
    ui_data: Mapping[str, object]
    expected_state_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", own_json_object(self.arguments))
        object.__setattr__(self, "ui_data", own_json_object(self.ui_data))


@dataclass(frozen=True, slots=True)
class StagedCancellation:
    owner: str
    continuation_id: str | None = None
    ordinal: int = 0
    only_if_unstarted: bool = False


@dataclass(frozen=True, slots=True)
class StagedBudgetUpdate:
    owner: str
    action: Literal["set", "clear"]
    budget: PluginBudget | None = None


@dataclass(frozen=True, slots=True)
class ObserverReceiptRecord:
    session_id: str
    event_sequence: int
    owner: str
    expected_run_id: str | None = None
    require_interruption_source: bool = False


@dataclass(frozen=True, slots=True)
class PluginStateRow:
    """One durable owner state row plus its core-owned version metadata."""

    owner: str
    scope: StateScope
    scope_key: str
    payload: Mapping[str, object]
    version: int
    schema_version: int
    revision_id: str


@dataclass(frozen=True, slots=True)
class WindowProjectionRecord:
    """One verified projection: the committed prefix it replaces and its measured accounting."""

    session_id: str
    owner: str
    covered_from_sequence: int
    covered_to_sequence: int
    replacement: str
    covered_bytes: int
    dropped_bytes: int
    created_at: int


def canonical_json(value: Mapping[str, object]) -> str:
    """Produce deterministic, sorted, compact UTF-8 JSON without NaN."""
    return json.dumps(
        json_wire_object(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_json_value(value: object) -> str:
    """Canonical JSON for one pre-validated JSON value (a staged background action part)."""
    return json.dumps(json_wire_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
