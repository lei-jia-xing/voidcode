"""SQLite storage implementation for the VoidCode core."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self, cast
from uuid import uuid4

from ...sdk.json_values import json_wire_object, json_wire_value, own_json_object
from ...sdk.services import PluginBudget, SessionInfo
from ..events import CHILD_MESSAGE_OWNER, EventType

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


class ForeignDatabaseError(StorageError):
    """Raised when an existing database is foreign, empty, legacy, or corrupt."""


class StoreConflict(StorageError):
    """Raised when an optimistic concurrency check fails or state is stale."""


class SequenceConflict(StoreConflict):
    """The session advanced between the caller's snapshot and its atomic CAS.

    The transaction committed nothing, so retrying the same payload after reconciling to the
    fresh tip is safe; callers must never replay external work on this signal.
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
    status: str
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class Checkpoint:
    kind: str
    revision_id: str
    run_id: str
    last_event_sequence: int
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


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
    status: str
    created_at: int
    decided_at: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", own_json_object(self.arguments))
        object.__setattr__(self, "ui_data", own_json_object(self.ui_data))


@dataclass(frozen=True, slots=True)
class ContinuationRecord:
    continuation_id: str
    owner: str
    session_id: str
    source: str
    run_id: str
    revision_id: str
    idempotency_key: str | None
    status: str
    payload: Mapping[str, object]
    created_at: int
    updated_at: int
    root_id: str | None = None
    #: Message role the core renders this continuation with ("system" by default).
    role: str = "system"

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class StagedStateUpdate:
    owner: str
    scope: str
    scope_key: str
    payload: Mapping[str, object]
    expected_version: int
    schema_version: int = 1
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
    role: str = "system"

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
    role: str
    status: str
    claim_run_id: str | None = None

    root_action_budget: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class StagedWakeup:
    """A wakeup scheduled atomically with the callback that scheduled it."""

    wakeup_id: str
    owner: str
    session_id: str
    payload: Mapping[str, object]
    due_at: int
    role: str = "system"

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


#: `StagedContinuationDraft.source` value routing one staged draft to the background
#: actions table at commit. Stored rows are namespaced by table; this marker lives
#: only on the staged draft, never in a stored row.
BACKGROUND_ACTION_SOURCE = "@core/background"


def _background_record(row: Sequence[object], *, status: str | None = None) -> ContinuationRecord:
    """Project one background_actions row onto the caller-visible continuation shape.

    Columns are the durable truth; the payload dict only reassembles the keys drivers
    and status readers expect. The stored action passes through verbatim.
    """
    action = json.loads(str(row[8]))
    payload: dict[str, object] = {
        "action": action if isinstance(action, Mapping) else {},
        "root_action_budget": row[6],
        "tool_wait_authority": None if row[9] is None else own_json_object(json.loads(str(row[9]))),
    }
    if row[11] is not None:
        payload["error"] = str(row[11])
    return ContinuationRecord(
        continuation_id=str(row[0]),
        owner=str(row[1]),
        session_id=str(row[2]),
        source="",
        run_id="",
        revision_id=str(row[3]),
        idempotency_key=None if row[4] is None else str(row[4]),
        status=status if status is not None else str(row[7]),
        payload=payload,
        created_at=row[12] if isinstance(row[12], int) else 0,
        updated_at=row[13] if isinstance(row[13], int) else 0,
        root_id=str(row[5]),
    )


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
    scope: str
    scope_key: str
    payload: Mapping[str, object]
    version: int
    schema_version: int
    revision_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))


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


def _allowlist_from_json(raw: object) -> tuple[str, ...] | None:
    """Decode a session tool scope, refusing anything that is not a list of names."""
    if not isinstance(raw, str) or not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise StorageError("session tool scope is malformed")
    return tuple(parsed)


