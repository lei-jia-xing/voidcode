"""Shared SQLite storage vocabulary: file identity, typed records, and row decoders.

This module is the bottom of the storage package: constants and errors, the frozen
domain records, one `NamedTuple` per SELECT shape with its decoder, and the connection
lifecycle base the domain mixins share. `store.SqliteStore` composes the domain mixins
and re-exports these names for the rest of the core, so callers keep importing from
`storage.store` exactly as before.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, NamedTuple

from ...sdk.json_values import json_wire_object, json_wire_value, own_json_object
from ...sdk.plugin import StateScope
from ...sdk.services import PluginBudget, SessionInfo
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


#: One typed tuple per SELECT shape, plus one decoder that turns it into the domain
#: record. Column order is the SQL contract: it is written positionally exactly once,
#: where the row crosses the SQLite boundary, and every later access is a field read.
#: Fields the schema constrains (STRICT tables, NOT NULL, CHECK) are trusted as stored;
#: no per-field `isinstance` fallback re-checks what the database already enforced.
#:
#: Payload read-back contract (one deliberate freeze, not a redundant copy): every
#: decoded record payload is normalized once through `own_json_object`, so JSON arrays
#: become tuples and objects become read-only mappings at the storage boundary. This is
#: observable: plugin observers stage `asdict(fact)` from SDK fact dataclasses whose
#: sequence fields are tuples, and `test_sdk_facts` requires the stored observation to
#: compare equal to the live fact after read-back. Without the normalization a JSON
#: array decodes as a list and `tuple != list`. `SessionInfo.pending_approval` is not
#: frozen here because the SDK `SessionInfo.__post_init__` owns that snapshot.
class _SessionColumns(NamedTuple):
    """The eleven `sessions` columns every session read selects."""

    session_id: str
    workspace: str
    status: str
    active_revision_id: str
    run_id: str | None
    turn: int
    last_event_sequence: int
    parent_session_id: str | None
    root_session_id: str
    depth: int
    tool_allowlist_json: str | None


class _SessionInfoTailColumns(NamedTuple):
    """The checkpoint/root-budget/owner-budget joins appended by `session_info`."""

    approval_json: str | None
    root_budget: int | None
    root_reserved: int | None
    budget_owner: str | None
    budget_since_sequence: int | None
    budget_max_input_tokens: int | None
    budget_max_output_tokens: int | None
    budget_max_total_tokens: int | None
    budget_max_cost_usd: float | None
    budget_max_elapsed_seconds: float | None


class _EventColumns(NamedTuple):
    """One `session_events` row."""

    session_id: str
    sequence: int
    run_id: str
    event_type: str
    source: str
    actor: str
    revision_id: str
    payload_json: str


class _WakeupColumns(NamedTuple):
    """One `wakeups` row."""

    wakeup_id: str
    owner: str
    workspace: str
    session_id: str
    payload_json: str
    root_id: str
    root_action_budget: int | None
    role: str
    due_at: int
    status: str
    claim_run_id: str | None


class _ProjectionColumns(NamedTuple):
    """One `window_projections` row."""

    session_id: str
    owner: str
    covered_from_sequence: int
    covered_to_sequence: int
    replacement: str
    covered_bytes: int
    dropped_bytes: int
    created_at: int


class _CheckpointColumns(NamedTuple):
    """One `checkpoints` row."""

    kind: str
    revision_id: str
    run_id: str
    last_event_sequence: int
    payload_json: str


class _CheckpointGuardColumns(NamedTuple):
    """The claim guard columns `claim_and_start_continuation` reads for a checkpoint."""

    kind: str
    run_id: str
    payload_json: str


class _ContinuationColumns(NamedTuple):
    """One `continuations` row."""

    continuation_id: str
    owner: str
    session_id: str
    source: str
    run_id: str
    revision_id: str
    idempotency_key: str | None
    root_id: str | None
    status: str
    payload_json: str
    created_at: int
    updated_at: int
    role: str


class _BackgroundActionColumns(NamedTuple):
    """One `background_actions` row, including the claim/settle columns drivers read."""

    action_id: str
    owner: str
    session_id: str
    revision_id: str
    idempotency_key: str | None
    root_id: str
    root_action_budget: int | None
    status: str
    payload_json: str
    authority_json: str | None
    claim_json: str | None
    error: str | None
    created_at: int
    updated_at: int


class _PluginStateColumns(NamedTuple):
    """One `plugin_state` row."""

    owner: str
    scope: str
    scope_key: str
    payload_json: str
    version: int
    schema_version: int
    revision_id: str


class _ConfirmationColumns(NamedTuple):
    """One `action_confirmations` row."""

    request_id: str
    session_id: str
    owner: str
    command_name: str
    arguments_json: str
    revision_id: str
    expected_state_version: int | None
    ui_type: str
    ui_data_json: str
    status: str
    created_at: int
    decided_at: int | None


class _BudgetColumns(NamedTuple):
    """One `session_budgets` row."""

    owner: str
    since_sequence: int
    max_input_tokens: int | None
    max_output_tokens: int | None
    max_total_tokens: int | None
    max_cost_usd: float | None
    max_elapsed_seconds: float | None


class _WaitingAskColumns(NamedTuple):
    """One waiting session's checkpoint ask binding."""

    session_id: str
    run_id: str
    revision_id: str
    ask_sequence: int
    ask_request_id: str
    checkpoint_sequence: int


class _AdoptSessionColumns(NamedTuple):
    """The CAS columns `adopt_revision` reads for its acting session."""

    workspace: str
    status: str
    active_revision_id: str
    run_id: str | None
    turn: int
    last_event_sequence: int


class _ChildClaimGuardColumns(NamedTuple):
    """The eligibility columns `claim_child_messages` reads for its running session."""

    status: str
    active_revision_id: str
    run_id: str | None
    last_event_sequence: int


class _RootBudgetColumns(NamedTuple):
    """One `root_budgets` row's counters."""

    budget: int
    reserved: int


def _decode_tool_allowlist(raw: str | None) -> tuple[str, ...] | None:
    """Decode a session tool scope, refusing anything that is not a list of names."""
    if raw is None:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise StorageError("session tool scope is malformed")
    return tuple(parsed)


def _decode_closed_enum[E: StrEnum](enum_type: type[E], value: object, *, field: str, record: str) -> E:
    """Decode one persisted closed-set value, or fail with the typed storage error."""
    try:
        return enum_type(value)
    except ValueError as exc:
        raise StoredValueError(f"{record} has unknown {field} {value!r}", field=field, value=value) from exc


def _decode_session(row: Sequence[Any]) -> SessionRecord:
    """Map one eleven-column sessions row onto its immutable record."""
    cols = _SessionColumns(*row)
    return SessionRecord(
        session_id=cols.session_id,
        workspace=cols.workspace,
        status=_decode_closed_enum(SessionStatus, cols.status, field="status", record=f"session {cols.session_id!r}"),
        active_revision_id=cols.active_revision_id,
        run_id=cols.run_id,
        turn=cols.turn,
        last_event_sequence=cols.last_event_sequence,
        root_session_id=cols.root_session_id,
        parent_session_id=cols.parent_session_id,
        depth=cols.depth,
        tool_allowlist=_decode_tool_allowlist(cols.tool_allowlist_json),
    )


def _decode_session_info(row: Sequence[Any]) -> SessionInfo:
    """Map the session/checkpoint/budget join row onto the SDK's neutral facts."""
    session = _decode_session(row[:11])
    tail = _SessionInfoTailColumns(*row[11:])
    budget = (
        None
        if tail.budget_owner is None
        else PluginBudget(
            owner=tail.budget_owner,
            since_sequence=0 if tail.budget_since_sequence is None else tail.budget_since_sequence,
            max_input_tokens=tail.budget_max_input_tokens,
            max_output_tokens=tail.budget_max_output_tokens,
            max_total_tokens=tail.budget_max_total_tokens,
            max_cost_usd=tail.budget_max_cost_usd,
            max_elapsed_seconds=tail.budget_max_elapsed_seconds,
        )
    )
    return SessionInfo(
        session_id=session.session_id,
        status=session.status.value,
        run_id=session.run_id,
        revision_id=session.active_revision_id,
        parent_session_id=session.parent_session_id,
        root_session_id=session.root_session_id,
        depth=session.depth,
        turn=session.turn,
        last_event_sequence=session.last_event_sequence,
        tool_allowlist=session.tool_allowlist,
        pending_approval=None if tail.approval_json is None else json.loads(tail.approval_json),
        chain_budget=tail.root_budget,
        chain_reserved=tail.root_reserved,
        budget=budget,
    )