def _session_record(row: tuple[object, ...]) -> SessionRecord:
    """Map one sessions row onto its immutable record, including delegation lineage."""
    allowlist = _allowlist_from_json(row[10])
    root = row[8] if isinstance(row[8], str) and row[8] else str(row[0])
    parent = row[7] if isinstance(row[7], str) and row[7] else None
    depth = row[9] if isinstance(row[9], int) else 0
    return SessionRecord(
        session_id=str(row[0]),
        workspace=str(row[1]),
        status=str(row[2]),
        active_revision_id=str(row[3]),
        run_id=None if row[4] is None else str(row[4]),
        turn=row[5] if isinstance(row[5], int) else 0,
        last_event_sequence=row[6] if isinstance(row[6], int) else 0,
        root_session_id=root,
        parent_session_id=parent,
        depth=depth,
        tool_allowlist=allowlist,
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


class SqliteStore:
    """Concrete SQLite store for core sessions, revisions, events, and checkpoints."""

    def __init__(self, path: Path | str) -> None:
        self._lock = threading.RLock()
        #: Optional in-process cancellation fence consulted inside store
        #: transactions (set by DelegationManager). The guard takes a
        #: caller-captured ancestor chain plus an optional child id, reads only
        #: in-memory fence state under its own lock, and never calls back into
        #: the store, so lock order stays lifecycle -> store and never inverts.
        self._cancel_fence_guard: Callable[[frozenset[str], str | None], str | None] | None = None
        self._closed = False
        if isinstance(path, str):
            path = Path(path)
        self._path = path

        is_memory = str(path) == ":memory:"
        is_new = False
        resolved_path: Path | None = None

        if not is_memory:
            resolved_path = path.resolve()
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(resolved_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                _check_regular_nonempty(resolved_path)
            else:
                os.close(descriptor)
                is_new = True
        else:
            is_new = True

        self._connection = sqlite3.connect(
            str(resolved_path) if resolved_path is not None else ":memory:",
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            if not is_new:
                assert resolved_path is not None
                app_id, user_version = _read_file_identity(self._connection, resolved_path)
                _check_identity(app_id, user_version, resolved_path)
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if is_new:
                self._connection.executescript(_load_schema_sql())
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except Exception:
            self._connection.close()
            self._closed = True
            if is_new and resolved_path is not None:
                resolved_path.unlink(missing_ok=True)
            raise

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise StorageError("store is closed")

    def put_revision(self, payload: Mapping[str, object], *, revision_id: str | None = None) -> str:
        """Persist one generation payload under an opaque id and return that id.

        A generation id names a composition/config snapshot, not source bytes; callers that
        already own an id (an admission, a pinned approval target) pass it so the same row is
        addressed before and after the host finalized its runtime metadata.
        """
        canonical_text = canonical_json(payload)
        revision_id = uuid4().hex if revision_id is None else revision_id
        if not revision_id:
            raise ValueError("revision id must be a non-empty string")
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            existing = self._connection.execute(
                "SELECT payload_json FROM revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != canonical_text:
                    raise ValueError(f"revision {revision_id} already exists with other content")
                return revision_id
            self._connection.execute(
                "INSERT INTO revisions (revision_id, created_at, payload_json) VALUES (?, ?, ?)",
                (revision_id, now, canonical_text),
            )
        return revision_id

    def revision(self, revision_id: str) -> Mapping[str, object]:
        """Fetch the payload of a revision by ID, returning an immutable mapping snapshot."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT payload_json FROM revisions WHERE revision_id = ?",
                (revision_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"unknown revision: {revision_id}")
            return own_json_object(json.loads(row[0]))

    def create_session(self, session_id: str, workspace: str, revision_id: str) -> SessionRecord:
        """Create a new session pinned to the workspace's recorded revision.

        The insert takes the revision from the workspace pointer in the SAME statement and
        requires it to still be the caller's expected revision, so a session created while a
        capability activation commits can never be pinned to the generation that just moved.
        """
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                INSERT INTO sessions (
                    session_id, workspace, status, active_revision_id, run_id, turn, created_at, updated_at, last_event_sequence,
                    parent_session_id, root_session_id, depth, tool_allowlist_json
                )
                SELECT ?, ?, 'idle', w.active_revision_id, NULL, 0, ?, ?, 0, NULL, ?, 0, NULL
                FROM workspace_state w
                WHERE w.workspace = ? AND w.active_revision_id = ?
                """,
                (session_id, workspace, now, now, session_id, workspace, revision_id),
            )
            if cursor.rowcount == 0:
                raise StoreConflict(f"workspace revision moved away from {revision_id!r}; retry against the current revision")
            return SessionRecord(
                session_id=session_id,
                workspace=workspace,
                status="idle",
                active_revision_id=revision_id,
                run_id=None,
                turn=0,
                last_event_sequence=0,
                root_session_id=session_id,
            )

    def create_child_session(
        self,
        session_id: str,
        workspace: str,
        revision_id: str,
        *,
        parent_session_id: str,
        root_session_id: str,
        depth: int,
        tool_allowlist: tuple[str, ...] | None = None,
        max_root_children: int | None = None,
        birth: Mapping[str, object] | None = None,
        caller_chain: frozenset[str] | None = None,
        admission_guard: Callable[[], bool] | None = None,
    ) -> SessionRecord:
        """Create a delegated child with its immutable action identity in the birth event."""
        if not isinstance(birth, Mapping):
            raise ValueError("child reservation requires an atomic birth record")
        root_id = birth.get("root_id")
        if not isinstance(root_id, str) or not root_id:
            raise ValueError("child birth record requires a captured root action identity")
        birth_label = birth.get("label")
        if birth_label is not None and (not isinstance(birth_label, str) or len(birth_label.encode("utf-8")) > MAX_CHILD_LABEL_BYTES):
            raise ValueError(f"child birth label must be UTF-8 text within {MAX_CHILD_LABEL_BYTES // 1024} KiB")
        now = int(time.time() * 1000)
        allowlist_json = None if tool_allowlist is None else json.dumps(list(tool_allowlist))
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                if max_root_children is not None or birth is not None:
                    self._connection.execute("BEGIN IMMEDIATE")
                    in_transaction = True
                guard = self._cancel_fence_guard
                if guard is not None and caller_chain is not None:
                    fenced = guard(caller_chain, None)
                    if fenced is not None:
                        raise FencedSubtree(fenced, parent_session_id)
                if admission_guard is not None and not admission_guard():
                    raise PermissionError("calling invocation is no longer active")
                if max_root_children is not None:
                    row = self._connection.execute(
                        "SELECT COUNT(*) FROM sessions WHERE root_session_id = ? AND parent_session_id IS NOT NULL",
                        (root_session_id,),
                    ).fetchone()
                    if row is not None and int(row[0]) >= max_root_children:
                        raise ValueError("delegation spawn budget exhausted for this chain")
                cursor = self._connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, workspace, status, active_revision_id, run_id, turn, created_at, updated_at, last_event_sequence,
                        parent_session_id, root_session_id, depth, tool_allowlist_json
                    )
                    SELECT ?, ?, 'idle', w.active_revision_id, NULL, 0, ?, ?, 0, ?, ?, ?, ?
                    FROM workspace_state w
                    WHERE w.workspace = ? AND w.active_revision_id = ?
                    """,
                    (session_id, workspace, now, now, parent_session_id, root_session_id, depth, allowlist_json, workspace, revision_id),
                )
                if cursor.rowcount == 0:
                    raise StoreConflict(f"workspace revision moved away from {revision_id!r}; retry against the current revision")
                if birth is not None:
                    birth_payload = dict(own_json_object(birth))
                    birth_payload["eligible_owners"] = []
                    birth_payload_json = canonical_json(birth_payload)
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                        ) VALUES (?, 1, '', ?, 'kernel', 'plugin', ?, ?, ?)
                        """,
                        (session_id, EventType.CHILD_RESERVED, revision_id, birth_payload_json, len(birth_payload_json.encode("utf-8"))),
                    )
                    self._connection.execute(
                        "UPDATE sessions SET last_event_sequence = 1, updated_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
                    if birth_payload.get("readonly") is True:
                        # The birth stance lands in the same transaction as the row: no window
                        # exists where a read-only child runs without its kernel overlay.
                        self._connection.execute(
                            """
                            INSERT INTO session_overlays (session_id, owner, tier, created_at, updated_at)
                            VALUES (?, 'kernel', 'read-only', ?, ?)
                            ON CONFLICT (session_id, owner) DO NOTHING
                            """,
                            (session_id, now, now),
                        )
                if in_transaction:
                    self._connection.execute("COMMIT")
                    in_transaction = False
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            return SessionRecord(
                session_id=session_id,
                workspace=workspace,
                status="idle",
                active_revision_id=revision_id,
                run_id=None,
                turn=0,
                last_event_sequence=1 if birth is not None else 0,
                root_session_id=root_session_id,
                parent_session_id=parent_session_id,
                depth=depth,
                tool_allowlist=tool_allowlist,
            )

    def set_session_overlay(self, session_id: str, owner: str, tier: str = "read-only") -> None:
        """Add (or keep) one owner's restrictive overlay on a session; tiers can only tighten."""
        if tier != "read-only":
            raise ValueError(f"unsupported overlay tier: {tier!r}")
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            self.session(session_id)
            self._connection.execute(
                """
                INSERT INTO session_overlays (session_id, owner, tier, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (session_id, owner) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (session_id, owner, tier, now, now),
            )

    def clear_session_overlay(self, session_id: str, owner: str) -> bool:
        """Revoke exactly this owner's overlay; other owners and the core policy stay."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "DELETE FROM session_overlays WHERE session_id = ? AND owner = ?",
                (session_id, owner),
            )
            return cursor.rowcount > 0

    def session_overlays(self, session_id: str) -> tuple[str, ...]:
        """Tiers currently restricting this session (one row per owner)."""
        with self._lock:
            self._check_open()
            rows = self._connection.execute(
                "SELECT tier FROM session_overlays WHERE session_id = ? ORDER BY owner ASC",
                (session_id,),
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

    def set_window_projection(
        self,
        session_id: str,
        owner: str,
        *,
        covered_from_sequence: int,
        covered_to_sequence: int,
        replacement: str,
        covered_bytes: int,
        dropped_bytes: int,
    ) -> None:
        """Record the session's verified window projection (one per session, latest wins)."""
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            self.session(session_id)
            self._connection.execute(
                """
                INSERT INTO window_projections (
                    session_id, owner, covered_from_sequence, covered_to_sequence,
                    replacement, covered_bytes, dropped_bytes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (session_id) DO UPDATE SET
                    owner = excluded.owner,
                    covered_from_sequence = excluded.covered_from_sequence,
                    covered_to_sequence = excluded.covered_to_sequence,
                    replacement = excluded.replacement,
                    covered_bytes = excluded.covered_bytes,
                    dropped_bytes = excluded.dropped_bytes,
                    created_at = excluded.created_at
                """,
                (
                    session_id,
                    owner,
                    covered_from_sequence,
                    covered_to_sequence,
                    replacement,
                    covered_bytes,
                    dropped_bytes,
                    now,
                ),
            )

    def window_projection(self, session_id: str) -> WindowProjectionRecord | None:
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT session_id, owner, covered_from_sequence, covered_to_sequence,
                       replacement, covered_bytes, dropped_bytes, created_at
                FROM window_projections WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return WindowProjectionRecord(
            session_id=str(row[0]),
            owner=str(row[1]),
            covered_from_sequence=int(row[2]),
            covered_to_sequence=int(row[3]),
            replacement=str(row[4]),
            covered_bytes=int(row[5]),
            dropped_bytes=int(row[6]),
            created_at=int(row[7]),
        )

    def claim_and_start_wakeup(
        self,
        workspace: str,
        now: int,
        *,
        run_id: str,
        logical_turn: str,
        revision_id: str,
        eligible_owners: Sequence[str],
    ) -> tuple[WakeupRecord, SessionRecord] | None:
        """Claim a due wakeup and start it under the root identity captured when scheduled."""
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                cursor = self._connection.execute(
                    """
                    SELECT wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget, role, due_at, status, claim_run_id
                    FROM wakeups
                    WHERE workspace = ? AND status = 'pending' AND due_at <= ?
                    ORDER BY due_at ASC, created_at ASC
                    LIMIT 1
                    """,
                    (workspace, now),
                )
                row = cursor.fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return None
                stored = json.loads(row[4])
                if not isinstance(stored, Mapping):
                    raise StoreConflict(f"wakeup {str(row[0])!r} has a malformed payload")
                wakeup = WakeupRecord(
                    wakeup_id=str(row[0]),
                    owner=str(row[1]),
                    workspace=str(row[2]),
                    session_id=str(row[3]),
                    payload=own_json_object(stored),
                    root_id=str(row[5]),
                    role=str(row[7]),
                    due_at=int(row[8]),
                    status=str(row[9]),
                    claim_run_id=str(row[10]) if row[10] is not None else None,
                    root_action_budget=None if row[6] is None else int(row[6]),
                )
                claimed = self._connection.execute(
                    """
                    UPDATE wakeups SET status = 'claimed', claim_run_id = ?, updated_at = ?
                    WHERE wakeup_id = ? AND status = 'pending'
                    """,
                    (run_id, now, wakeup.wakeup_id),
                )
                if claimed.rowcount == 0:
                    raise StoreConflict(f"wakeup {wakeup.wakeup_id!r} was claimed concurrently")
                session_row = self._connection.execute(
                    """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                           parent_session_id, root_session_id, depth, tool_allowlist_json
                    FROM sessions WHERE session_id = ?
                    """,
                    (wakeup.session_id,),
                ).fetchone()
                if session_row is None:
                    raise UnknownSession(f"wakeup {wakeup.wakeup_id!r} names an unknown session", wakeup.session_id)
                session = _session_record(session_row)
                if session.active_revision_id != revision_id:
                    raise StoreConflict("wakeup session belongs to another capability revision")
                if session.status in ("running", "waiting"):
                    raise StoreConflict(f"wakeup session {session.session_id!r} is busy ({session.status})")
                budget_row = self._connection.execute(
                    "SELECT budget FROM root_budgets WHERE root_id = ?",
                    (wakeup.root_id,),
                ).fetchone()
                if wakeup.root_action_budget is None and budget_row is not None:
                    raise StoreConflict("wakeup captured an uncapped root with a finite budget row")
                if wakeup.root_action_budget is not None and (budget_row is None or int(budget_row[0]) < wakeup.root_action_budget):
                    raise StoreConflict("wakeup lost its captured finite root authorization")
                initial_root_budget = wakeup.root_action_budget
                new_seq = session.last_event_sequence + 1
                fired_payload = canonical_json(
                    {
                        "eligible_owners": list(eligible_owners),
                        "wakeup_id": wakeup.wakeup_id,
                        "owner": wakeup.owner,
                        "payload": json_wire_object(wakeup.payload),
                        "role": wakeup.role,
                        "logical_turn": logical_turn,
                    }
                )
                self._connection.execute(
                    """
                    INSERT INTO session_events (
                        session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                    ) VALUES (?, ?, ?, ?, ?, 'plugin', ?, ?, ?)
                    """,
                    (
                        session.session_id,
                        new_seq,
                        run_id,
                        EventType.WAKEUP_FIRED,
                        f"plugin.{wakeup.owner}",
                        revision_id,
                        fired_payload,
                        len(fired_payload.encode("utf-8")),
                    ),
                )
                wakeup_checkpoint = {
                    "batch_sequence": None,
                    "next_call": 0,
                    "intent_sequence": None,
                    "approval_sequence": None,
                    "deferred_sequence": None,
                    "deferred_depth": 0,
                    "deferred_only": False,
                    "max_steps": None,
                    "step": 0,
                    "abandon_batch": False,
                    "continuation_id": None,
                    "logical_turn": logical_turn,
                    "stop_cause": None,
                    "initial_root_budget": initial_root_budget,
                    "root_id": wakeup.root_id,
                    "resume_intent": "",
                }
                checkpoint_payload = canonical_json(wakeup_checkpoint)
                checkpoint_root_id, checkpoint_approval, checkpoint_ask_seq, checkpoint_ask_req = _checkpoint_index_values(wakeup_checkpoint)
                self._connection.execute(
                    """
                    INSERT INTO checkpoints (
                        session_id, kind, revision_id, run_id, last_event_sequence, payload_json,
                        root_id, approval_sequence, ask_sequence, ask_request_id, updated_at
                    ) VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (session_id) DO UPDATE SET
                        kind = 'ready',
                        revision_id = excluded.revision_id,
                        run_id = excluded.run_id,
                        last_event_sequence = excluded.last_event_sequence,
                        payload_json = excluded.payload_json,
                        root_id = excluded.root_id,
                        approval_sequence = excluded.approval_sequence,
                        ask_sequence = excluded.ask_sequence,
                        ask_request_id = excluded.ask_request_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session.session_id,
                        revision_id,
                        run_id,
                        new_seq,
                        checkpoint_payload,
                        checkpoint_root_id,
                        checkpoint_approval,
                        checkpoint_ask_seq,
                        checkpoint_ask_req,
                        now,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'running', run_id = ?, updated_at = ?, last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (run_id, now, new_seq, session.session_id),
                )
                self._connection.execute("COMMIT")
                in_transaction = False
                return (
                    WakeupRecord(
                        wakeup_id=wakeup.wakeup_id,
                        owner=wakeup.owner,
                        workspace=wakeup.workspace,
                        session_id=wakeup.session_id,
                        payload=wakeup.payload,
                        root_id=wakeup.root_id,
                        role=wakeup.role,
                        due_at=wakeup.due_at,
                        status="claimed",
                        claim_run_id=run_id,
                    ),
                    SessionRecord(
                        session_id=session.session_id,
                        workspace=session.workspace,
                        status="running",
                        active_revision_id=revision_id,
                        run_id=run_id,
                        turn=session.turn,
                        last_event_sequence=new_seq,
                        root_session_id=session.root_session_id,
                        parent_session_id=session.parent_session_id,
                        depth=session.depth,
                        tool_allowlist=session.tool_allowlist,
                    ),
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def schedule_wakeup(
        self,
        wakeup_id: str,
        owner: str,
        workspace: str,
        session_id: str,
        payload: Mapping[str, object],
        due_at: int,
        role: str = "system",
        *,
        root_action_id: str | None = None,
        root_action_budget: int | None = None,
    ) -> str:
        """Persist a wakeup with the root authorization captured by its caller."""
        if root_action_id is not None and (not isinstance(root_action_id, str) or not root_action_id or "\0" in root_action_id):
            raise ValueError("root_action_id must be nonempty text without NUL")
        if root_action_budget is not None and (type(root_action_budget) is not int or root_action_budget < 0):
            raise ValueError("root_action_budget must be a non-negative integer or null")
        if root_action_id is None and root_action_budget is not None:
            raise ValueError("a finite root_action_budget requires its captured root_action_id")
        wakeup_size = len(canonical_json(payload).encode("utf-8"))
        if wakeup_size > MAX_WAKEUP_PAYLOAD_BYTES:
            raise StoreConflict(
                f"wakeup {wakeup_id!r} payload is {wakeup_size} bytes, exceeding the {MAX_WAKEUP_PAYLOAD_BYTES}-byte wakeup-payload bound"
            )
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                session = self._connection.execute(
                    "SELECT status FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise UnknownSession(f"wakeup session {session_id!r} does not exist", session_id)
                if root_action_id is None:
                    if session[0] in ("running", "waiting"):
                        checkpoint = self._connection.execute(
                            "SELECT payload_json FROM checkpoints WHERE session_id = ?",
                            (session_id,),
                        ).fetchone()
                        checkpoint_payload = None if checkpoint is None else own_json_object(json.loads(checkpoint[0]))
                        candidate_root_id = None if checkpoint_payload is None else checkpoint_payload.get("root_id")
                        if not isinstance(candidate_root_id, str) or not candidate_root_id:
                            raise StoreConflict(f"active wakeup session {session_id!r} has no captured root action identity")
                        root_action_id = candidate_root_id
                        if checkpoint_payload is None or "initial_root_budget" not in checkpoint_payload:
                            raise StoreConflict(f"active wakeup session {session_id!r} has no captured root budget")
                        candidate_budget = checkpoint_payload.get("initial_root_budget")
                        if candidate_budget is None:
                            root_action_budget = None
                        elif type(candidate_budget) is int and candidate_budget >= 0:
                            root_action_budget = candidate_budget
                        else:
                            raise StoreConflict(f"active wakeup session {session_id!r} has an invalid captured root budget")
                    else:
                        root_action_id = uuid4().hex
                        root_action_budget = None
                budget_row = self._connection.execute(
                    "SELECT budget FROM root_budgets WHERE root_id = ?",
                    (root_action_id,),
                ).fetchone()
                if root_action_budget is None and budget_row is not None:
                    raise StoreConflict("wakeup captured an uncapped root with a finite budget row")
                if root_action_budget is not None and (budget_row is None or int(budget_row[0]) < root_action_budget):
                    raise StoreConflict("wakeup lost its captured finite root authorization")
                self._connection.execute(
                    """
                    INSERT INTO wakeups (
                        wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget,
                        role, due_at, status, claim_run_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                    ON CONFLICT (wakeup_id) DO NOTHING
                    """,
                    (
                        wakeup_id,
                        owner,
                        workspace,
                        session_id,
                        canonical_json(payload),
                        root_action_id,
                        root_action_budget,
                        role if role in ("system", "user") else "system",
                        due_at,
                        now,
                        now,
                    ),
                )
                self._connection.execute("COMMIT")
                return wakeup_id
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def cancel_wakeup(self, wakeup_id: str, owner: str) -> bool:
        """Cancel one pending wakeup of this owner; already claimed wakeups are not touched."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "UPDATE wakeups SET status = 'cancelled', updated_at = ? WHERE wakeup_id = ? AND owner = ? AND status = 'pending'",
                (int(time.time() * 1000), wakeup_id, owner),
            )
            return cursor.rowcount > 0

    def complete_wakeup(self, wakeup_id: str, status: Literal["completed", "failed", "cancelled"] = "completed") -> None:
        """Settle a claimed wakeup once its run reached a terminal state."""
        with self._lock:
            self._check_open()
            self._connection.execute(
                "UPDATE wakeups SET status = ?, updated_at = ? WHERE wakeup_id = ? AND status = 'claimed'",
                (status, int(time.time() * 1000), wakeup_id),
            )

    def pending_wakeups(self, workspace: str | None = None, limit: int = 100) -> tuple[WakeupRecord, ...]:
        """Read pending wakeups (diagnostics and tests); ordered by due time."""
        with self._lock:
            self._check_open()
            if workspace is None:
                cursor = self._connection.execute(
                    """
                    SELECT wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget, role, due_at, status, claim_run_id
                    FROM wakeups WHERE status = 'pending' ORDER BY due_at ASC LIMIT ?
                    """,
                    (max(1, limit),),
                )
            else:
                cursor = self._connection.execute(
                    """
                    SELECT wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget, role, due_at, status, claim_run_id
                    FROM wakeups WHERE workspace = ? AND status = 'pending' ORDER BY due_at ASC LIMIT ?
                    """,
                    (workspace, max(1, limit)),
                )
            items: list[WakeupRecord] = []
            for row in cursor.fetchall():
                stored = json.loads(row[4])
                if not isinstance(stored, Mapping):
                    raise StoreConflict(f"wakeup {str(row[0])!r} has a malformed payload")
                items.append(
                    WakeupRecord(
                        wakeup_id=str(row[0]),
                        owner=str(row[1]),
                        workspace=str(row[2]),
                        session_id=str(row[3]),
                        payload=own_json_object(stored),
                        root_id=str(row[5]),
                        role=str(row[7]),
                        due_at=int(row[8]),
                        status=str(row[9]),
                        claim_run_id=str(row[10]) if row[10] is not None else None,
                        root_action_budget=None if row[6] is None else int(row[6]),
                    )
                )
            return tuple(items)

    def count_root_children(self, root_session_id: str) -> int:
        """How many child sessions one chain root has already spawned (its durable budget)."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE root_session_id = ? AND parent_session_id IS NOT NULL",
                (root_session_id,),
            ).fetchone()
            return int(row[0]) if row is not None else 0

    def session(self, session_id: str) -> SessionRecord:
        """Fetch session metadata by session ID."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                       parent_session_id, root_session_id, depth, tool_allowlist_json
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"unknown session: {session_id}")
            return _session_record(row)

    def descendant_sessions(self, session_id: str) -> tuple[SessionRecord, ...]:
        """Every transitive descendant of one session, in stable creation order."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            cursor = self._connection.execute(
                """
                WITH RECURSIVE subtree(session_id) AS (
                    SELECT session_id FROM sessions WHERE parent_session_id = ?
                    UNION
                    SELECT s.session_id FROM sessions s JOIN subtree t ON s.parent_session_id = t.session_id
                )
                SELECT s.session_id, s.workspace, s.status, s.active_revision_id, s.run_id, s.turn,
                       s.last_event_sequence, s.parent_session_id, s.root_session_id, s.depth, s.tool_allowlist_json
                FROM sessions s JOIN subtree t ON s.session_id = t.session_id
                ORDER BY s.rowid ASC
                """,
                (session_id,),
            )
            return tuple(_session_record(row) for row in cursor.fetchall())

    def latest_stop_payload(self, session_id: str, run_id: str | None) -> Mapping[str, object] | None:
        """Return the exact latest committed interruption payload for one run."""
        event = self.latest_stop_event(session_id, run_id)
        return None if event is None else event.payload

    def latest_stop_event(self, session_id: str, run_id: str | None) -> StoredEvent | None:
        """Return the exact latest committed interruption event for one run."""
        with self._lock:
            self._check_open()
            if not run_id:
                return None
            row = self._connection.execute(
                """
                SELECT sequence, source, actor, revision_id, payload_json
                FROM session_events
                WHERE session_id = ? AND run_id = ? AND event_type = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (session_id, run_id, EventType.RUN_INTERRUPTED),
            ).fetchone()
            if row is None:
                return None
            return StoredEvent(
                session_id=session_id,
                sequence=int(row[0]),
                run_id=run_id,
                event_type=EventType.RUN_INTERRUPTED,
                source=str(row[1]),
                actor=str(row[2]),
                revision_id=str(row[3]),
                payload=own_json_object(json.loads(row[4])),
            )

    def latest_stop_cause(self, session_id: str, run_id: str | None) -> str | None:
        """The committed stop cause of one run, or None when no terminal fact names one."""
        payload = self.latest_stop_payload(session_id, run_id)
        cause = None if payload is None else payload.get("stop_cause")
        return cause if isinstance(cause, str) and cause else None

    def has_queued_child_followup(self, session_id: str) -> bool:
        """Whether one session holds a pending core child follow-up (never notices or claims)."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT 1 FROM continuations
                WHERE session_id = ? AND status = 'pending' AND owner = ?
                  AND kind = 'child_followup'
                LIMIT 1
                """,
                (session_id, CHILD_MESSAGE_OWNER),
            ).fetchone()
            return row is not None

    def child_message_ids(self, session_id: str) -> tuple[str, ...]:
        """Capture exact pending or claimed core child-message identities."""
        with self._lock:
            self._check_open()
            rows = self._connection.execute(
                """
                SELECT continuation_id FROM continuations
                WHERE session_id = ? AND status IN ('pending', 'claimed') AND owner = ?
                ORDER BY rowid ASC
                """,
                (session_id, CHILD_MESSAGE_OWNER),
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

    def child_wait_registrations(self, child_session_ids: Sequence[str]) -> tuple[tuple[SessionRecord, tuple[str, ...]], ...]:
        """Snapshot current child runs and their registered child messages atomically."""
        with self._lock:
            self._check_open()
            self._connection.execute("BEGIN")
            try:
                snapshots = tuple(
                    (self.session(child_session_id), self.child_message_ids(child_session_id)) for child_session_id in child_session_ids
                )
                self._connection.execute("COMMIT")
                return snapshots
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def has_pending_user_message(self, session_id: str) -> bool:
        """Whether fresh user input is queued behind this session's active run."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT 1 FROM continuations
                WHERE session_id = ? AND status = 'pending' AND owner = 'kernel' AND source = 'user_message'
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return row is not None

    def latest_tool_fact(self, session_id: str) -> tuple[str, str] | None:
        """The newest committed tool identity/outcome pair, or None when no tool finished."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT event_type, payload_json FROM session_events
                WHERE session_id = ? AND event_type IN (?, ?)
                ORDER BY sequence DESC LIMIT 1
                """,
                (session_id, EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED),
            ).fetchone()
            if row is None:
                return None
            try:
                payload = own_json_object(json.loads(row[1]))
                call = payload.get("call")
                result = payload.get("result")
                if not isinstance(call, Mapping) or not isinstance(result, Mapping):
                    return None
                name = call.get("tool_name")
                if not isinstance(name, str) or not name:
                    return None
                outcome = result.get("status")
                if outcome not in ("ok", "error"):
                    return None
                if row[0] == EventType.TOOL_INTERRUPTED:
                    outcome = "cancelled"
                return name, str(outcome)
            except ValueError, OSError:
                return None

    def session_info(self, session_id: str, owner: str) -> SessionInfo:
        """Read session, parked approval and budgets in one consistent row."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT s.session_id, s.workspace, s.status, s.active_revision_id, s.run_id,
                       s.turn, s.last_event_sequence, s.parent_session_id, s.root_session_id,
                       s.depth, s.tool_allowlist_json, a.payload_json, r.budget, r.reserved,
                       b.owner, b.since_sequence, b.max_input_tokens, b.max_output_tokens,
                       b.max_total_tokens, b.max_cost_usd, b.max_elapsed_seconds
                FROM sessions s
                LEFT JOIN checkpoints c ON c.session_id = s.session_id AND c.run_id = s.run_id
                LEFT JOIN session_events a ON a.session_id = s.session_id
                    AND a.sequence = c.approval_sequence
                    AND c.kind = 'approval_wait' AND a.event_type = ?
                LEFT JOIN root_budgets r ON r.root_id = c.root_id
                LEFT JOIN session_budgets b ON b.session_id = s.session_id AND b.owner = ?
                WHERE s.session_id = ?
                """,
                (EventType.APPROVAL_REQUESTED, owner, session_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown session: {session_id}")
            session = _session_record(row[:11])
            budget = (
                None
                if row[14] is None
                else PluginBudget(
                    owner=row[14],
                    since_sequence=row[15],
                    max_input_tokens=row[16],
                    max_output_tokens=row[17],
                    max_total_tokens=row[18],
                    max_cost_usd=row[19],
                    max_elapsed_seconds=row[20],
                )
            )
            return SessionInfo(
                session_id=session.session_id,
                status=session.status,
                run_id=session.run_id,
                revision_id=session.active_revision_id,
                parent_session_id=session.parent_session_id,
                root_session_id=session.root_session_id,
                depth=session.depth,
                turn=session.turn,
                last_event_sequence=session.last_event_sequence,
                tool_allowlist=session.tool_allowlist,
                pending_approval=None if row[11] is None else json.loads(row[11]),
                chain_budget=row[12],
                chain_reserved=row[13],
                budget=budget,
            )

    def sessions(self, workspace: str | None = None, limit: int | None = 100) -> tuple[SessionRecord, ...]:
        """Fetch sessions, optionally filtered by workspace, ordered by updated_at descending.

        ``limit=None`` is the explicit complete-scan form used by startup publication recovery;
        ordinary callers keep the bounded default.
        """
        with self._lock:
            self._check_open()
            capped_limit = None if limit is None else max(1, limit)
            if workspace is not None:
                sql = """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                           parent_session_id, root_session_id, depth, tool_allowlist_json
                    FROM sessions
                    WHERE workspace = ?
                    ORDER BY updated_at DESC
                """
                params: tuple[object, ...] = (workspace,)
            else:
                sql = """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                           parent_session_id, root_session_id, depth, tool_allowlist_json
                    FROM sessions
                    ORDER BY updated_at DESC
                """
                params = ()
            if capped_limit is not None:
                sql += " LIMIT ?"
                params = (*params, capped_limit)
            cursor = self._connection.execute(sql, params)
            return tuple(_session_record(row) for row in cursor.fetchall())

    def events(self, session_id: str, after_sequence: int = 0, limit: int | None = None) -> tuple[StoredEvent, ...]:
        """Fetch stored events for a session strictly after a given sequence number, optionally limited."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            if limit is not None:
                cursor = self._connection.execute(
                    """
                    SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    FROM session_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    LIMIT ?
                    """,
                    (session_id, after_sequence, max(1, limit)),
                )
            else:
                cursor = self._connection.execute(
                    """
                    SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    FROM session_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    """,
                    (session_id, after_sequence),
                )
            return tuple(
                StoredEvent(
                    session_id=row[0],
                    sequence=row[1],
                    run_id=row[2],
                    event_type=row[3],
                    source=row[4],
                    actor=row[5],
                    revision_id=row[6],
                    payload=json.loads(row[7]),
                )
                for row in cursor.fetchall()
            )

    def events_after(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> tuple[StoredEvent, ...]:
        """Bounded tail read of stored events for continuous SSE streaming."""
        return self.events(session_id, after_sequence=after_sequence, limit=limit)

    def latest_event(self, session_id: str, event_type: str, run_id: str | None = None) -> StoredEvent | None:
        """Newest event of one type (optionally one run), or None when absent."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            if run_id is None:
                row = self._connection.execute(
                    """
                    SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    FROM session_events
                    WHERE session_id = ? AND event_type = ?
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (session_id, event_type),
                ).fetchone()
            else:
                row = self._connection.execute(
                    """
                    SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    FROM session_events
                    WHERE session_id = ? AND event_type = ? AND run_id = ?
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (session_id, event_type, run_id),
                ).fetchone()
            if row is None:
                return None
            return StoredEvent(
                session_id=row[0],
                sequence=row[1],
                run_id=row[2],
                event_type=row[3],
                source=row[4],
                actor=row[5],
                revision_id=row[6],
                payload=json.loads(row[7]),
            )

    def events_by_types(self, session_id: str, types: Sequence[str], after_sequence: int = 0, limit: int | None = None) -> tuple[StoredEvent, ...]:
        """Events of the given types in ascending sequence order, optionally ranged."""
        if not types:
            return ()
        with self._lock:
            self._check_open()
            self.session(session_id)
            placeholders = ", ".join("?" for _ in types)
            sql = f"""
                SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                FROM session_events
                WHERE session_id = ? AND event_type IN ({placeholders}) AND sequence > ?
                ORDER BY sequence ASC
                """
            params: list[object] = [session_id, *types, after_sequence]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(max(1, limit))
            cursor = self._connection.execute(sql, tuple(params))
            return tuple(
                StoredEvent(
                    session_id=row[0],
                    sequence=row[1],
                    run_id=row[2],
                    event_type=row[3],
                    source=row[4],
                    actor=row[5],
                    revision_id=row[6],
                    payload=json.loads(row[7]),
                )
                for row in cursor.fetchall()
            )

    def event_payload_bytes(self, session_id: str, after_sequence: int = 0, limit: int | None = None) -> dict[int, int]:
        """Stored canonical payload bytes by sequence for a range; no payload decode."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            if limit is not None:
                cursor = self._connection.execute(
                    """
                    SELECT sequence, payload_bytes
                    FROM session_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    LIMIT ?
                    """,
                    (session_id, after_sequence, max(1, limit)),
                )
            else:
                cursor = self._connection.execute(
                    """
                    SELECT sequence, payload_bytes
                    FROM session_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    """,
                    (session_id, after_sequence),
                )
            return {int(row[0]): int(row[1]) for row in cursor.fetchall()}

    def event_type_counts(self, session_id: str) -> dict[str, int]:
        """Count committed events by type with one SQL aggregate, no payload reads."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            cursor = self._connection.execute(
                """
                SELECT event_type, COUNT(*)
                FROM session_events
                WHERE session_id = ?
                GROUP BY event_type
                """,
                (session_id,),
            )
            return {row[0]: int(row[1]) for row in cursor.fetchall()}

    def checkpoint(self, session_id: str) -> Checkpoint | None:
        """Fetch the current checkpoint of a session, or None if none has been committed."""
        with self._lock:
            self._check_open()
            # Verify session exists
            self.session(session_id)
            cursor = self._connection.execute(
                """
                SELECT kind, revision_id, run_id, last_event_sequence, payload_json
                FROM checkpoints
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return Checkpoint(
                kind=row[0],
                revision_id=row[1],
                run_id=row[2],
                last_event_sequence=row[3],
                payload=json.loads(row[4]),
            )

    def waiting_ask_parks(self) -> tuple[WaitingAskPark, ...]:
        """Waiting sessions holding a live ask binding, from checkpoint columns only.

        Candidates for the ask-expiry sweep: no event scan, no answer resolution.
        The caller revalidates each candidate (session, binding, request, answer
        absence) under that session's lock before settling anything.
        """
        with self._lock:
            self._check_open()
            rows = self._connection.execute(
                """
                SELECT s.session_id, c.run_id, c.revision_id,
                       c.ask_sequence,
                       c.ask_request_id,
                       c.last_event_sequence
                FROM sessions s JOIN checkpoints c ON c.session_id = s.session_id
                WHERE s.status = 'waiting' AND c.run_id = s.run_id
                  AND c.kind <> 'terminal'
                  AND c.ask_request_id IS NOT NULL
                ORDER BY s.session_id ASC
                """
            ).fetchall()
            parks: list[WaitingAskPark] = []
            for row in rows:
                sequence, request_id = row[3], row[4]
                if type(sequence) is not int or sequence < 1 or not isinstance(request_id, str) or not request_id:
                    raise StoreConflict(f"session {row[0]!r} holds a malformed live ask binding")
                parks.append(
                    WaitingAskPark(
                        session_id=str(row[0]),
                        run_id=str(row[1]),
                        revision_id=str(row[2]),
                        ask_sequence=sequence,
                        ask_request_id=request_id,
                        checkpoint_sequence=int(row[5]),
                    )
                )
            return tuple(parks)

    def _record_event_indexes(self, session_id: str, event_type: str, payload: Mapping[str, object], now: int) -> None:
        """Mirror indexable event keys into side tables; callers hold the lock and a transaction."""
        if event_type == EventType.ASK_ANSWERED:
            request_id = payload.get("request_id")
            if isinstance(request_id, str) and request_id:
                self._connection.execute(
                    "INSERT OR IGNORE INTO ask_answers (session_id, request_id, created_at) VALUES (?, ?, ?)",
                    (session_id, request_id, now),
                )
        elif event_type == EventType.CONTINUATION_STARTED:
            continuation_id = payload.get("continuation_id")
            if isinstance(continuation_id, str) and continuation_id:
                self._connection.execute(
                    "INSERT OR IGNORE INTO continuation_starts (session_id, continuation_id, created_at) VALUES (?, ?, ?)",
                    (session_id, continuation_id, now),
                )

    def commit(
        self,
        session_id: str,
        *,
        expected_sequence: int,
        expected_run_id: str | None,
        run_id: str,
        revision_id: str,
        events: tuple[EventDraft, ...] | Sequence[EventDraft],
        status: str,
        checkpoint_kind: str,
        checkpoint_payload: Mapping[str, object],
        start_turn: bool = False,
    ) -> SessionRecord:
        """Atomic CAS commit appending events, advancing session state, and upserting a checkpoint."""
        return self.commit_staged(
            session_id,
            expected_sequence=expected_sequence,
            expected_run_id=expected_run_id,
            run_id=run_id,
            revision_id=revision_id,
            events=events,
            status=status,
            checkpoint_kind=checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
            start_turn=start_turn,
        )

    def commit_staged(
        self,
        session_id: str,
        *,
        expected_sequence: int | None = None,
        expected_run_id: str | None = None,
        run_id: str,
        revision_id: str,
        events: tuple[EventDraft, ...] | Sequence[EventDraft],
        status: str,
        checkpoint_kind: str | None = None,
        checkpoint_payload: Mapping[str, object] | None = None,
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        staged_confirmations: Sequence[StagedConfirmation] = (),
        staged_wakeups: Sequence[StagedWakeup] = (),
        staged_wakeup_cancels: Sequence[StagedWakeupCancel] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: Literal["completed", "failed", "cancelled"] = "completed",
        checkpoint_bindings: Sequence[tuple[str, str]] = (),
        root_action_id: str | None = None,
        root_action_budget: int | None = None,
        root_budget_grant: tuple[str, int, str] | None = None,
        root_reservation: tuple[str, int | None] | None = None,
        start_turn: bool = False,
        ask_answer_guard: str | None = None,
    ) -> SessionRecord:
        """Atomic CAS commit applying staged state updates, continuations, event drafts, and checkpoint.

        Each `(payload_field, event_type)` binding in `checkpoint_bindings` is
        assigned the actual appended sequence of the first event of that type in
        THIS transaction, so a checkpoint reference can never point at a guessed
        or not-yet-committed sequence.
        """

        if staged_wakeups:
            if not isinstance(root_action_id, str) or not root_action_id:
                raise StoreConflict("staged wakeups require a captured root action identity")
            if root_action_budget is not None and (type(root_action_budget) is not int or root_action_budget < 0):
                raise StoreConflict("staged wakeups have an invalid captured root budget")
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                if staged_wakeups:
                    assert root_action_id is not None
                    wakeup_budget_row = self._connection.execute(
                        "SELECT budget FROM root_budgets WHERE root_id = ?",
                        (root_action_id,),
                    ).fetchone()
                    if root_action_budget is None and wakeup_budget_row is not None:
                        raise StoreConflict("staged wakeup captured an uncapped root with a finite budget row")
                    if root_action_budget is not None and (wakeup_budget_row is None or int(wakeup_budget_row[0]) < root_action_budget):
                        raise StoreConflict("staged wakeup lost its captured finite root authorization")

                if root_budget_grant is not None:
                    root_id, amount, grant_id = root_budget_grant
                    prior = self._connection.execute(
                        "SELECT root_id, amount FROM root_grants WHERE grant_id = ?",
                        (grant_id,),
                    ).fetchone()
                    if prior is not None:
                        if prior[0] != root_id or prior[1] != amount:
                            raise StoreConflict("root action grant id was already used for a different authorization")
                    else:
                        state = self._connection.execute(
                            "SELECT budget FROM root_budgets WHERE root_id = ?",
                            (root_id,),
                        ).fetchone()
                        if state is None:
                            raise StoreConflict("there is no finite root action budget to extend")
                        if int(state[0]) + amount > 9_223_372_036_854_775_807:
                            raise ValueError("root action budget extension exceeds SQLite INTEGER range")
                        self._connection.execute(
                            "UPDATE root_budgets SET budget = budget + ?, updated_at = ? WHERE root_id = ?",
                            (amount, int(time.time() * 1000), root_id),
                        )
                        self._connection.execute(
                            "INSERT INTO root_grants (grant_id, root_id, amount, created_at) VALUES (?, ?, ?, ?)",
                            (grant_id, root_id, amount, int(time.time() * 1000)),
                        )

                if root_reservation is not None:
                    root_id, root_limit = root_reservation
                    reserved_at = int(time.time() * 1000)
                    if root_limit is not None:
                        self._connection.execute(
                            """
                            INSERT OR IGNORE INTO root_budgets (root_id, budget, reserved, created_at, updated_at)
                            VALUES (?, ?, 0, ?, ?)
                            """,
                            (root_id, root_limit, reserved_at, reserved_at),
                        )
                    state = self._connection.execute(
                        "SELECT budget, reserved FROM root_budgets WHERE root_id = ?",
                        (root_id,),
                    ).fetchone()
                    if state is not None:
                        reserved = self._connection.execute(
                            """
                            UPDATE root_budgets
                            SET reserved = reserved + 1, updated_at = ?
                            WHERE root_id = ? AND reserved < budget
                            """,
                            (reserved_at, root_id),
                        )
                        if reserved.rowcount == 0:
                            raise RootBudgetExhausted(root_id, int(state[0]), int(state[1]))

                cursor = self._connection.execute(
                    """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                           parent_session_id, root_session_id, depth, tool_allowlist_json
                    FROM sessions
                    WHERE session_id = ?
                    """,
                    (session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise UnknownSession(f"session {session_id!r} does not exist", session_id)

                (_, workspace, _, curr_revision_id, curr_run_id, curr_turn, curr_seq, curr_parent, curr_root, curr_depth, curr_allowlist) = row

                if expected_sequence is not None and curr_seq != expected_sequence:
                    raise SequenceConflict(f"sequence mismatch for session {session_id!r}: expected {expected_sequence}, got {curr_seq}")
                if expected_run_id is not None and curr_run_id != expected_run_id:
                    raise StoreConflict(f"run_id mismatch for session {session_id!r}: expected {expected_run_id!r}, got {curr_run_id!r}")
                if curr_revision_id != revision_id:
                    raise StoreConflict(f"active revision mismatch for session {session_id!r}: active is {curr_revision_id!r}, got {revision_id!r}")
                if ask_answer_guard is not None:
                    decided = self._connection.execute(
                        "SELECT 1 FROM ask_answers WHERE session_id = ? AND request_id = ? LIMIT 1",
                        (session_id, ask_answer_guard),
                    ).fetchone()
                    if decided is not None:
                        raise AskAnswerConflict(session_id, ask_answer_guard)
                now = int(time.time() * 1000)

                # Apply staged state updates
                self._apply_state_updates(staged_state, default_revision_id=revision_id, now=now)

                # Apply staged continuation operations in their recorded order, so
                # cancel-then-enqueue keeps the new continuation while enqueue-then-cancel
                # (including by returned id) cancels exactly the one that was enqueued.
                ops: list[tuple[int, Literal["enqueue", "cancel"], object]] = []
                ops.extend((c.ordinal, "enqueue", c) for c in staged_continuations)
                ops.extend((c.ordinal, "cancel", c) for c in staged_cancellations)
                for _ord, kind, payload in sorted(ops, key=lambda item: item[0]):
                    if kind == "enqueue":
                        cont = cast(StagedContinuationDraft, payload)
                        cont_payload_json = canonical_json(cont.payload)
                        if cont.idempotency_key is not None:
                            # Model rows and background rows live in separate tables: one
                            # owner/session/key names each independently, and only rows
                            # of the same table dedupe.
                            if cont.source == BACKGROUND_ACTION_SOURCE:
                                c_chk = self._connection.execute(
                                    """
                                    SELECT action_id FROM background_actions
                                    WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                                    """,
                                    (session_id, cont.owner, cont.idempotency_key),
                                )
                            else:
                                c_chk = self._connection.execute(
                                    """
                                    SELECT continuation_id FROM continuations
                                    WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                                    """,
                                    (session_id, cont.owner, cont.idempotency_key),
                                )
                            if c_chk.fetchone() is not None:
                                continue
                        continuation_size = len(cont_payload_json.encode("utf-8"))
                        if continuation_size > MAX_CONTINUATION_PAYLOAD_BYTES:
                            raise StoreConflict(
                                f"continuation {cont.continuation_id!r} payload is {continuation_size} bytes, "
                                f"exceeding the {MAX_CONTINUATION_PAYLOAD_BYTES}-byte continuation-payload bound"
                            )
                        if cont.source == BACKGROUND_ACTION_SOURCE:
                            draft = cont.payload
                            if "root_action_budget" not in draft:
                                raise StoreConflict(f"background action {cont.continuation_id!r} has no captured root budget")
                            staged_budget = draft["root_action_budget"]
                            if staged_budget is not None and (type(staged_budget) is not int or not 0 <= staged_budget <= 9_223_372_036_854_775_807):
                                raise StoreConflict(f"background action {cont.continuation_id!r} has an invalid captured root budget")
                            if not isinstance(cont.root_id, str) or not cont.root_id:
                                raise StoreConflict(f"background action {cont.continuation_id!r} has no captured root authorization")
                            staged_authority = draft.get("tool_wait_authority")
                            try:
                                self._connection.execute(
                                    """
                                    INSERT INTO background_actions (
                                        action_id, owner, session_id, revision_id, idempotency_key, root_id,
                                        root_action_budget, status, payload_json, authority_json, claim_json,
                                        error, created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, ?, ?)
                                    """,
                                    (
                                        cont.continuation_id,
                                        cont.owner,
                                        session_id,
                                        revision_id,
                                        cont.idempotency_key,
                                        cont.root_id,
                                        staged_budget,
                                        _canonical_json_value(draft.get("action")),
                                        None if staged_authority is None else _canonical_json_value(staged_authority),
                                        now,
                                        now,
                                    ),
                                )
                            except sqlite3.IntegrityError as exc:
                                raise StoreConflict(f"background action {cont.continuation_id!r} already exists") from exc
                        else:
                            try:
                                self._connection.execute(
                                    """
                                    INSERT INTO continuations (
                                        continuation_id, owner, session_id, source, kind, run_id, revision_id,
                                        idempotency_key, root_id, role, status, payload_json, created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                                    """,
                                    (
                                        cont.continuation_id,
                                        cont.owner,
                                        session_id,
                                        cont.source,
                                        _continuation_kind(cont.payload),
                                        run_id,
                                        revision_id,
                                        cont.idempotency_key,
                                        cont.root_id,
                                        cont.role if cont.role in ("system", "user") else "system",
                                        cont_payload_json,
                                        now,
                                        now,
                                    ),
                                )
                            except sqlite3.IntegrityError as exc:
                                raise StoreConflict(f"continuation {cont.continuation_id!r} already exists") from exc
                    else:
                        canc = cast(StagedCancellation, payload)
                        if canc.continuation_id is not None:
                            self._connection.execute(
                                """
                                UPDATE continuations
                                SET status = 'cancelled', updated_at = ?
                                WHERE session_id = ? AND owner = ? AND continuation_id = ?
                                  AND status IN ('pending', 'claimed') AND (
                                      ? = 0 OR NOT EXISTS (
                                          SELECT 1 FROM continuation_starts
                                          WHERE continuation_starts.session_id = continuations.session_id
                                            AND continuation_starts.continuation_id = continuations.continuation_id
                                      )
                                  )
                                """,
                                (
                                    now,
                                    session_id,
                                    canc.owner,
                                    canc.continuation_id,
                                    int(canc.only_if_unstarted),
                                ),
                            )
                        else:
                            # Background actions are cancelled through their own abort-aware path;
                            # a blanket model-continuation cancel must not strand live drivers.
                            self._connection.execute(
                                """
                                UPDATE continuations
                                SET status = 'cancelled', updated_at = ?
                                WHERE session_id = ? AND owner = ? AND status IN ('pending', 'claimed')
                                """,
                                (now, session_id, canc.owner),
                            )

                # Apply staged budgets
                for bu in staged_budgets:
                    if bu.action == "clear":
                        self._connection.execute(
                            "DELETE FROM session_budgets WHERE session_id = ? AND owner = ?",
                            (session_id, bu.owner),
                        )
                    elif bu.action == "set" and bu.budget is not None:
                        self._connection.execute(
                            """
                            INSERT INTO session_budgets (
                                session_id, owner, since_sequence, max_input_tokens, max_output_tokens, max_total_tokens,
                                max_cost_usd, max_elapsed_seconds, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (session_id, owner) DO UPDATE SET
                                since_sequence = excluded.since_sequence,
                                max_input_tokens = excluded.max_input_tokens,
                                max_output_tokens = excluded.max_output_tokens,
                                max_total_tokens = excluded.max_total_tokens,
                                max_cost_usd = excluded.max_cost_usd,
                                max_elapsed_seconds = excluded.max_elapsed_seconds,
                                updated_at = excluded.updated_at
                            """,
                            (
                                session_id,
                                bu.owner,
                                bu.budget.since_sequence,
                                bu.budget.max_input_tokens,
                                bu.budget.max_output_tokens,
                                bu.budget.max_total_tokens,
                                bu.budget.max_cost_usd,
                                bu.budget.max_elapsed_seconds,
                                now,
                                now,
                            ),
                        )

                # Create pending action confirmations with the event that announces them.
                for confirmation in staged_confirmations:
                    try:
                        self._connection.execute(
                            """
                            INSERT INTO action_confirmations (
                                request_id, session_id, owner, command_name, arguments_json, revision_id,
                                expected_state_version, ui_type, ui_data_json, status, created_at, decided_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
                            """,
                            (
                                confirmation.request_id,
                                session_id,
                                confirmation.owner,
                                confirmation.command_name,
                                canonical_json(confirmation.arguments),
                                revision_id,
                                confirmation.expected_state_version,
                                confirmation.ui_type,
                                canonical_json(confirmation.ui_data),
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise StoreConflict(f"confirmation {confirmation.request_id!r} already exists") from exc

                # Schedule or cancel owner wakeups with the callback that asked for them.
                if staged_wakeups and (not isinstance(root_action_id, str) or not root_action_id):
                    raise StoreConflict("staged wakeups require the run's captured root action identity")
                for wakeup in staged_wakeups:
                    wakeup_size = len(canonical_json(wakeup.payload).encode("utf-8"))
                    if wakeup_size > MAX_WAKEUP_PAYLOAD_BYTES:
                        raise StoreConflict(
                            f"wakeup {wakeup.wakeup_id!r} payload is {wakeup_size} bytes, "
                            f"exceeding the {MAX_WAKEUP_PAYLOAD_BYTES}-byte wakeup-payload bound"
                        )
                    try:
                        self._connection.execute(
                            """
                            INSERT INTO wakeups (
                                wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget,
                                role, due_at, status, claim_run_id, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                            """,
                            (
                                wakeup.wakeup_id,
                                wakeup.owner,
                                workspace,
                                wakeup.session_id,
                                canonical_json(wakeup.payload),
                                root_action_id,
                                root_action_budget,
                                wakeup.role if wakeup.role in ("system", "user") else "system",
                                wakeup.due_at,
                                now,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise StoreConflict(f"wakeup {wakeup.wakeup_id!r} already exists") from exc
                for cancel in staged_wakeup_cancels:
                    self._connection.execute(
                        """
                        UPDATE wakeups SET status = 'cancelled', updated_at = ?
                        WHERE wakeup_id = ? AND owner = ? AND status = 'pending'
                        """,
                        (now, cancel.wakeup_id, cancel.owner),
                    )

                # Apply observer receipts
                for r in observer_receipts:
                    if r.require_interruption_source:
                        if not isinstance(r.expected_run_id, str) or not r.expected_run_id:
                            raise StoreConflict("an interruption receipt has no exact child run identity")
                        source = self._connection.execute(
                            """
                            SELECT event_type, run_id FROM session_events
                            WHERE session_id = ? AND sequence = ?
                            """,
                            (r.session_id, r.event_sequence),
                        ).fetchone()
                        if source is None or source[0] != EventType.RUN_INTERRUPTED or source[1] != r.expected_run_id:
                            raise StoreConflict("child interruption receipt does not match its exact source event")
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO observer_receipts (session_id, event_sequence, owner, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (r.session_id, r.event_sequence, r.owner, now),
                    )

                # Update completed continuation
                if completed_continuation_id is not None:
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = ?, updated_at = ?
                        WHERE continuation_id = ?
                        """,
                        (completed_continuation_status, now, completed_continuation_id),
                    )

                # Append events
                current_seq = curr_seq
                first_sequence_by_type: dict[str, int] = {}
                for draft in events:
                    if draft.event_type == EventType.WINDOW_PROJECTED:
                        projection = draft.payload
                        self.set_window_projection(
                            session_id,
                            str(projection["owner"]),
                            covered_from_sequence=cast(int, projection["covered_from_sequence"]),
                            covered_to_sequence=cast(int, projection["covered_to_sequence"]),
                            replacement=str(projection["replacement"]),
                            covered_bytes=cast(int, projection["covered_bytes"]),
                            dropped_bytes=cast(int, projection["dropped_bytes"]),
                        )
                    current_seq += 1
                    first_sequence_by_type.setdefault(draft.event_type, current_seq)
                    payload_json = canonical_json(draft.payload)
                    payload_size = len(payload_json.encode("utf-8"))
                    if payload_size > MAX_COMMITTED_EVENT_BYTES:
                        raise StoreConflict(
                            f"session event {draft.event_type!r} payload is {payload_size} bytes, "
                            f"exceeding the {MAX_COMMITTED_EVENT_BYTES}-byte committed-event bound"
                        )
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            current_seq,
                            run_id,
                            draft.event_type,
                            draft.source,
                            draft.actor,
                            revision_id,
                            payload_json,
                            payload_size,
                        ),
                    )
                    self._record_event_indexes(session_id, draft.event_type, draft.payload, now)

                new_turn = curr_turn + 1 if start_turn else curr_turn

                # Update session
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?,
                        run_id = ?,
                        turn = ?,
                        updated_at = ?,
                        last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (status, run_id, new_turn, now, current_seq, session_id),
                )

                if checkpoint_kind is not None and checkpoint_payload is not None:
                    bound_payload = dict(checkpoint_payload)
                    for bound_field, bound_event_type in checkpoint_bindings:
                        bound_sequence = first_sequence_by_type.get(bound_event_type)
                        if bound_sequence is None:
                            raise StoreConflict(f"checkpoint binding {bound_field!r} has no {bound_event_type!r} event in this commit")
                        bound_payload[bound_field] = bound_sequence
                    checkpoint_json = canonical_json(bound_payload)
                    checkpoint_root_id, checkpoint_approval, checkpoint_ask_seq, checkpoint_ask_req = _checkpoint_index_values(bound_payload)
                    self._connection.execute(
                        """
                        INSERT INTO checkpoints (
                            session_id, kind, revision_id, run_id, last_event_sequence, payload_json,
                            root_id, approval_sequence, ask_sequence, ask_request_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (session_id) DO UPDATE SET
                            kind = excluded.kind,
                            revision_id = excluded.revision_id,
                            run_id = excluded.run_id,
                            last_event_sequence = excluded.last_event_sequence,
                            payload_json = excluded.payload_json,
                            root_id = excluded.root_id,
                            approval_sequence = excluded.approval_sequence,
                            ask_sequence = excluded.ask_sequence,
                            ask_request_id = excluded.ask_request_id,
                            updated_at = excluded.updated_at
                        """,
                        (
                            session_id,
                            checkpoint_kind,
                            revision_id,
                            run_id,
                            current_seq,
                            checkpoint_json,
                            checkpoint_root_id,
                            checkpoint_approval,
                            checkpoint_ask_seq,
                            checkpoint_ask_req,
                            now,
                        ),
                    )
                self._connection.execute("COMMIT")
                in_transaction = False

                return SessionRecord(
                    session_id=session_id,
                    workspace=workspace,
                    status=status,
                    active_revision_id=revision_id,
                    run_id=run_id,
                    turn=new_turn,
                    root_session_id=curr_root,
                    parent_session_id=curr_parent,
                    depth=curr_depth,
                    tool_allowlist=_allowlist_from_json(curr_allowlist),
                    last_event_sequence=current_seq,
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _apply_state_updates(
        self,
        state_updates: Sequence[StagedStateUpdate],
        *,
        default_revision_id: str,
        now: int,
    ) -> None:
        """Apply owner state copies with their optimistic-concurrency obligation, inside an open transaction."""
        seen: set[tuple[str, str, str]] = set()
        for st in state_updates:
            key = (st.scope, st.scope_key, st.owner)
            if key in seen:
                raise StoreConflict(f"duplicate state update for {st.owner!r} in {st.scope}:{st.scope_key} in one commit")
            seen.add(key)
            payload_json = canonical_json(st.payload)
            actual_schema_ver = max(1, st.schema_version)
            actual_rev_id = st.revision_id or default_revision_id
            if st.expected_version == 0:
                try:
                    self._connection.execute(
                        """
                        INSERT INTO plugin_state (scope, scope_key, owner, version, schema_version, revision_id, payload_json, updated_at)
                        VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                        """,
                        (st.scope, st.scope_key, st.owner, actual_schema_ver, actual_rev_id, payload_json, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StoreConflict(f"state already exists for owner {st.owner!r} in {st.scope}:{st.scope_key}") from exc
            else:
                c = self._connection.execute(
                    """
                    UPDATE plugin_state
                    SET version = version + 1, schema_version = ?, revision_id = ?, payload_json = ?, updated_at = ?
                    WHERE scope = ? AND scope_key = ? AND owner = ? AND version = ?
                    """,
                    (actual_schema_ver, actual_rev_id, payload_json, now, st.scope, st.scope_key, st.owner, st.expected_version),
                )
                if c.rowcount == 0:
                    raise StoreConflict(f"state CAS conflict for {st.owner!r} in {st.scope}:{st.scope_key}: expected version {st.expected_version}")

    def commit_activation(
        self,
        *,
        workspace: str,
        session_id: str,
        expected_sequence: int,
        expected_run_id: str | None,
        expected_revision_id: str,
        target_revision_id: str,
        target_payload: Mapping[str, object],
        run_id: str,
        status: str,
        events: Sequence[EventDraft],
        checkpoint_kind: Literal["ready", "approval_wait", "terminal", "interrupted"] | None,
        checkpoint_payload: Mapping[str, object] | None,
        state_updates: Sequence[StagedStateUpdate] = (),
        cancelled_owners: Sequence[str] = (),
        cleared_budget_owners: Sequence[str] = (),
    ) -> SessionRecord:
        """One CAS transaction that switches the workspace to a new capability revision.

        Covers, atomically: the immutable revision row, every migrated/deactivated
        owner state copy, the workspace pointer, EVERY session ref in the workspace
        (including the acting running session), retargeted or cancelled queued work,
        cleared budgets, the acting session's events/checkpoint, and therefore the
        durable activation request consumption that those events record. Blockers
        are re-verified inside the transaction, so a racing writer cannot slip a
        second generation in.
        """
        target_text = canonical_json(target_payload)
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                now = int(time.time() * 1000)

                cursor = self._connection.execute(
                    """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                           parent_session_id, root_session_id, depth, tool_allowlist_json
                    FROM sessions
                    WHERE session_id = ?
                    """,
                    (session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise UnknownSession(f"session {session_id!r} does not exist", session_id)
                (_, row_workspace, _, curr_revision_id, curr_run_id, curr_turn, curr_seq, curr_parent, curr_root, curr_depth, curr_allowlist) = row
                if row_workspace != workspace:
                    raise StoreConflict(f"session {session_id!r} belongs to workspace {row_workspace!r}")
                if curr_seq != expected_sequence:
                    raise SequenceConflict(f"sequence mismatch for session {session_id!r}: expected {expected_sequence}, got {curr_seq}")
                if expected_run_id is not None and curr_run_id != expected_run_id:
                    raise StoreConflict(f"run_id mismatch for session {session_id!r}: expected {expected_run_id!r}, got {curr_run_id!r}")
                if curr_revision_id != expected_revision_id:
                    raise StoreConflict(
                        f"active revision mismatch for session {session_id!r}: active is {curr_revision_id!r}, got {expected_revision_id!r}"
                    )
                if target_revision_id == expected_revision_id:
                    raise StoreConflict("the workspace already runs the target capability revision")

                blockers = self._activation_blockers_locked(workspace, session_id, exclude_claimed_in_session=True)
                if blockers:
                    raise StoreConflict(f"activation blocked: {'; '.join(blockers)}")

                self._connection.execute(
                    """
                    INSERT INTO revisions (revision_id, created_at, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (target_revision_id, now, target_text),
                )
                self._connection.execute(
                    """
                    INSERT INTO workspace_state (workspace, active_revision_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (workspace) DO UPDATE SET
                        active_revision_id = excluded.active_revision_id,
                        updated_at = excluded.updated_at
                    """,
                    (workspace, target_revision_id, now),
                )
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET active_revision_id = ?, updated_at = ?
                    WHERE workspace = ?
                    """,
                    (target_revision_id, now, workspace),
                )

                self._apply_state_updates(state_updates, default_revision_id=target_revision_id, now=now)

                for owner in cancelled_owners:
                    # A deactivated owner's generic constraints die with it: an orphaned
                    # restrictive overlay must never keep blocking ordinary chat. Session-keyed
                    # rows are narrowed to this workspace's sessions; a shared database must
                    # never lose another workspace's overlays to this switch.
                    self._connection.execute(
                        """
                        DELETE FROM session_overlays
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                    # A deactivated owner's projection disappears with it: the raw window
                    # becomes visible again instead of a summary nobody owns.
                    self._connection.execute(
                        """
                        DELETE FROM window_projections
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = 'cancelled', updated_at = ?
                        WHERE status IN ('pending', 'claimed') AND owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (now, owner, workspace),
                    )
                    self._connection.execute(
                        """
                        UPDATE background_actions
                        SET status = 'cancelled', updated_at = ?
                        WHERE status IN ('pending', 'claimed') AND owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (now, owner, workspace),
                    )
                    # A deactivated owner's pending wakeups die with it: a schedule nobody owns
                    # must never fire into the new generation. Claimed rows already have their
                    # run; settling them stays with that run's terminal path. Wakeups carry
                    # their own workspace column, so this narrows directly instead of joining
                    # sessions.
                    self._connection.execute(
                        """
                        UPDATE wakeups
                        SET status = 'cancelled', updated_at = ?
                        WHERE status = 'pending' AND owner = ? AND workspace = ?
                        """,
                        (now, owner, workspace),
                    )
                # Budgets die for cancelled owners AND for still-enabled owners the caller
                # explicitly clears (e.g. a budget reset across a generation that keeps the
                # owner): the union preserves that path, narrowed to this workspace.
                for owner in set(cancelled_owners) | set(cleared_budget_owners):
                    self._connection.execute(
                        """
                        DELETE FROM session_budgets
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                # Queued work that stays enabled keeps its provenance and shared root
                # budget, but is rebound to the revision that will execute it.
                self._connection.execute(
                    """
                    UPDATE continuations
                    SET revision_id = ?, updated_at = ?
                    WHERE session_id = ? AND status IN ('pending', 'claimed')
                    """,
                    (target_revision_id, now, session_id),
                )
                self._connection.execute(
                    """
                    UPDATE background_actions
                    SET revision_id = ?, updated_at = ?
                    WHERE session_id = ? AND status IN ('pending', 'claimed')
                    """,
                    (target_revision_id, now, session_id),
                )

                current_seq = curr_seq
                first_sequence_by_type: dict[str, int] = {}
                for draft in events:
                    if draft.event_type == EventType.WINDOW_PROJECTED:
                        projection = draft.payload
                        self.set_window_projection(
                            session_id,
                            str(projection["owner"]),
                            covered_from_sequence=cast(int, projection["covered_from_sequence"]),
                            covered_to_sequence=cast(int, projection["covered_to_sequence"]),
                            replacement=str(projection["replacement"]),
                            covered_bytes=cast(int, projection["covered_bytes"]),
                            dropped_bytes=cast(int, projection["dropped_bytes"]),
                        )
                    current_seq += 1
                    first_sequence_by_type.setdefault(draft.event_type, current_seq)
                    event_payload_json = canonical_json(draft.payload)
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            current_seq,
                            run_id,
                            draft.event_type,
                            draft.source,
                            draft.actor,
                            target_revision_id,
                            event_payload_json,
                            len(event_payload_json.encode("utf-8")),
                        ),
                    )
                    self._record_event_indexes(session_id, draft.event_type, draft.payload, now)

                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?, run_id = ?, updated_at = ?, last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (status, run_id, now, current_seq, session_id),
                )

                if checkpoint_kind is not None and checkpoint_payload is not None:
                    checkpoint_root_id, checkpoint_approval, checkpoint_ask_seq, checkpoint_ask_req = _checkpoint_index_values(checkpoint_payload)
                    self._connection.execute(
                        """
                        INSERT INTO checkpoints (
                            session_id, kind, revision_id, run_id, last_event_sequence, payload_json,
                            root_id, approval_sequence, ask_sequence, ask_request_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (session_id) DO UPDATE SET
                            kind = excluded.kind,
                            revision_id = excluded.revision_id,
                            run_id = excluded.run_id,
                            last_event_sequence = excluded.last_event_sequence,
                            payload_json = excluded.payload_json,
                            root_id = excluded.root_id,
                            approval_sequence = excluded.approval_sequence,
                            ask_sequence = excluded.ask_sequence,
                            ask_request_id = excluded.ask_request_id,
                            updated_at = excluded.updated_at
                        """,
                        (
                            session_id,
                            checkpoint_kind,
                            target_revision_id,
                            run_id,
                            current_seq,
                            canonical_json(checkpoint_payload),
                            checkpoint_root_id,
                            checkpoint_approval,
                            checkpoint_ask_seq,
                            checkpoint_ask_req,
                            now,
                        ),
                    )

                self._connection.execute("COMMIT")
                in_transaction = False
                return SessionRecord(
                    session_id=session_id,
                    workspace=workspace,
                    status=status,
                    active_revision_id=target_revision_id,
                    run_id=run_id,
                    turn=curr_turn,
                    last_event_sequence=current_seq,
                    root_session_id=curr_root,
                    parent_session_id=curr_parent,
                    depth=curr_depth,
                    tool_allowlist=_allowlist_from_json(curr_allowlist),
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def adopt_revision(
        self,
        *,
        workspace: str,
        session_id: str | None,
        expected_sequence: int | None,
        expected_run_id: str | None,
        expected_revision_id: str,
        target_revision_id: str,
        target_payload: Mapping[str, object],
        run_id: str,
        status: str | None,
        events: Sequence[EventDraft] = (),
        checkpoint_kind: Literal["ready", "approval_wait", "terminal", "interrupted"] | None = None,
        checkpoint_payload: Mapping[str, object] | None = None,
        state_updates: Sequence[StagedStateUpdate] = (),
        cancelled_owners: Sequence[str] = (),
        cleared_budget_owners: Sequence[str] = (),
        require_quiescent: bool,
        recover_orphaned_runs: bool = False,
        fact_observers: Mapping[str, Sequence[str]] | None = None,
    ) -> str:
        """One CAS transaction that adopts a new core generation for the workspace.

        Covers, atomically: the immutable revision row, the workspace pointer, EVERY session ref in
        the workspace, every migrated/deactivated owner state copy, cancelled work for dropped
        owners, retargeted queued work, and (when an acting session names the transition) that
        session's CAS, events, and checkpoint. An adoption without an acting session is allowed
        only when the workspace has no sessions at all.

        ``recover_orphaned_runs`` may be set only by a caller that proved no core owns the
        workspace (exclusive lease). Every other ``running`` row is then a dead run at its last
        committed boundary: it becomes ``interrupted`` with one durable fact, while its cursor,
        batch references, and replay policies are preserved, so an explicit resume still applies
        the ordinary interrupted-batch and unknown-usage settlement instead of replaying native
        work here. All other blockers keep refusing regardless of this flag.
        ``fact_observers`` is the target generation's fact-kind -> owners roster captured before
        this transaction: the recovery fact freezes it exactly as the engine stamps its own facts,
        so delivery eligibility never has to be guessed from a later generation.
        """
        target_text = canonical_json(target_payload)
        if session_id is None:
            if expected_sequence is not None or expected_run_id is not None:
                raise ValueError("adoption without an acting session must not carry session expectations")
        elif expected_sequence is None:
            raise ValueError("adoption with an acting session needs its expected sequence")
        if recover_orphaned_runs and fact_observers is None:
            raise ValueError("orphan recovery needs the target generation's fact observers")
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                now = int(time.time() * 1000)

                acting_was_running = False
                if session_id is None:
                    count = self._connection.execute("SELECT COUNT(*) FROM sessions WHERE workspace = ?", (workspace,)).fetchone()
                    if int(count[0]) != 0:
                        raise StoreConflict("adoption without an acting session requires a workspace with no sessions")
                else:
                    assert expected_sequence is not None
                    row = self._connection.execute(
                        """
                        SELECT workspace, status, active_revision_id, run_id, turn, last_event_sequence
                        FROM sessions
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    if row is None:
                        raise UnknownSession(f"session {session_id!r} does not exist", session_id)
                    row_workspace, curr_status, curr_revision_id, curr_run_id, _, curr_seq = row
                    if row_workspace != workspace:
                        raise StoreConflict(f"session {session_id!r} belongs to workspace {row_workspace!r}")
                    if curr_seq != expected_sequence:
                        raise SequenceConflict(f"sequence mismatch for session {session_id!r}: expected {expected_sequence}, got {curr_seq}")
                    if curr_run_id != expected_run_id:
                        raise StoreConflict(f"run_id mismatch for session {session_id!r}: expected {expected_run_id!r}, got {curr_run_id!r}")
                    if curr_revision_id != expected_revision_id:
                        raise StoreConflict(
                            f"active revision mismatch for session {session_id!r}: active is {curr_revision_id!r}, got {expected_revision_id!r}"
                        )
                    if target_revision_id == expected_revision_id:
                        raise StoreConflict("the workspace already runs the target capability revision")
                    if recover_orphaned_runs and curr_status == "running":
                        # The caller proved no core owns the workspace, so this acting row is an
                        # orphan too. It may adopt only as interrupted, keeping its cursor for an
                        # explicit resume instead of ending the transaction still "running".
                        if status != "interrupted" or checkpoint_kind not in (None, "interrupted"):
                            raise StoreConflict("an orphaned acting session must be adopted as interrupted")
                        acting_was_running = True

                # The revision row precedes orphan settlement: the durable recovery fact it
                # appends carries the identity this transaction is about to publish.
                self._connection.execute(
                    """
                    INSERT INTO revisions (revision_id, created_at, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (target_revision_id, now, target_text),
                )
                if recover_orphaned_runs:
                    assert fact_observers is not None
                    self._settle_orphaned_runs_locked(
                        workspace,
                        target_revision_id=target_revision_id,
                        acting_session_id=session_id,
                        fact_observers=fact_observers,
                        now=now,
                    )
                blockers = self._adoption_blockers_locked(
                    workspace,
                    include_parked=require_quiescent,
                    # The acting orphan is settled by the caller's explicit interrupted commit;
                    # it is still subject to every non-running blocker below.
                    exclude_running=session_id if acting_was_running else None,
                )
                if blockers:
                    joined = "; ".join(f"{sess} is blocked ({reason})" for sess, reason in blockers)
                    raise StoreConflict(f"adoption blocked: {joined}")

                self._connection.execute(
                    """
                    INSERT INTO workspace_state (workspace, active_revision_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (workspace) DO UPDATE SET
                        active_revision_id = excluded.active_revision_id,
                        updated_at = excluded.updated_at
                    """,
                    (workspace, target_revision_id, now),
                )
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET active_revision_id = ?, updated_at = ?
                    WHERE workspace = ?
                    """,
                    (target_revision_id, now, workspace),
                )

                self._apply_state_updates(state_updates, default_revision_id=target_revision_id, now=now)

                for owner in cancelled_owners:
                    self._connection.execute(
                        """
                        DELETE FROM session_overlays
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                    self._connection.execute(
                        """
                        DELETE FROM window_projections
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = 'cancelled', updated_at = ?
                        WHERE status IN ('pending', 'claimed') AND owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (now, owner, workspace),
                    )
                    self._connection.execute(
                        """
                        UPDATE background_actions
                        SET status = 'cancelled', updated_at = ?
                        WHERE status IN ('pending', 'claimed') AND owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (now, owner, workspace),
                    )
                    self._connection.execute(
                        """
                        UPDATE wakeups
                        SET status = 'cancelled', updated_at = ?
                        WHERE status = 'pending' AND owner = ? AND workspace = ?
                        """,
                        (now, owner, workspace),
                    )
                for owner in set(cancelled_owners) | set(cleared_budget_owners):
                    self._connection.execute(
                        """
                        DELETE FROM session_budgets
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                # Queued work that stays enabled keeps its provenance and shared root budget, but is
                # rebound to the revision that will execute it - every session, not just the acting one.
                self._connection.execute(
                    """
                    UPDATE continuations
                    SET revision_id = ?, updated_at = ?
                    WHERE status IN ('pending', 'claimed')
                      AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                    """,
                    (target_revision_id, now, workspace),
                )
                self._connection.execute(
                    """
                    UPDATE background_actions
                    SET revision_id = ?, updated_at = ?
                    WHERE status IN ('pending', 'claimed')
                      AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                    """,
                    (target_revision_id, now, workspace),
                )
                # Every resumable cursor moves with the generation: paused and parked checkpoints
                # keep their payload and run identity, but a candidate that refused a stale
                # checkpoint revision would strand them. Terminal history is never read again.
                self._connection.execute(
                    """
                    UPDATE checkpoints
                    SET revision_id = ?, updated_at = ?
                    WHERE kind <> 'terminal'
                      AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                    """,
                    (target_revision_id, now, workspace),
                )

                if session_id is not None:
                    assert status is not None
                    current_seq = curr_seq
                    for draft in events:
                        current_seq += 1
                        event_payload_json = canonical_json(draft.payload)
                        self._connection.execute(
                            """
                            INSERT INTO session_events (
                                session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                session_id,
                                current_seq,
                                run_id,
                                draft.event_type,
                                draft.source,
                                draft.actor,
                                target_revision_id,
                                event_payload_json,
                                len(event_payload_json.encode("utf-8")),
                            ),
                        )
                        self._record_event_indexes(session_id, draft.event_type, draft.payload, now)
                    self._connection.execute(
                        """
                        UPDATE sessions
                        SET status = ?, run_id = ?, updated_at = ?, last_event_sequence = ?
                        WHERE session_id = ?
                        """,
                        (status, run_id, now, current_seq, session_id),
                    )
                    if checkpoint_kind is not None and checkpoint_payload is not None:
                        checkpoint_root_id, checkpoint_approval, checkpoint_ask_seq, checkpoint_ask_req = _checkpoint_index_values(checkpoint_payload)
                        self._connection.execute(
                            """
                            INSERT INTO checkpoints (
                                session_id, kind, revision_id, run_id, last_event_sequence, payload_json,
                                root_id, approval_sequence, ask_sequence, ask_request_id, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (session_id) DO UPDATE SET
                                kind = excluded.kind,
                                revision_id = excluded.revision_id,
                                run_id = excluded.run_id,
                                last_event_sequence = excluded.last_event_sequence,
                                payload_json = excluded.payload_json,
                                root_id = excluded.root_id,
                                approval_sequence = excluded.approval_sequence,
                                ask_sequence = excluded.ask_sequence,
                                ask_request_id = excluded.ask_request_id,
                                updated_at = excluded.updated_at
                            """,
                            (
                                session_id,
                                checkpoint_kind,
                                target_revision_id,
                                run_id,
                                current_seq,
                                canonical_json(checkpoint_payload),
                                checkpoint_root_id,
                                checkpoint_approval,
                                checkpoint_ask_seq,
                                checkpoint_ask_req,
                                now,
                            ),
                        )

                self._connection.execute("COMMIT")
                in_transaction = False
                return target_revision_id
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _adoption_blockers_locked(self, workspace: str, *, include_parked: bool, exclude_running: str | None = None) -> list[tuple[str, str]]:
        """Work that must settle before an adoption commits; callers hold the connection lock.

        ``exclude_running`` names the one acting session an orphan recovery is already settling
        explicitly; it still appears through every other blocker category.
        """
        blockers: list[tuple[str, str]] = []
        running_query = "SELECT session_id FROM sessions WHERE workspace = ? AND status = 'running'"
        running_params: list[object] = [workspace]
        if exclude_running is not None:
            running_query += " AND session_id <> ?"
            running_params.append(exclude_running)
        cursor = self._connection.execute(running_query + " ORDER BY session_id ASC", tuple(running_params))
        blockers.extend((str(row[0]), "running") for row in cursor.fetchall())
        if include_parked:
            cursor = self._connection.execute(
                """
                SELECT session_id
                FROM sessions
                WHERE workspace = ? AND status = 'waiting'
                ORDER BY session_id ASC
                """,
                (workspace,),
            )
            blockers.extend((str(row[0]), "approval_wait") for row in cursor.fetchall())
            cursor = self._connection.execute(
                """
                SELECT session_id, request_id
                FROM action_confirmations
                WHERE status = 'pending'
                  AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                ORDER BY request_id ASC
                """,
                (workspace,),
            )
            blockers.extend((str(row[0]), "pending_confirmation") for row in cursor.fetchall())
        cursor = self._connection.execute(
            """
            SELECT session_id, continuation_id, owner
            FROM continuations
            WHERE status = 'claimed'
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY continuation_id ASC
            """,
            (workspace,),
        )
        blockers.extend((str(row[0]), "claimed_continuation") for row in cursor.fetchall())
        return blockers

    def _settle_orphaned_runs_locked(
        self,
        workspace: str,
        *,
        target_revision_id: str,
        acting_session_id: str | None,
        fact_observers: Mapping[str, Sequence[str]],
        now: int,
    ) -> None:
        """Settle dead runs of ONE workspace inside the caller's adoption transaction.

        Only ``running`` rows of ``workspace`` are touched, and only because the caller proved no
        core owns it. Each becomes ``interrupted`` at its last committed boundary with one
        ``core.run_interrupted`` fact. The checkpoint payload (cursor, batch references, replay
        policies) is preserved byte-for-byte: native effects and unsettled dispatches stay exactly
        as unknown as they were, so an explicit resume applies the ordinary replay-policy and
        unknown-usage settlement instead of this transaction inventing a replay or a stop.
        """
        query = "SELECT session_id, run_id, last_event_sequence FROM sessions WHERE workspace = ? AND status = 'running'"
        params: list[object] = [workspace]
        if acting_session_id is not None:
            query += " AND session_id <> ?"
            params.append(acting_session_id)
        rows = self._connection.execute(query + " ORDER BY session_id ASC", tuple(params)).fetchall()
        eligible_owners = list(fact_observers.get("run_finished", ()))
        for session_id, run_id, last_sequence in rows:
            if not isinstance(run_id, str) or not run_id:
                raise StoreConflict(f"running session {session_id!r} has no durable run identity")
            sequence = int(last_sequence) + 1
            logical_turn = ""
            checkpoint = self._connection.execute(
                "SELECT payload_json FROM checkpoints WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            if checkpoint is not None:
                payload = json.loads(str(checkpoint[0]))
                if isinstance(payload, dict) and isinstance(payload.get("logical_turn"), str):
                    logical_turn = payload["logical_turn"]
            orphan_payload_json = canonical_json(
                {
                    "reason": "the process that owned this run is gone; adoption recovered the session at its last committed boundary",
                    "stop_cause": "shutdown",
                    "logical_turn": logical_turn,
                    "eligible_owners": eligible_owners,
                }
            )
            self._connection.execute(
                """
                INSERT INTO session_events (
                    session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                ) VALUES (?, ?, ?, ?, 'core', 'core', ?, ?, ?)
                """,
                (
                    str(session_id),
                    sequence,
                    str(run_id),
                    EventType.RUN_INTERRUPTED,
                    target_revision_id,
                    orphan_payload_json,
                    len(orphan_payload_json.encode("utf-8")),
                ),
            )
            self._connection.execute(
                "UPDATE sessions SET status = 'interrupted', last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                (sequence, now, str(session_id)),
            )
            # The cursor is kept as-is; only its status and generation move with the adoption.
            self._connection.execute(
                "UPDATE checkpoints SET kind = 'interrupted', revision_id = ?, last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                (target_revision_id, sequence, now, str(session_id)),
            )

    def workspace_current(self, workspace: str) -> str | None:
        """The capability revision the workspace is currently running, or None before first adoption."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT active_revision_id FROM workspace_state WHERE workspace = ?",
                (workspace,),
            )
            row = cursor.fetchone()
            return None if row is None else str(row[0])

    def record_workspace_revision(self, workspace: str, revision_id: str) -> None:
        """Adopt a revision for a workspace without ever overwriting an existing pointer."""
        with self._lock:
            self._check_open()
            self._connection.execute(
                """
                INSERT INTO workspace_state (workspace, active_revision_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (workspace) DO NOTHING
                """,
                (workspace, revision_id, int(time.time() * 1000)),
            )

    def plugin_state_rows(self, scope: str, scope_key: str) -> tuple[PluginStateRow, ...]:
        """Every owner state row in one scope, with its core-owned version metadata."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT owner, scope, scope_key, payload_json, version, schema_version, revision_id
                FROM plugin_state
                WHERE scope = ? AND scope_key = ?
                ORDER BY owner ASC
                """,
                (scope, scope_key),
            )
            return tuple(
                PluginStateRow(
                    owner=str(row[0]),
                    scope=str(row[1]),
                    scope_key=str(row[2]),
                    payload=own_json_object(json.loads(row[3])),
                    version=int(row[4]),
                    schema_version=int(row[5]),
                    revision_id=str(row[6]),
                )
                for row in cursor.fetchall()
            )

    def _activation_blockers_locked(self, workspace: str, acting_session_id: str, *, exclude_claimed_in_session: bool) -> list[str]:
        """Blockers that must stop a capability switch; callers hold the connection lock."""
        blockers: list[str] = []
        cursor = self._connection.execute(
            """
            SELECT session_id, status
            FROM sessions
            WHERE workspace = ? AND session_id <> ? AND status IN ('running', 'waiting')
            ORDER BY session_id ASC
            """,
            (workspace, acting_session_id),
        )
        blockers.extend(f"session {row[0]} is {row[1]}" for row in cursor.fetchall())
        cursor = self._connection.execute(
            """
            SELECT session_id, request_id
            FROM action_confirmations
            WHERE status = 'pending' AND session_id <> ?
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY request_id ASC
            """,
            (acting_session_id, workspace),
        )
        blockers.extend(f"session {row[0]} awaits the decision for confirmation {row[1]}" for row in cursor.fetchall())
        cursor = self._connection.execute(
            """
            SELECT session_id, continuation_id, owner, status
            FROM continuations
            WHERE status IN ('pending', 'claimed')
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY continuation_id ASC
            """,
            (workspace,),
        )
        for row in cursor.fetchall():
            if row[0] == acting_session_id:
                # The acting session may keep its own queued work; it is retargeted
                # by the activation transaction instead of being rejected.
                continue
            blockers.append(f"session {row[0]} has a {row[3]} continuation {row[1]} owned by {row[2]}")
        cursor = self._connection.execute(
            """
            SELECT session_id, action_id, owner, status
            FROM background_actions
            WHERE status IN ('pending', 'claimed')
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY action_id ASC
            """,
            (workspace,),
        )
        for row in cursor.fetchall():
            if row[0] == acting_session_id:
                continue
            blockers.append(f"session {row[0]} has a {row[3]} continuation {row[1]} owned by {row[2]}")
        if exclude_claimed_in_session:
            cursor = self._connection.execute(
                """
                SELECT continuation_id, owner
                FROM continuations
                WHERE session_id = ? AND status = 'claimed'
                ORDER BY continuation_id ASC
                """,
                (acting_session_id,),
            )
            claimed = cursor.fetchall()
            if len(claimed) > 1:
                blockers.append(f"session {acting_session_id} has multiple claimed continuations")
        return blockers

    def activation_blockers(self, workspace: str, acting_session_id: str) -> tuple[str, ...]:
        """Read-only view of everything that must be idle before a capability switch."""
        with self._lock:
            self._check_open()
            return tuple(self._activation_blockers_locked(workspace, acting_session_id, exclude_claimed_in_session=False))

    def plugin_state(self, owner: str, scope: str, scope_key: str) -> tuple[Mapping[str, object] | None, int]:
        """Fetch current plugin state and version for the given owner, scope, and scope_key."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT payload_json, version
                FROM plugin_state
                WHERE scope = ? AND scope_key = ? AND owner = ?
                """,
                (scope, scope_key, owner),
            )
            row = cursor.fetchone()
            if row is None:
                return (None, 0)
            return (own_json_object(json.loads(row[0])), row[1])

    def confirmation(self, request_id: str) -> ActionConfirmationRecord | None:
        """Fetch an action confirmation record by request ID."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT request_id, session_id, owner, command_name, arguments_json, revision_id,
                       expected_state_version, ui_type, ui_data_json, status, created_at, decided_at
                FROM action_confirmations
                WHERE request_id = ?
                """,
                (request_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return ActionConfirmationRecord(
                request_id=row[0],
                session_id=row[1],
                owner=row[2],
                command_name=row[3],
                arguments=json.loads(row[4]),
                revision_id=row[5],
                expected_state_version=row[6],
                ui_type=row[7],
                ui_data=json.loads(row[8]),
                status=row[9],
                created_at=row[10],
                decided_at=row[11],
            )

    def decide_confirmation(self, request_id: str, decision: str) -> None:
        """Transition a pending confirmation record to allowed, denied, or stale."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            cursor = self._connection.execute(
                """
                UPDATE action_confirmations
                SET status = ?, decided_at = ?
                WHERE request_id = ? AND status = 'pending'
                """,
                (decision, now, request_id),
            )
            if cursor.rowcount == 0:
                raise UnknownConfirmation(f"confirmation {request_id!r} is not in pending state or does not exist", request_id)

    def enqueue_continuation(
        self,
        continuation_id: str,
        owner: str,
        session_id: str,
        source: str,
        run_id: str,
        revision_id: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        root_id: str | None = None,
        root_budget: int | None = None,
        role: str = "system",
        caller_chain: frozenset[str] | None = None,
        admission_guard: Callable[[], bool] | None = None,
    ) -> str:
        """Queue a continuation with its stable action-chain identity."""
        if source == BACKGROUND_ACTION_SOURCE:
            raise ValueError("enqueue_continuation is model-only; queue background actions with enqueue_background_action")
        if role not in ("system", "user"):
            raise ValueError("continuation role must be 'system' or 'user'")
        if not isinstance(root_id, str) or not root_id:
            raise StoreConflict("continuation requires a captured root action identity")
        payload_json = canonical_json(payload)
        with self._lock:
            self._check_open()
            guard = self._cancel_fence_guard
            if guard is not None and owner == CHILD_MESSAGE_OWNER and payload.get("kind") == "child_followup":
                chain = caller_chain
                if chain is None:
                    ancestors = self._connection.execute(
                        """
                        WITH RECURSIVE ancestors(session_id, parent_session_id) AS (
                            SELECT session_id, parent_session_id FROM sessions WHERE session_id = ?
                            UNION
                            SELECT s.session_id, s.parent_session_id FROM sessions s
                            JOIN ancestors a ON s.session_id = a.parent_session_id
                        )
                        SELECT session_id FROM ancestors
                        """,
                        (session_id,),
                    ).fetchall()
                    chain = frozenset(row[0] for row in ancestors)
                fenced = guard(chain, session_id)
                if fenced is not None:
                    raise FencedSubtree(fenced, session_id)
            if admission_guard is not None and not admission_guard():
                raise PermissionError("calling invocation is no longer active")
            now = int(time.time() * 1000)
            if idempotency_key is not None:
                cursor = self._connection.execute(
                    """
                    SELECT continuation_id, root_id, payload_json FROM continuations
                    WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                    """,
                    (session_id, owner, idempotency_key),
                )
                row = cursor.fetchone()
                if row is not None:
                    if row[1] != root_id or row[2] != payload_json:
                        raise StoreConflict("continuation idempotency key was already used with a different authorization or payload")
                    return row[0]
            continuation_size = len(payload_json.encode("utf-8"))
            if continuation_size > MAX_CONTINUATION_PAYLOAD_BYTES:
                raise StoreConflict(
                    f"continuation {continuation_id!r} payload is {continuation_size} bytes, "
                    f"exceeding the {MAX_CONTINUATION_PAYLOAD_BYTES}-byte continuation-payload bound"
                )
            if root_budget is not None:
                if type(root_budget) is not int or not 0 <= root_budget <= 9_223_372_036_854_775_807:
                    raise ValueError("root_budget must fit a non-negative SQLite INTEGER")
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO root_budgets (root_id, budget, reserved, created_at, updated_at)
                    VALUES (?, ?, 0, ?, ?)
                    """,
                    (root_id, root_budget, now, now),
                )
            try:
                self._connection.execute(
                    """
                    INSERT INTO continuations (
                        continuation_id, owner, session_id, source, kind, run_id, revision_id,
                        idempotency_key, root_id, role, status, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        continuation_id,
                        owner,
                        session_id,
                        source,
                        _continuation_kind(payload),
                        run_id,
                        revision_id,
                        idempotency_key,
                        root_id,
                        role,
                        payload_json,
                        now,
                        now,
                    ),
                )
                return continuation_id
            except sqlite3.IntegrityError as exc:
                raise StoreConflict(f"continuation {continuation_id!r} already exists") from exc

    def claim_child_messages(
        self,
        session_id: str,
        run_id: str,
        *,
        revision_id: str,
        limit: int = 8,
    ) -> tuple[StoredEvent, ...]:
        """Claim pending core child messages into the CURRENT running run.

        This is the steering boundary: the caller has just committed the whole native batch and
        is about to assemble the next provider request, so the message is appended to the same
        run and rendered by the next context assembly. Eligibility is checked in the same
        transaction as the claim: a session that is waiting, interrupted, terminal, on another
        revision, or driven by another run cannot consume the message here.
        """
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                row = self._connection.execute(
                    """
                    SELECT status, active_revision_id, run_id, last_event_sequence
                    FROM sessions WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise UnknownSession(f"session {session_id!r} does not exist", session_id)
                status, current_revision, current_run, sequence = row
                if status != "running" or current_run != run_id or current_revision != revision_id:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return ()
                rows = self._connection.execute(
                    """
                    SELECT continuation_id, source, payload_json, role
                    FROM continuations
                    WHERE session_id = ? AND status = 'pending' AND owner = ?
                      AND kind IN ('child_followup', 'child_finished')
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT ?
                    """,
                    (session_id, CHILD_MESSAGE_OWNER, max(1, limit)),
                ).fetchall()
                if not rows:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return ()
                now = int(time.time() * 1000)
                claimed: list[StoredEvent] = []
                for continuation_id, source, payload_json, role in rows:
                    # The appended event IS the delivery: the message now belongs to this run's
                    # committed prefix, so completing the row prevents any later drain from
                    # starting it again. A crash before the next dispatch resumes from the
                    # checkpoint with the event already in context.
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = 'completed', run_id = ?, updated_at = ?
                        WHERE continuation_id = ? AND status = 'pending'
                        """,
                        (run_id, now, continuation_id),
                    )
                    sequence += 1
                    payload = own_json_object(json.loads(payload_json))
                    child_payload_json = canonical_json({"continuation_id": continuation_id, "payload": payload, "role": str(role)})
                    self._connection.execute(
                        """
                    INSERT INTO session_events (
                        session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                    ) VALUES (?, ?, ?, ?, ?, 'plugin', ?, ?, ?)
                    """,
                        (
                            session_id,
                            sequence,
                            run_id,
                            EventType.CONTINUATION_STARTED,
                            source,
                            revision_id,
                            child_payload_json,
                            len(child_payload_json.encode("utf-8")),
                        ),
                    )
                    self._record_event_indexes(session_id, EventType.CONTINUATION_STARTED, {"continuation_id": continuation_id}, now)
                    claimed.append(
                        StoredEvent(
                            session_id=session_id,
                            sequence=sequence,
                            run_id=run_id,
                            event_type=EventType.CONTINUATION_STARTED,
                            source=source,
                            actor="plugin",
                            revision_id=revision_id,
                            payload={"continuation_id": continuation_id, "payload": payload, "role": str(role)},
                        )
                    )
                self._connection.execute(
                    "UPDATE sessions SET last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                    (sequence, now, session_id),
                )
                # The appended messages are part of the resumable prefix: a crash before the next
                # provider request must render them, not reject them as intervening events.
                self._connection.execute(
                    "UPDATE checkpoints SET last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                    (sequence, now, session_id),
                )
                self._connection.execute("COMMIT")
                in_transaction = False
                return tuple(claimed)
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def ensure_root_budget(self, root_id: str, budget: int | None) -> None:
        """Record a finite root action authorization once; `None` means no action cap."""
        if budget is None:
            return
        if type(budget) is not int or not 0 <= budget <= 9_223_372_036_854_775_807:
            raise ValueError("root_action_budget must fit a non-negative SQLite INTEGER")
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO root_budgets (root_id, budget, reserved, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (root_id, budget, now, now),
            )

    def root_budget_state(self, root_id: str) -> tuple[int, int] | None:
        """Return (budget, reserved) for a root action chain, or None when unknown."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT budget, reserved FROM root_budgets WHERE root_id = ?",
                (root_id,),
            )
            row = cursor.fetchone()
            return None if row is None else (int(row[0]), int(row[1]))

    def continuation_id_for_idempotency(self, session_id: str, owner: str, idempotency_key: str) -> str | None:
        """Return the durable model-continuation id already recorded for an idempotency key.

        Background actions live in their own table with their own namespace: the same
        owner/session/key may name one model message AND one background action, and
        neither lookup may swallow the other (ordinary keys are arbitrary strings).
        """
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id FROM continuations
                WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                """,
                (session_id, owner, idempotency_key),
            )
            row = cursor.fetchone()
            return None if row is None else str(row[0])

    def continuation_status(self, continuation_id: str) -> str | None:
        """Return the durable status of one continuation row, or None when absent."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT status FROM continuations WHERE continuation_id = ?",
                (continuation_id,),
            )
            row = cursor.fetchone()
            return None if row is None else str(row[0])

    def continuation_owner(self, continuation_id: str) -> str | None:
        """Return the owning plugin of one continuation row, or None when absent."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT owner FROM continuations WHERE continuation_id = ?",
                (continuation_id,),
            )
            row = cursor.fetchone()
            return None if row is None else str(row[0])

    def pending_continuations(
        self,
        session_id: str,
        limit: int = 1,
    ) -> tuple[ContinuationRecord, ...]:
        """Fetch pending model continuations in order of creation (never background actions)."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                       idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                FROM continuations
                WHERE session_id = ? AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, max(1, limit)),
            )
            rows = cursor.fetchall()
            return tuple(
                ContinuationRecord(
                    continuation_id=row[0],
                    owner=row[1],
                    session_id=row[2],
                    source=row[3],
                    run_id=row[4],
                    revision_id=row[5],
                    idempotency_key=row[6],
                    status=row[8],
                    payload=own_json_object(json.loads(row[9])),
                    created_at=row[10],
                    updated_at=row[11],
                    root_id=row[7],
                    role=str(row[12]),
                )
                for row in rows
            )

    def claimed_continuations(
        self,
        session_id: str,
        limit: int = 100,
    ) -> tuple[ContinuationRecord, ...]:
        """Fetch previously claimed model continuations for recovery (never background actions)."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                       idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                FROM continuations
                WHERE session_id = ? AND status = 'claimed'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, max(1, limit)),
            )
            rows = cursor.fetchall()
            return tuple(
                ContinuationRecord(
                    continuation_id=row[0],
                    owner=row[1],
                    session_id=row[2],
                    source=row[3],
                    run_id=row[4],
                    revision_id=row[5],
                    idempotency_key=row[6],
                    status=row[8],
                    payload=own_json_object(json.loads(row[9])),
                    created_at=row[10],
                    updated_at=row[11],
                    root_id=row[7],
                    role=str(row[12]),
                )
                for row in rows
            )

    def complete_continuation(self, continuation_id: str, status: Literal["completed", "failed", "cancelled"] = "completed") -> None:
        """Mark a claimed continuation as completed, failed, or cancelled."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            cursor = self._connection.execute(
                """
                UPDATE continuations
                SET status = ?, updated_at = ?
                WHERE continuation_id = ?
                """,
                (status, now, continuation_id),
            )
            if cursor.rowcount == 0:
                raise UnknownContinuation(f"continuation {continuation_id!r} does not exist", continuation_id)

    def claim_and_start_continuation(
        self,
        session_id: str,
        run_id: str,
        logical_turn: str,
        *,
        revision_id: str,
        caller_chain: frozenset[str],
        initial_root_budget: int | None = None,
        max_steps: int | None = None,
        continuation_id: str | None = None,
        automatic: bool = False,
    ) -> tuple[ContinuationRecord, SessionRecord]:
        """Atomically claim ONE continuation, emit continuation_started, advance session, and commit ready CP.

        With `continuation_id`, only that exact row is eligible: a caller that reserved an
        identity before enqueuing never starts somebody else's older message.
        """
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True

                cursor = self._connection.execute(
                    """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                           parent_session_id, root_session_id, depth, tool_allowlist_json
                    FROM sessions
                    WHERE session_id = ?
                    """,
                    (session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise UnknownSession(f"session {session_id!r} does not exist", session_id)
                (_, workspace, status, curr_revision_id, curr_run_id, curr_turn, curr_seq, curr_parent, curr_root, curr_depth, curr_allowlist) = row
                if curr_revision_id != revision_id:
                    raise StoreConflict(f"active revision mismatch for session {session_id!r}: active is {curr_revision_id!r}, got {revision_id!r}")

                checkpoint_row = self._connection.execute(
                    "SELECT kind, run_id, payload_json FROM checkpoints WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                now = int(time.time() * 1000)

                c_cursor = self._connection.execute(
                    """
                    SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                           idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                    FROM continuations
                    WHERE session_id = ? AND status = 'pending'
                      AND owner <> 'kernel'
                      AND (? IS NULL OR continuation_id = ?)
                      AND NOT (? AND owner = ? AND COALESCE(kind, '') = 'child_finished')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (session_id, continuation_id, continuation_id, automatic and status == "interrupted", CHILD_MESSAGE_OWNER),
                )
                cont_row = c_cursor.fetchone()
                guard = self._cancel_fence_guard
                if guard is not None and cont_row is not None and cont_row[1] == CHILD_MESSAGE_OWNER:
                    fenced = guard(caller_chain, session_id)
                    if fenced is not None:
                        raise FencedSubtree(fenced, session_id)
                if cont_row is None:
                    c_cursor2 = self._connection.execute(
                        """
                        SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                               idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                        FROM continuations
                        WHERE session_id = ? AND status = 'claimed'
                          AND owner <> 'kernel'
                          AND (? IS NULL OR continuation_id = ?)
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (session_id, continuation_id, continuation_id),
                    )
                    claimed_row = c_cursor2.fetchone()
                    if claimed_row is None:
                        raise StoreConflict(f"no pending or claimed continuations for session {session_id!r}")
                    cont_row = claimed_row
                    if checkpoint_row is None:
                        raise StoreConflict(f"claimed continuation {cont_row[0]!r} has no resumable checkpoint")
                    cp_kind, cp_run_id, cp_payload_json = checkpoint_row
                    if cp_kind == "terminal":
                        raise StoreConflict(f"claimed continuation {cont_row[0]!r} has a terminal checkpoint")
                    try:
                        cp_payload = own_json_object(json.loads(cp_payload_json))
                    except (TypeError, ValueError) as exc:
                        raise StoreConflict(f"claimed continuation {cont_row[0]!r} has a malformed checkpoint") from exc
                    if (
                        str(cp_run_id) != str(cont_row[4])
                        or str(cp_payload.get("continuation_id")) != str(cont_row[0])
                        or not isinstance(cont_row[7], str)
                        or not cont_row[7]
                        or cp_payload.get("root_id") != cont_row[7]
                    ):
                        raise StoreConflict(f"claimed continuation {cont_row[0]!r} does not own its checkpoint/root action identity")
                    claimed_record = ContinuationRecord(
                        continuation_id=cont_row[0],
                        owner=cont_row[1],
                        session_id=cont_row[2],
                        source=cont_row[3],
                        run_id=cont_row[4],
                        revision_id=cont_row[5],
                        idempotency_key=cont_row[6],
                        status="claimed",
                        payload=own_json_object(json.loads(cont_row[9])),
                        created_at=cont_row[10],
                        updated_at=cont_row[11],
                        root_id=cont_row[7],
                        role=str(cont_row[12]),
                    )
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return claimed_record, SessionRecord(
                        session_id=session_id,
                        workspace=workspace,
                        status=status,
                        active_revision_id=curr_revision_id,
                        run_id=curr_run_id,
                        turn=curr_turn,
                        last_event_sequence=curr_seq,
                        root_session_id=curr_root,
                        parent_session_id=curr_parent,
                        depth=curr_depth,
                        tool_allowlist=_allowlist_from_json(curr_allowlist),
                    )

                if not isinstance(cont_row[7], str) or not cont_row[7]:
                    raise StoreConflict(f"continuation {cont_row[0]!r} has no captured root action identity")
                if status not in ("idle", "completed", "failed", "interrupted"):
                    raise StoreConflict(f"session {session_id!r} already holds active work ({status})")
                if status == "idle" and curr_parent is not None:
                    # A spawned child is idle only before its first run commits; its first
                    # boundary merges the queued follow-up instead of racing a second start.
                    raise StoreConflict(f"child session {session_id!r} has not committed its spawn run")
                if checkpoint_row is not None and checkpoint_row[0] not in ("terminal",):
                    raise StoreConflict(f"session {session_id!r} holds a resumable/parked checkpoint ({checkpoint_row[0]})")
                claimed_other = self._connection.execute(
                    "SELECT 1 FROM continuations WHERE session_id = ? AND status = 'claimed' LIMIT 1",
                    (session_id,),
                ).fetchone()
                if claimed_other is not None:
                    raise StoreConflict(f"session {session_id!r} already has claimed work")
                cid = cont_row[0]
                self._connection.execute(
                    """
                    UPDATE continuations
                    SET status = 'claimed', run_id = ?, updated_at = ?
                    WHERE continuation_id = ?
                    """,
                    (run_id, now, cid),
                )

                cont_record = ContinuationRecord(
                    continuation_id=cont_row[0],
                    owner=cont_row[1],
                    session_id=cont_row[2],
                    source=cont_row[3],
                    run_id=run_id,
                    revision_id=revision_id,
                    idempotency_key=cont_row[6],
                    status="claimed",
                    payload=own_json_object(json.loads(cont_row[9])),
                    created_at=cont_row[10],
                    updated_at=now,
                    root_id=cont_row[7],
                    role=str(cont_row[12]),
                )

                new_seq = curr_seq + 1
                disp_payload = canonical_json(
                    {"continuation_id": cont_record.continuation_id, "payload": cont_record.payload, "role": cont_record.role}
                )
                self._connection.execute(
                    """
                    INSERT INTO session_events (
                        session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                    ) VALUES (?, ?, ?, ?, ?, 'plugin', ?, ?, ?)
                    """,
                    (
                        session_id,
                        new_seq,
                        run_id,
                        EventType.CONTINUATION_STARTED,
                        f"plugin.{cont_record.owner}",
                        revision_id,
                        disp_payload,
                        len(disp_payload.encode("utf-8")),
                    ),
                )
                self._record_event_indexes(session_id, EventType.CONTINUATION_STARTED, {"continuation_id": cont_record.continuation_id}, now)

                new_turn = curr_turn + 1
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'running',
                        run_id = ?,
                        turn = ?,
                        updated_at = ?,
                        last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (run_id, new_turn, now, new_seq, session_id),
                )

                payload_max_steps = cont_record.payload.get("max_steps")
                if payload_max_steps is not None and (type(payload_max_steps) is not int or payload_max_steps < 1):
                    raise StoreConflict(f"continuation {cont_record.continuation_id!r} has an invalid max_steps cap")
                if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
                    raise ValueError("max_steps must be a positive integer or null")
                effective_steps = payload_max_steps
                if max_steps is not None:
                    effective_steps = max_steps if effective_steps is None else min(effective_steps, max_steps)
                checkpoint_payload = {
                    "step": 0,
                    "continuation_id": cont_record.continuation_id,
                    "root_id": cont_record.root_id,
                    "logical_turn": logical_turn,
                    "initial_root_budget": initial_root_budget,
                    "max_steps": effective_steps,
                }
                checkpoint_json = canonical_json(checkpoint_payload)
                checkpoint_root_id, checkpoint_approval, checkpoint_ask_seq, checkpoint_ask_req = _checkpoint_index_values(checkpoint_payload)
                self._connection.execute(
                    """
                    INSERT INTO checkpoints (
                        session_id, kind, revision_id, run_id, last_event_sequence, payload_json,
                        root_id, approval_sequence, ask_sequence, ask_request_id, updated_at
                    ) VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (session_id) DO UPDATE SET
                        kind = excluded.kind,
                        revision_id = excluded.revision_id,
                        run_id = excluded.run_id,
                        last_event_sequence = excluded.last_event_sequence,
                        payload_json = excluded.payload_json,
                        root_id = excluded.root_id,
                        approval_sequence = excluded.approval_sequence,
                        ask_sequence = excluded.ask_sequence,
                        ask_request_id = excluded.ask_request_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        revision_id,
                        run_id,
                        new_seq,
                        checkpoint_json,
                        checkpoint_root_id,
                        checkpoint_approval,
                        checkpoint_ask_seq,
                        checkpoint_ask_req,
                        now,
                    ),
                )

                self._connection.execute("COMMIT")
                in_transaction = False
                return cont_record, SessionRecord(
                    session_id=session_id,
                    workspace=workspace,
                    status="running",
                    active_revision_id=revision_id,
                    run_id=run_id,
                    root_session_id=curr_root,
                    parent_session_id=curr_parent,
                    depth=curr_depth,
                    tool_allowlist=_allowlist_from_json(curr_allowlist),
                    turn=new_turn,
                    last_event_sequence=new_seq,
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def session_budgets(self, session_id: str) -> tuple[PluginBudget, ...]:
        """Fetch all active budget constraints for a session."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT owner, since_sequence, max_input_tokens, max_output_tokens, max_total_tokens, max_cost_usd, max_elapsed_seconds
                FROM session_budgets
                WHERE session_id = ?
                """,
                (session_id,),
            )
            rows = cursor.fetchall()
            return tuple(
                PluginBudget(
                    owner=r[0],
                    since_sequence=r[1],
                    max_input_tokens=r[2],
                    max_output_tokens=r[3],
                    max_total_tokens=r[4],
                    max_cost_usd=r[5],
                    max_elapsed_seconds=r[6],
                )
                for r in rows
            )

    def owner_budget(self, session_id: str, owner: str) -> PluginBudget | None:
        """Fetch active budget constraint for the bound owner in a session."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT owner, since_sequence, max_input_tokens, max_output_tokens, max_total_tokens, max_cost_usd, max_elapsed_seconds
                FROM session_budgets
                WHERE session_id = ? AND owner = ?
                """,
                (session_id, owner),
            )
            r = cursor.fetchone()
            if r is None:
                return None
            return PluginBudget(
                owner=r[0],
                since_sequence=r[1],
                max_input_tokens=r[2],
                max_output_tokens=r[3],
                max_total_tokens=r[4],
                max_cost_usd=r[5],
                max_elapsed_seconds=r[6],
            )

    def has_observer_receipt(self, session_id: str, event_sequence: int, owner: str) -> bool:
        """Whether this (source fact event, owner) pair was already delivered exactly once."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT 1 FROM observer_receipts
                WHERE session_id = ? AND event_sequence = ? AND owner = ?
                """,
                (session_id, event_sequence, owner),
            )
            return cursor.fetchone() is not None

    def continuation_started(self, session_id: str, continuation_id: str) -> bool:
        """Whether the exact continuation input was committed into this session's event transcript."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT 1 FROM continuation_starts
                WHERE session_id = ? AND continuation_id = ?
                LIMIT 1
                """,
                (session_id, continuation_id),
            ).fetchone()
            return row is not None

    def cancel_pending_continuations(
        self, session_id: str, owner: str, *, continuation_id: str | None = None, expected_session: SessionRecord | None = None
    ) -> int:
        """Cancel pending owned work, optionally pinned to the session's observed run."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            cursor = self._connection.execute(
                """
                UPDATE continuations
                SET status = 'cancelled', updated_at = ?
                WHERE session_id = ? AND owner = ? AND status = 'pending'
                  AND (? IS NULL OR continuation_id = ?)
                  AND (? OR EXISTS (
                      SELECT 1 FROM sessions s WHERE s.session_id = ? AND s.run_id IS ?
                  ))
                """,
                (
                    now,
                    session_id,
                    owner,
                    continuation_id,
                    continuation_id,
                    expected_session is None,
                    session_id,
                    expected_session.run_id if expected_session is not None else None,
                ),
            )
            return cursor.rowcount

    def enqueue_background_action(
        self,
        action_id: str,
        owner: str,
        session_id: str,
        revision_id: str,
        action: Mapping[str, object],
        *,
        root_id: str,
        root_action_budget: int | None = None,
        authority: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Queue one background action with its captured root authorization (direct insert)."""
        if not isinstance(root_id, str) or not root_id:
            raise StoreConflict(f"background action {action_id!r} has no captured root authorization")
        if root_action_budget is not None and (type(root_action_budget) is not int or not 0 <= root_action_budget <= 9_223_372_036_854_775_807):
            raise StoreConflict(f"background action {action_id!r} has an invalid captured root budget")
        action_json = canonical_json(action)
        action_size = len(action_json.encode("utf-8"))
        if action_size > MAX_CONTINUATION_PAYLOAD_BYTES:
            raise StoreConflict(
                f"background action {action_id!r} payload is {action_size} bytes, "
                f"exceeding the {MAX_CONTINUATION_PAYLOAD_BYTES}-byte continuation-payload bound"
            )
        authority_json = None if authority is None else canonical_json(authority)
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            if idempotency_key is not None:
                row = self._connection.execute(
                    """
                    SELECT action_id, root_id, payload_json, authority_json, root_action_budget FROM background_actions
                    WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                    """,
                    (session_id, owner, idempotency_key),
                ).fetchone()
                if row is not None:
                    if row[1] != root_id or row[2] != action_json or row[3] != authority_json or row[4] != root_action_budget:
                        raise StoreConflict("background action idempotency key was already used with a different authorization or payload")
                    return str(row[0])
            try:
                self._connection.execute(
                    """
                    INSERT INTO background_actions (
                        action_id, owner, session_id, revision_id, idempotency_key, root_id,
                        root_action_budget, status, payload_json, authority_json, claim_json,
                        error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        action_id,
                        owner,
                        session_id,
                        revision_id,
                        idempotency_key,
                        root_id,
                        root_action_budget,
                        action_json,
                        authority_json,
                        now,
                        now,
                    ),
                )
                return action_id
            except sqlite3.IntegrityError as exc:
                raise StoreConflict(f"background action {action_id!r} already exists") from exc

    def claim_background_action(self, *, workspace: str, claim_identity: Mapping[str, object]) -> ContinuationRecord | None:
        """Claim ONE pending background action of this workspace; its driver runs it, not a model turn.

        The `claim_identity` records who holds the claim in the claim column; the
        action payload itself is never rewritten by the claim.
        """
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                row = self._connection.execute(
                    """
                    SELECT action_id, owner, session_id, revision_id, idempotency_key, root_id,
                           root_action_budget, status, payload_json, authority_json, claim_json, error,
                           created_at, updated_at
                    FROM background_actions
                    WHERE status = 'pending'
                      AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (workspace,),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return None
                now = int(time.time() * 1000)
                root_id = row[5]
                if not isinstance(root_id, str) or not root_id:
                    raise StoreConflict(f"background action {row[0]!r} has no captured root authorization")
                root_budget = row[6]
                if root_budget is not None and (type(root_budget) is not int or root_budget < 0):
                    raise StoreConflict(f"background action {row[0]!r} has an invalid captured root budget")
                budget_row = self._connection.execute(
                    "SELECT budget FROM root_budgets WHERE root_id = ?",
                    (root_id,),
                ).fetchone()
                if root_budget is None and budget_row is not None:
                    raise StoreConflict(f"background action {row[0]!r} captured an uncapped root with a finite budget row")
                if root_budget is not None and (budget_row is None or int(budget_row[0]) < root_budget):
                    raise StoreConflict(f"background action {row[0]!r} lost its captured finite root authorization")
                claimed = self._connection.execute(
                    """
                    UPDATE background_actions SET status = 'claimed', claim_json = ?, updated_at = ?
                    WHERE action_id = ? AND status = 'pending'
                    """,
                    (canonical_json(claim_identity), now, row[0]),
                )
                if claimed.rowcount == 0:
                    raise StoreConflict(f"background action {row[0]!r} was claimed concurrently")
                self._connection.execute("COMMIT")
                in_transaction = False
                return _background_record(row, status="claimed")
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def complete_background_action(self, action_id: str, *, status: Literal["completed", "failed", "cancelled"], error: str | None = None) -> None:
        """Settle a pending or claimed background action into its terminal state.

        A failed settle records the driver's error in the error column, leaving the
        owner's action payload byte-identical: business payloads are arbitrary JSON
        and must never be rewritten or misread as execution errors.
        """
        with self._lock:
            self._check_open()
            if status not in ("completed", "failed", "cancelled"):
                raise ValueError(f"background action completion must be terminal, got {status!r}")
            cursor = self._connection.execute(
                """
                UPDATE background_actions SET status = ?, updated_at = ?, error = ?
                WHERE action_id = ? AND status IN ('pending', 'claimed')
                """,
                (status, int(time.time() * 1000), error if status == "failed" else None, action_id),
            )
            if cursor.rowcount == 0:
                raise StoreConflict(f"background action {action_id!r} is not settleable")

    def background_action(self, action_id: str) -> ContinuationRecord | None:
        """One background action row by id, or None when absent."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT action_id, owner, session_id, revision_id, idempotency_key, root_id,
                       root_action_budget, status, payload_json, authority_json, claim_json, error,
                       created_at, updated_at
                FROM background_actions
                WHERE action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if row is None:
                return None
            return _background_record(row)

    def background_action_id_for_idempotency(self, session_id: str, owner: str, idempotency_key: str) -> str | None:
        """The durable background action id already recorded for an idempotency key."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT action_id FROM background_actions
                WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                """,
                (session_id, owner, idempotency_key),
            ).fetchone()
            return None if row is None else str(row[0])

    def pending_background_actions(self, session_id: str) -> tuple[ContinuationRecord, ...]:
        """Every pending or claimed background action of one session, oldest first."""
        with self._lock:
            self._check_open()
            rows = self._connection.execute(
                """
                SELECT action_id, owner, session_id, revision_id, idempotency_key, root_id,
                       root_action_budget, status, payload_json, authority_json, claim_json, error,
                       created_at, updated_at
                FROM background_actions
                WHERE session_id = ? AND status IN ('pending', 'claimed')
                ORDER BY created_at ASC
                LIMIT 64
                """,
                (session_id,),
            ).fetchall()
            return tuple(_background_record(row) for row in rows)

    def claim_pending_followup(
        self,
        session_id: str,
    ) -> tuple[str, str, int | None, list[dict[str, object]], str | None, int | None] | None:
        """Atomically claim a kernel follow-up with its captured root authorization."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id, payload_json, root_id
                FROM continuations
                WHERE session_id = ? AND status = 'pending' AND owner = 'kernel' AND source = 'user_message'
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cid, payload_json, root_id = row
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise StoreConflict(f"queued follow-up {cid!r} has invalid JSON payload") from exc
            if not isinstance(payload, Mapping) or "root_action_budget" not in payload:
                raise StoreConflict(f"queued follow-up {cid!r} has no captured root budget")
            if not isinstance(root_id, str) or not root_id:
                raise StoreConflict(f"queued follow-up {cid!r} has no captured root action identity")
            root_budget = payload["root_action_budget"]
            if root_budget is not None and (type(root_budget) is not int or root_budget < 0):
                raise StoreConflict(f"queued follow-up {cid!r} has an invalid captured root budget")
            prompt = payload.get("prompt")
            if not isinstance(prompt, str):
                raise StoreConflict(f"queued follow-up {cid!r} has no prompt")
            max_steps_value = payload.get("max_steps")
            if max_steps_value is not None and type(max_steps_value) is not int:
                raise StoreConflict(f"queued follow-up {cid!r} has an invalid provider-turn cap")
            steps = max_steps_value
            raw_images = payload.get("images")
            images: list[dict[str, object]] = (
                [dict(img) for img in raw_images if isinstance(img, Mapping)] if isinstance(raw_images, (list, tuple)) else []
            )
            now = int(time.time() * 1000)
            claimed = self._connection.execute(
                "UPDATE continuations SET status = 'claimed', updated_at = ? WHERE continuation_id = ? AND status = 'pending'",
                (now, cid),
            )
            if claimed.rowcount == 0:
                raise StoreConflict(f"queued follow-up {cid!r} was claimed concurrently")
            return cid, prompt, steps, images, root_id, root_budget

    def set_continuation_run_id(self, continuation_id: str, run_id: str) -> None:
        """Record the active run_id for a claimed continuation."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            self._connection.execute(
                "UPDATE continuations SET run_id = ?, updated_at = ? WHERE continuation_id = ?",
                (run_id, now, continuation_id),
            )