def _decode_event(row: Sequence[Any]) -> StoredEvent:
    """Map one session_events row onto its immutable record (payload decoded once)."""
    cols = _EventColumns(*row)
    return StoredEvent(
        session_id=cols.session_id,
        sequence=cols.sequence,
        run_id=cols.run_id,
        event_type=cols.event_type,
        source=cols.source,
        actor=cols.actor,
        revision_id=cols.revision_id,
        payload=own_json_object(json.loads(cols.payload_json)),
    )


def _decode_role(value: str) -> MessageRole:
    if value in ("system", "user", "assistant", "tool"):
        return value
    return "system"


def _decode_wakeup(row: Sequence[Any]) -> WakeupRecord:
    """Map one wakeups row; the stored payload must be a JSON object written by this core."""
    cols = _WakeupColumns(*row)
    payload = json.loads(cols.payload_json)
    if not isinstance(payload, Mapping):
        raise StoreConflict(f"wakeup {cols.wakeup_id!r} has a malformed payload")
    return WakeupRecord(
        wakeup_id=cols.wakeup_id,
        owner=cols.owner,
        workspace=cols.workspace,
        session_id=cols.session_id,
        payload=own_json_object(payload),
        root_id=cols.root_id,
        role=_decode_role(cols.role),
        due_at=cols.due_at,
        status=cols.status,
        claim_run_id=cols.claim_run_id,
        root_action_budget=cols.root_action_budget,
    )


def _decode_projection(row: Sequence[Any]) -> WindowProjectionRecord:
    """Map one window_projections row."""
    cols = _ProjectionColumns(*row)
    return WindowProjectionRecord(
        session_id=cols.session_id,
        owner=cols.owner,
        covered_from_sequence=cols.covered_from_sequence,
        covered_to_sequence=cols.covered_to_sequence,
        replacement=cols.replacement,
        covered_bytes=cols.covered_bytes,
        dropped_bytes=cols.dropped_bytes,
        created_at=cols.created_at,
    )


def _decode_checkpoint(row: Sequence[Any]) -> Checkpoint:
    """Map one checkpoints row onto its immutable record."""
    cols = _CheckpointColumns(*row)
    return Checkpoint(
        kind=_decode_closed_enum(CheckpointKind, cols.kind, field="kind", record="checkpoint"),
        revision_id=cols.revision_id,
        run_id=cols.run_id,
        last_event_sequence=cols.last_event_sequence,
        payload=own_json_object(json.loads(cols.payload_json)),
    )


def _decode_continuation(row: Sequence[Any]) -> ContinuationRecord:
    """Map one continuations row; `payload_json` is canonical JSON written by this core."""
    cols = _ContinuationColumns(*row)
    return ContinuationRecord(
        continuation_id=cols.continuation_id,
        owner=cols.owner,
        session_id=cols.session_id,
        source=cols.source,
        run_id=cols.run_id,
        revision_id=cols.revision_id,
        idempotency_key=cols.idempotency_key,
        status=_decode_closed_enum(ContinuationStatus, cols.status, field="status", record=f"continuation {cols.continuation_id!r}"),
        payload=own_json_object(json.loads(cols.payload_json)),
        created_at=cols.created_at,
        updated_at=cols.updated_at,
        root_id=cols.root_id,
        role=_decode_role(cols.role),
    )


def _decode_background_action(row: Sequence[Any], *, status: ContinuationStatus | None = None) -> ContinuationRecord:
    """Project one background_actions row onto the caller-visible continuation shape.

    Columns are the durable truth; the payload dict only reassembles the keys drivers
    and status readers expect. The stored action passes through verbatim.
    """
    cols = _BackgroundActionColumns(*row)
    payload: dict[str, object] = {
        "action": json.loads(cols.payload_json),
        "root_action_budget": cols.root_action_budget,
        "tool_wait_authority": None if cols.authority_json is None else json.loads(cols.authority_json),
    }
    if cols.error is not None:
        payload["error"] = cols.error
    return ContinuationRecord(
        continuation_id=cols.action_id,
        owner=cols.owner,
        session_id=cols.session_id,
        source="",
        run_id="",
        revision_id=cols.revision_id,
        idempotency_key=cols.idempotency_key,
        status=(
            status
            if status is not None
            else _decode_closed_enum(ContinuationStatus, cols.status, field="status", record=f"background action {cols.action_id!r}")
        ),
        payload=payload,
        created_at=cols.created_at,
        updated_at=cols.updated_at,
        root_id=cols.root_id,
    )


def _decode_plugin_state(row: Sequence[Any]) -> PluginStateRow:
    """Map one plugin_state row (payload already validated by the write that stored it)."""
    cols = _PluginStateColumns(*row)
    scope: StateScope = "session" if cols.scope == "session" else "workspace"
    return PluginStateRow(
        owner=cols.owner,
        scope=scope,
        scope_key=cols.scope_key,
        payload=own_json_object(json.loads(cols.payload_json)),
        version=cols.version,
        schema_version=cols.schema_version,
        revision_id=cols.revision_id,
    )


def _decode_confirmation(row: Sequence[Any]) -> ActionConfirmationRecord:
    """Map one action_confirmations row."""
    cols = _ConfirmationColumns(*row)
    return ActionConfirmationRecord(
        request_id=cols.request_id,
        session_id=cols.session_id,
        owner=cols.owner,
        command_name=cols.command_name,
        arguments=own_json_object(json.loads(cols.arguments_json)),
        revision_id=cols.revision_id,
        expected_state_version=cols.expected_state_version,
        ui_type=cols.ui_type,
        ui_data=own_json_object(json.loads(cols.ui_data_json)),
        status=_decode_closed_enum(ConfirmationStatus, cols.status, field="status", record=f"confirmation {cols.request_id!r}"),
        created_at=cols.created_at,
        decided_at=cols.decided_at,
    )


def _decode_budget(row: Sequence[Any]) -> PluginBudget:
    """Map one session_budgets row."""
    cols = _BudgetColumns(*row)
    return PluginBudget(
        owner=cols.owner,
        since_sequence=cols.since_sequence,
        max_input_tokens=cols.max_input_tokens,
        max_output_tokens=cols.max_output_tokens,
        max_total_tokens=cols.max_total_tokens,
        max_cost_usd=cols.max_cost_usd,
        max_elapsed_seconds=cols.max_elapsed_seconds,
    )


def _stored_root_budget(row: Sequence[Any] | None) -> int | None:
    """The finite root budget a `root_budgets` lookup found, or None when the root is uncapped."""
    if row is None:
        return None
    (budget,) = row
    return budget


def _decode_waiting_ask(row: Sequence[Any]) -> WaitingAskPark:
    """Map one live ask binding, refusing a checkpoint whose binding columns are malformed."""
    cols = _WaitingAskColumns(*row)
    if type(cols.ask_sequence) is not int or cols.ask_sequence < 1 or not cols.ask_request_id:
        raise StoreConflict(f"session {cols.session_id!r} holds a malformed live ask binding")
    return WaitingAskPark(
        session_id=cols.session_id,
        run_id=cols.run_id,
        revision_id=cols.revision_id,
        ask_sequence=cols.ask_sequence,
        ask_request_id=cols.ask_request_id,
        checkpoint_sequence=cols.checkpoint_sequence,
    )


def _continuation_kind(payload: Mapping[str, object]) -> str | None:
    """Derive the indexable child-message kind; non-text kinds stay invisible, never loud.

    Model payloads are arbitrary JSON: only a text kind can ever match the child
    filters, so anything else maps to NULL exactly like a missing key.
    """
    kind = payload.get("kind")
    return kind if isinstance(kind, str) else None


def _checkpoint_index_values(payload: Mapping[str, object]) -> tuple[str | None, int | None, int | None, str | None]:
    """Derive the indexed checkpoint columns from the payload dict; wrong types fail loudly."""
    root_value = payload.get("root_id")
    if root_value is None:
        root_id: str | None = None
    elif isinstance(root_value, str):
        root_id = root_value
    else:
        raise ValueError("checkpoint root_id must be text or null")
    approval_value = payload.get("approval_sequence")
    if approval_value is None:
        approval: int | None = None
    elif type(approval_value) is int:
        approval = approval_value
    else:
        raise ValueError("checkpoint approval_sequence must be an integer or null")
    ask_sequence_value = payload.get("ask_sequence")
    if ask_sequence_value is None:
        ask_sequence: int | None = None
    elif type(ask_sequence_value) is int:
        ask_sequence = ask_sequence_value
    else:
        raise ValueError("checkpoint ask_sequence must be an integer or null")
    ask_request_value = payload.get("ask_request_id")
    if ask_request_value is None:
        ask_request_id: str | None = None
    elif isinstance(ask_request_value, str):
        ask_request_id = ask_request_value
    else:
        raise ValueError("checkpoint ask_request_id must be text or null")
    return (root_id, approval, ask_sequence, ask_request_id)


def _load_schema_sql() -> str:
    return files("voidcode.core.storage").joinpath("schema.sql").read_text(encoding="utf-8")


def _check_regular_nonempty(path: Path) -> None:
    """Refuse paths that are not a usable pre-existing database file."""
    if not path.is_file():
        raise ForeignDatabaseError(f"database path exists but is not a regular file: {path}")
    if path.stat().st_size == 0:
        raise ForeignDatabaseError(f"refusing pre-existing empty database file: {path}")


def _read_file_identity(connection: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """Read the file-header identity of an open database without writing anything."""
    try:
        app_id = connection.execute("PRAGMA application_id").fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as exc:
        raise ForeignDatabaseError(f"unsupported database file: {path}") from exc
    return (int(app_id[0]), int(user_version[0]))


def _check_identity(app_id: int, user_version: int, path: Path) -> None:
    """Refuse any database whose file-header identity is not this build's own."""
    if app_id != APPLICATION_ID or user_version != SCHEMA_VERSION:
        raise ForeignDatabaseError(
            f"database is not a valid voidcode database: unsupported identity in {path}: "
            f"expected ({APPLICATION_ID:#x}, {SCHEMA_VERSION}), got ({app_id:#x}, {user_version})"
        )


def open_read_only(database: Path) -> sqlite3.Connection:
    """Open one core database for inspection, with writes refused by the connection itself.

    Uses a ``mode=ro`` URI plus ``PRAGMA query_only``, after the PRAGMA identity
    check a writer runs. Foreign files raise without a single byte written.
    """
    _check_regular_nonempty(database)
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ForeignDatabaseError(f"unsupported database file: {database}") from exc
    try:
        app_id, user_version = _read_file_identity(connection, database)
        _check_identity(app_id, user_version, database)
    except Exception:
        connection.close()
        raise
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def inspect_existing_workspace(database: Path, workspace: str) -> tuple[str | None, Mapping[str, object] | None, int | None]:
    """Read one workspace's adopted revision without creating or changing anything.

    Returns ``(None, None, None)`` when there is no database, ``(None, None, schema)``
    when the workspace has no adopted revision, ``(revision_id, payload, schema)`` when
    it has one, and raises for a file that is not a core database. Never writes, never sets
    write pragmas. This build opens exactly its own schema; foreign files are refused.
    """
    if not database.exists():
        return (None, None, None)
    probe = open_read_only(database)
    try:
        identity = probe.execute("PRAGMA user_version").fetchone()
        schema = int(identity[0])
        cursor = probe.execute("SELECT active_revision_id FROM workspace_state WHERE workspace = ?", (workspace,))
        row = cursor.fetchone()
        if row is None:
            return (None, None, schema)
        revision_id = str(row["active_revision_id"])
        payload_row = probe.execute("SELECT payload_json FROM revisions WHERE revision_id = ?", (revision_id,)).fetchone()
        if payload_row is None:
            raise ForeignDatabaseError(f"database {database} points at missing revision {revision_id}")
        payload = json.loads(str(payload_row["payload_json"]))
        if not isinstance(payload, dict):
            raise ForeignDatabaseError(f"database {database} stores a malformed revision payload")
        return (revision_id, json_wire_object(payload), schema)
    finally:
        probe.close()


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


class _ConnectionStore:
    """Connection state and lifecycle shared by every domain mixin.

    One `SqliteStore` owns exactly one connection and one re-entrant lock; the mixins
    only ever touch them through these attributes, so the storage package has one
    locking discipline instead of one per domain.
    """

    _lock: threading.RLock
    _connection: sqlite3.Connection
    _closed: bool
    #: Optional in-process cancellation fence (set by DelegationManager).
    _cancel_fence_guard: Callable[[frozenset[str], str | None], str | None] | None

    def _check_open(self) -> None:
        if self._closed:
            raise StorageError("store is closed")
