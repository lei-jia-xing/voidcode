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

from ...sdk.json_values import json_wire_object, own_json_object
from ...sdk.services import PluginBudget, SessionInfo
from ..events import CHILD_MESSAGE_OWNER, EventType

SCHEMA_MAGIC = "voidcode-kernel-v1"
SCHEMA_VERSION = 6


class StorageError(RuntimeError):
    """Base error for core storage failures."""


class ForeignDatabaseError(StorageError):
    """Raised when an existing database is foreign, empty, legacy, or corrupt."""


class StoreConflict(StorageError):
    """Raised when an optimistic concurrency check fails or state is stale."""


_WAKEUP_PAYLOAD_KEY = "_voidcode_core_wakeup"


def _decode_wakeup_payload(value: object, wakeup_id: str) -> tuple[Mapping[str, object], str, int | None]:
    if not isinstance(value, Mapping):
        raise StoreConflict(f"wakeup {wakeup_id!r} has no captured root authorization")
    stored = own_json_object(value)
    envelope = stored.get(_WAKEUP_PAYLOAD_KEY)
    if not isinstance(envelope, Mapping):
        raise StoreConflict(f"wakeup {wakeup_id!r} has no captured root authorization")
    root_id = envelope.get("root_id")
    root_budget = envelope.get("root_action_budget")
    payload = envelope.get("payload")
    if "root_action_budget" not in envelope or not isinstance(root_id, str) or not root_id or "\0" in root_id or not isinstance(payload, Mapping):
        raise StoreConflict(f"wakeup {wakeup_id!r} has a malformed root authorization envelope")
    if root_budget is None:
        validated_budget = None
    elif type(root_budget) is int and root_budget >= 0:
        validated_budget = root_budget
    else:
        raise StoreConflict(f"wakeup {wakeup_id!r} has a malformed root authorization envelope")
    return own_json_object(payload), root_id, validated_budget


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


class FencedSubtree(StoreConflict):
    """A child registration or follow-up enqueue landed inside a fenced subtree."""

    def __init__(self, fence: str, session_id: str) -> None:
        super().__init__(f"session {session_id!r} is inside subtree {fence!r} under cancellation")
        self.fence = fence
        self.session_id = session_id


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


#: `continuations.source` value marking one row as a plugin-owned background action. The
#: discriminator is the core-controlled column, never `payload.kind`: ordinary model
#: continuations accept arbitrary JSON and must pass through untouched.
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


@dataclass(frozen=True, slots=True)
class StagedBudgetUpdate:
    owner: str
    action: Literal["set", "clear"]
    budget: PluginBudget | None = None


@dataclass(frozen=True, slots=True)
class ObserverReceiptRecord:
    event_sequence: int
    owner: str


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
    allowlist = _allowlist_from_json(row[10] if len(row) > 10 else None)
    root = row[8] if len(row) > 8 and isinstance(row[8], str) and row[8] else str(row[0])
    parent = row[7] if len(row) > 7 and isinstance(row[7], str) and row[7] else None
    depth = row[9] if len(row) > 9 and isinstance(row[9], int) else 0
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


def _load_schema_sql() -> str:
    return files("voidcode.core.storage").joinpath("schema.sql").read_text(encoding="utf-8")


def _verify_existing_file(path: Path, *, supported: frozenset[int] | None = None, enforce_version: bool = True) -> int:
    """Verify an existing file without altering a single byte.

    Returns the recorded schema version. ``supported`` names every version this caller
    understands (tables are checked after the version, so a recorded version that is not
    supported refuses before a single table is read). Defaults to the current version only.
    With ``enforce_version=False`` the magic and table matrix are still verified, but any
    recorded version passes and the caller classifies it.
    """
    if not path.is_file():
        raise ForeignDatabaseError(f"database path exists but is not a regular file: {path}")
    if path.stat().st_size == 0:
        raise ForeignDatabaseError(f"refusing pre-existing empty database file: {path}")

    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        probe = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ForeignDatabaseError(f"unsupported database file: {path}") from exc

    expected = frozenset({SCHEMA_VERSION}) if supported is None else supported
    try:
        cursor = probe.execute("SELECT magic, schema_version FROM kernel_identity WHERE id = 1")
        row = cursor.fetchone()
        if row is None or row[0] != SCHEMA_MAGIC or (enforce_version and row[1] not in expected):
            raise ForeignDatabaseError(
                f"database is not a valid voidcode database: unsupported identity in {path}: "
                f"expected ({SCHEMA_MAGIC!r}, {sorted(expected)}), got {row!r}"
            )
        recorded = int(row[1])
        cursor = probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cursor.fetchall()}
        required = {
            "kernel_identity",
            "revisions",
            "sessions",
            "session_events",
            "checkpoints",
            "plugin_state",
            "action_confirmations",
            "continuations",
            "session_budgets",
            "observer_receipts",
            "root_budgets",
            "workspace_state",
            "wakeups",
            "session_overlays",
            "window_projections",
        }
        if not required.issubset(tables):
            raise ForeignDatabaseError(f"unsupported database file or missing tables in {path}: missing {required - tables}")
        cursor = probe.execute("PRAGMA table_info(plugin_state)")
        plugin_state_cols = {r[1] for r in cursor.fetchall()}
        required_plugin_state_cols = {
            "scope",
            "scope_key",
            "owner",
            "version",
            "schema_version",
            "revision_id",
            "payload_json",
            "updated_at",
        }
        if not required_plugin_state_cols.issubset(plugin_state_cols):
            raise ForeignDatabaseError(f"unsupported plugin_state schema in {path}: missing {required_plugin_state_cols - plugin_state_cols}")

        cursor = probe.execute("PRAGMA table_info(continuations)")
        continuation_cols = {r[1] for r in cursor.fetchall()}
        required_continuation_cols = {
            "continuation_id",
            "owner",
            "session_id",
            "source",
            "run_id",
            "revision_id",
            "idempotency_key",
            "root_id",
            "status",
            "payload_json",
            "created_at",
            "updated_at",
        }
        if not required_continuation_cols.issubset(continuation_cols):
            raise ForeignDatabaseError(f"unsupported continuations schema in {path}: missing {required_continuation_cols - continuation_cols}")

        cursor = probe.execute("PRAGMA table_info(root_budgets)")
        root_cols = {r[1] for r in cursor.fetchall()}
        required_root_cols = {"root_id", "budget", "reserved", "created_at", "updated_at"}
        if not required_root_cols.issubset(root_cols):
            raise ForeignDatabaseError(f"unsupported root_budgets schema in {path}: missing {required_root_cols - root_cols}")

        cursor = probe.execute("PRAGMA table_info(session_budgets)")
        budget_cols = {r[1] for r in cursor.fetchall()}
        required_budget_cols = {
            "session_id",
            "owner",
            "since_sequence",
            "max_input_tokens",
            "max_output_tokens",
            "max_total_tokens",
            "max_cost_usd",
            "max_elapsed_seconds",
            "created_at",
            "updated_at",
        }
        if not required_budget_cols.issubset(budget_cols):
            raise ForeignDatabaseError(f"unsupported session_budgets schema in {path}: missing {required_budget_cols - budget_cols}")

        cursor = probe.execute("PRAGMA table_info(wakeups)")
        wakeup_cols = {r[1] for r in cursor.fetchall()}
        required_wakeup_cols = {"wakeup_id", "owner", "workspace", "session_id", "payload_json", "role", "due_at", "status", "claim_run_id"}
        if not required_wakeup_cols.issubset(wakeup_cols):
            raise ForeignDatabaseError(f"unsupported wakeups schema in {path}: missing {required_wakeup_cols - wakeup_cols}")

        cursor = probe.execute("PRAGMA table_info(observer_receipts)")
        receipt_cols = {r[1] for r in cursor.fetchall()}
        required_receipt_cols = {"session_id", "event_sequence", "owner", "created_at"}
        if not required_receipt_cols.issubset(receipt_cols) or "generation" in receipt_cols:
            raise ForeignDatabaseError(f"unsupported observer_receipts schema in {path}: expected per-(event, owner) receipts")

        cursor = probe.execute("PRAGMA table_info(workspace_state)")
        workspace_cols = {r[1] for r in cursor.fetchall()}
        required_workspace_cols = {"workspace", "active_revision_id", "updated_at"}
        if not required_workspace_cols.issubset(workspace_cols):
            raise ForeignDatabaseError(f"unsupported workspace_state schema in {path}: missing {required_workspace_cols - workspace_cols}")
        return recorded
    except sqlite3.Error as exc:
        raise ForeignDatabaseError(f"database is not a valid voidcode database: unsupported database file or missing identity: {path}") from exc
    finally:
        probe.close()


#: Database versions whose table matrix the current code can read. This build reads exactly its
#: own schema; anything else refuses visibly. A schema change ships with its own transition.
READABLE_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})


def open_read_only(database: Path) -> sqlite3.Connection:
    """Open one core database for inspection, with writes refused by the connection itself.
    Uses a ``mode=ro`` URI plus ``PRAGMA query_only``, after the same identity check a writer runs.
    Accepts any recorded version whose magic and table matrix verify; foreign files raise.
    Callers that need version semantics classify the recorded version themselves.
    """
    _verify_existing_file(database, supported=READABLE_SCHEMA_VERSIONS, enforce_version=False)
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def inspect_existing_workspace(database: Path, workspace: str) -> tuple[str | None, Mapping[str, object] | None, int | None]:
    """Read one workspace's adopted revision without creating or changing anything.

    Returns ``(None, None, None)`` when there is no database, ``(None, None, recorded_schema)``
    when the workspace has no adopted revision, ``(revision_id, payload, recorded_schema)`` when
    it has one, and raises for a file that is not a core database. Never writes, never sets
    write pragmas. Accepts every version the current code can read, so the caller can decide
    between restore and transition.
    """
    if not database.exists():
        return (None, None, None)
    probe = open_read_only(database)
    try:
        identity = probe.execute("SELECT schema_version FROM kernel_identity WHERE id = 1").fetchone()
        schema = int(identity["schema_version"])
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
        recorded_schema = SCHEMA_VERSION

        if not is_memory:
            resolved_path = path.resolve()
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(resolved_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                recorded_schema = _verify_existing_file(resolved_path)
            else:
                os.close(descriptor)
                is_new = True
        else:
            is_new = True

        self._recorded_schema = recorded_schema
        self._connection = sqlite3.connect(
            str(resolved_path) if resolved_path is not None else ":memory:",
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if is_new:
                self._connection.executescript(_load_schema_sql())
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute(
                    "INSERT INTO kernel_identity (id, magic, schema_version) VALUES (1, ?, ?)",
                    (SCHEMA_MAGIC, SCHEMA_VERSION),
                )
        except Exception:
            self._connection.close()
            self._closed = True
            if is_new and resolved_path is not None:
                resolved_path.unlink(missing_ok=True)
            raise

    @property
    def recorded_schema(self) -> int:
        """The schema version the opened file records (this build opens exactly its own schema)."""
        return self._recorded_schema

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
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                        ) VALUES (?, 1, '', ?, 'kernel', 'plugin', ?, ?)
                        """,
                        (session_id, EventType.CHILD_RESERVED, revision_id, canonical_json(birth_payload)),
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
                    SELECT wakeup_id, owner, workspace, session_id, payload_json, role, due_at, status, claim_run_id
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
                payload, root_id, root_action_budget = _decode_wakeup_payload(json.loads(row[4]), str(row[0]))
                wakeup = WakeupRecord(
                    wakeup_id=str(row[0]),
                    owner=str(row[1]),
                    workspace=str(row[2]),
                    session_id=str(row[3]),
                    payload=payload,
                    root_id=root_id,
                    role=str(row[5]),
                    due_at=int(row[6]),
                    status=str(row[7]),
                    claim_run_id=str(row[8]) if row[8] is not None else None,
                    root_action_budget=root_action_budget,
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
                    raise StoreConflict(f"wakeup {wakeup.wakeup_id!r} names an unknown session")
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
                        session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?, 'plugin', ?, ?)
                    """,
                    (session.session_id, new_seq, run_id, EventType.WAKEUP_FIRED, f"plugin.{wakeup.owner}", revision_id, fired_payload),
                )
                checkpoint_payload = canonical_json(
                    {
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
                )
                self._connection.execute(
                    """
                    INSERT INTO checkpoints (
                        session_id, kind, revision_id, run_id, last_event_sequence, payload_json, updated_at
                    ) VALUES (?, 'ready', ?, ?, ?, ?, ?)
                    ON CONFLICT (session_id) DO UPDATE SET
                        kind = 'ready',
                        revision_id = excluded.revision_id,
                        run_id = excluded.run_id,
                        last_event_sequence = excluded.last_event_sequence,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (session.session_id, revision_id, run_id, new_seq, checkpoint_payload, now),
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
                    raise StoreConflict(f"wakeup session {session_id!r} does not exist")
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
                stored_payload = {
                    _WAKEUP_PAYLOAD_KEY: {
                        "root_id": root_action_id,
                        "root_action_budget": root_action_budget,
                        "payload": json_wire_object(payload),
                    }
                }
                self._connection.execute(
                    """
                    INSERT INTO wakeups (
                        wakeup_id, owner, workspace, session_id, payload_json, role, due_at,
                        status, claim_run_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                    ON CONFLICT (wakeup_id) DO NOTHING
                    """,
                    (
                        wakeup_id,
                        owner,
                        workspace,
                        session_id,
                        canonical_json(stored_payload),
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
                    SELECT wakeup_id, owner, workspace, session_id, payload_json, role, due_at, status, claim_run_id
                    FROM wakeups WHERE status = 'pending' ORDER BY due_at ASC LIMIT ?
                    """,
                    (max(1, limit),),
                )
            else:
                cursor = self._connection.execute(
                    """
                    SELECT wakeup_id, owner, workspace, session_id, payload_json, role, due_at, status, claim_run_id
                    FROM wakeups WHERE workspace = ? AND status = 'pending' ORDER BY due_at ASC LIMIT ?
                    """,
                    (workspace, max(1, limit)),
                )
            items: list[WakeupRecord] = []
            for row in cursor.fetchall():
                payload, root_id, root_action_budget = _decode_wakeup_payload(json.loads(row[4]), str(row[0]))
                items.append(
                    WakeupRecord(
                        wakeup_id=str(row[0]),
                        owner=str(row[1]),
                        workspace=str(row[2]),
                        session_id=str(row[3]),
                        payload=payload,
                        root_id=root_id,
                        role=str(row[5]),
                        due_at=int(row[6]),
                        status=str(row[7]),
                        claim_run_id=str(row[8]) if row[8] is not None else None,
                        root_action_budget=root_action_budget,
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

    def latest_stop_cause(self, session_id: str, run_id: str | None) -> str | None:
        """The committed stop cause of one run, or None when no terminal fact names one."""
        with self._lock:
            self._check_open()
            if not run_id:
                return None
            row = self._connection.execute(
                """
                SELECT payload_json FROM session_events
                WHERE session_id = ? AND run_id = ? AND event_type = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (session_id, run_id, EventType.RUN_INTERRUPTED),
            ).fetchone()
            if row is None:
                return None
            payload = own_json_object(json.loads(row[0]))
            cause = payload.get("stop_cause")
            return cause if isinstance(cause, str) and cause else None

    def has_queued_child_followup(self, session_id: str) -> bool:
        """Whether one session holds a pending core child follow-up (never notices or claims)."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT 1 FROM continuations
                WHERE session_id = ? AND status = 'pending' AND owner = ?
                  AND json_extract(payload_json, '$.kind') = 'child_followup'
                LIMIT 1
                """,
                (session_id, CHILD_MESSAGE_OWNER),
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
                    AND a.sequence = json_extract(c.payload_json, '$.approval_sequence')
                    AND c.kind = 'approval_wait' AND a.event_type = ?
                LEFT JOIN root_budgets r ON r.root_id = json_extract(c.payload_json, '$.root_id')
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
                        """
                        SELECT payload_json FROM session_events
                        WHERE event_type = ?
                          AND json_extract(payload_json, '$.root_action_grant_id') = ?
                        LIMIT 1
                        """,
                        (EventType.RUN_RESUMED, grant_id),
                    ).fetchone()
                    if prior is not None:
                        prior_payload = own_json_object(json.loads(prior[0]))
                        if prior_payload.get("root_id") != root_id or prior_payload.get("extend_root_actions") != amount:
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
                    raise StoreConflict(f"session {session_id!r} does not exist")

                (_, workspace, _, curr_revision_id, curr_run_id, curr_turn, curr_seq, curr_parent, curr_root, curr_depth, curr_allowlist) = row

                if expected_sequence is not None and curr_seq != expected_sequence:
                    raise SequenceConflict(f"sequence mismatch for session {session_id!r}: expected {expected_sequence}, got {curr_seq}")
                if expected_run_id is not None and curr_run_id != expected_run_id:
                    raise StoreConflict(f"run_id mismatch for session {session_id!r}: expected {expected_run_id!r}, got {curr_run_id!r}")
                if curr_revision_id != revision_id:
                    raise StoreConflict(f"active revision mismatch for session {session_id!r}: active is {curr_revision_id!r}, got {revision_id!r}")
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
                            # Namespaced by source family: a background action and a model
                            # message may share one owner/session/key without swallowing
                            # each other; only rows of the same family dedupe.
                            source_clause = "source = ?" if cont.source == BACKGROUND_ACTION_SOURCE else "source <> ?"
                            c_chk = self._connection.execute(
                                f"""
                                SELECT continuation_id FROM continuations
                                WHERE session_id = ? AND owner = ? AND idempotency_key = ? AND {source_clause}
                                """,
                                (session_id, cont.owner, cont.idempotency_key, BACKGROUND_ACTION_SOURCE),
                            )
                            if c_chk.fetchone() is not None:
                                continue
                        try:
                            self._connection.execute(
                                """
                                INSERT INTO continuations (
                                    continuation_id, owner, session_id, source, run_id, revision_id,
                                    idempotency_key, root_id, role, status, payload_json, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                                """,
                                (
                                    cont.continuation_id,
                                    cont.owner,
                                    session_id,
                                    cont.source,
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
                                  AND status IN ('pending', 'claimed') AND source <> ?
                                """,
                                (now, session_id, canc.owner, canc.continuation_id, BACKGROUND_ACTION_SOURCE),
                            )
                        else:
                            # Background actions are cancelled through their own abort-aware path;
                            # a blanket model-continuation cancel must not strand live drivers.
                            self._connection.execute(
                                """
                                UPDATE continuations
                                SET status = 'cancelled', updated_at = ?
                                WHERE session_id = ? AND owner = ? AND status IN ('pending', 'claimed') AND source <> ?
                                """,
                                (now, session_id, canc.owner, BACKGROUND_ACTION_SOURCE),
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
                    try:
                        self._connection.execute(
                            """
                            INSERT INTO wakeups (
                                wakeup_id, owner, workspace, session_id, payload_json, role, due_at,
                                status, claim_run_id, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                            """,
                            (
                                wakeup.wakeup_id,
                                wakeup.owner,
                                workspace,
                                wakeup.session_id,
                                canonical_json(
                                    {
                                        _WAKEUP_PAYLOAD_KEY: {
                                            "root_id": root_action_id,
                                            "root_action_budget": root_action_budget,
                                            "payload": json_wire_object(wakeup.payload),
                                        }
                                    }
                                ),
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
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO observer_receipts (session_id, event_sequence, owner, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (session_id, r.event_sequence, r.owner, now),
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
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                        ),
                    )

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
                    self._connection.execute(
                        """
                        INSERT INTO checkpoints (
                            session_id, kind, revision_id, run_id, last_event_sequence, payload_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (session_id) DO UPDATE SET
                            kind = excluded.kind,
                            revision_id = excluded.revision_id,
                            run_id = excluded.run_id,
                            last_event_sequence = excluded.last_event_sequence,
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            session_id,
                            checkpoint_kind,
                            revision_id,
                            run_id,
                            current_seq,
                            checkpoint_json,
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
                    raise StoreConflict(f"session {session_id!r} does not exist")
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
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            current_seq,
                            run_id,
                            draft.event_type,
                            draft.source,
                            draft.actor,
                            target_revision_id,
                            canonical_json(draft.payload),
                        ),
                    )

                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?, run_id = ?, updated_at = ?, last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (status, run_id, now, current_seq, session_id),
                )

                if checkpoint_kind is not None and checkpoint_payload is not None:
                    self._connection.execute(
                        """
                        INSERT INTO checkpoints (
                            session_id, kind, revision_id, run_id, last_event_sequence, payload_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (session_id) DO UPDATE SET
                            kind = excluded.kind,
                            revision_id = excluded.revision_id,
                            run_id = excluded.run_id,
                            last_event_sequence = excluded.last_event_sequence,
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (session_id, checkpoint_kind, target_revision_id, run_id, current_seq, canonical_json(checkpoint_payload), now),
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

                identity_row = self._connection.execute("SELECT schema_version FROM kernel_identity WHERE id = 1").fetchone()
                live_schema = int(identity_row[0])
                if live_schema != SCHEMA_VERSION:
                    raise StoreConflict(f"schema {live_schema} cannot be adopted")

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
                        raise StoreConflict(f"session {session_id!r} does not exist")
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
                        self._connection.execute(
                            """
                            INSERT INTO session_events (
                                session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                session_id,
                                current_seq,
                                run_id,
                                draft.event_type,
                                draft.source,
                                draft.actor,
                                target_revision_id,
                                canonical_json(draft.payload),
                            ),
                        )
                    self._connection.execute(
                        """
                        UPDATE sessions
                        SET status = ?, run_id = ?, updated_at = ?, last_event_sequence = ?
                        WHERE session_id = ?
                        """,
                        (status, run_id, now, current_seq, session_id),
                    )
                    if checkpoint_kind is not None and checkpoint_payload is not None:
                        self._connection.execute(
                            """
                            INSERT INTO checkpoints (
                                session_id, kind, revision_id, run_id, last_event_sequence, payload_json, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (session_id) DO UPDATE SET
                                kind = excluded.kind,
                                revision_id = excluded.revision_id,
                                run_id = excluded.run_id,
                                last_event_sequence = excluded.last_event_sequence,
                                payload_json = excluded.payload_json,
                                updated_at = excluded.updated_at
                            """,
                            (session_id, checkpoint_kind, target_revision_id, run_id, current_seq, canonical_json(checkpoint_payload), now),
                        )

                self._connection.execute("COMMIT")
                in_transaction = False
                self._recorded_schema = SCHEMA_VERSION
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
            WHERE status = 'claimed' AND source <> ?
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY continuation_id ASC
            """,
            (BACKGROUND_ACTION_SOURCE, workspace),
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
            self._connection.execute(
                """
                INSERT INTO session_events (
                    session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                ) VALUES (?, ?, ?, ?, 'core', 'core', ?, ?)
                """,
                (
                    str(session_id),
                    sequence,
                    str(run_id),
                    EventType.RUN_INTERRUPTED,
                    target_revision_id,
                    canonical_json(
                        {
                            "reason": "the process that owned this run is gone; adoption recovered the session at its last committed boundary",
                            "stop_cause": "shutdown",
                            "logical_turn": logical_turn,
                            "eligible_owners": eligible_owners,
                        }
                    ),
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
        if exclude_claimed_in_session:
            cursor = self._connection.execute(
                """
                SELECT continuation_id, owner
                FROM continuations
                WHERE session_id = ? AND status = 'claimed' AND source <> ?
                ORDER BY continuation_id ASC
                """,
                (acting_session_id, BACKGROUND_ACTION_SOURCE),
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
                raise StoreConflict(f"confirmation {request_id!r} is not in pending state or does not exist")

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
                    WHERE session_id = ? AND owner = ? AND idempotency_key = ? AND source <> ?
                    """,
                    (session_id, owner, idempotency_key, BACKGROUND_ACTION_SOURCE),
                )
                row = cursor.fetchone()
                if row is not None:
                    if row[1] != root_id or row[2] != payload_json:
                        raise StoreConflict("continuation idempotency key was already used with a different authorization or payload")
                    return row[0]
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
                        continuation_id, owner, session_id, source, run_id, revision_id,
                        idempotency_key, root_id, role, status, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        continuation_id,
                        owner,
                        session_id,
                        source,
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
                    raise StoreConflict(f"session {session_id!r} does not exist")
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
                      AND json_extract(payload_json, '$.kind') IN ('child_followup', 'child_finished')
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
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                        ) VALUES (?, ?, ?, ?, ?, 'plugin', ?, ?)
                        """,
                        (
                            session_id,
                            sequence,
                            run_id,
                            EventType.CONTINUATION_STARTED,
                            source,
                            revision_id,
                            canonical_json({"continuation_id": continuation_id, "payload": payload, "role": str(role)}),
                        ),
                    )
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

        Background actions share the table but own a separate idempotency namespace: the same
        owner/session/key may name one model message AND one background action, and neither
        lookup may swallow the other (ordinary keys are arbitrary strings, never prefixed).
        """
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id FROM continuations
                WHERE session_id = ? AND owner = ? AND idempotency_key = ? AND source <> ?
                """,
                (session_id, owner, idempotency_key, BACKGROUND_ACTION_SOURCE),
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
                WHERE session_id = ? AND status = 'pending' AND source <> ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, BACKGROUND_ACTION_SOURCE, max(1, limit)),
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
                WHERE session_id = ? AND status = 'claimed' AND source <> ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, BACKGROUND_ACTION_SOURCE, max(1, limit)),
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
                raise StoreConflict(f"continuation {continuation_id!r} does not exist")

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
                    raise StoreConflict(f"session {session_id!r} does not exist")
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
                      AND source <> '@core/background'
                      AND owner <> 'kernel'
                      AND (? IS NULL OR continuation_id = ?)
                      AND NOT (? AND owner = ? AND COALESCE(json_extract(payload_json, '$.kind'), '') = 'child_finished')
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
                          AND source <> '@core/background'
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
                    "SELECT 1 FROM continuations WHERE session_id = ? AND status = 'claimed' AND source <> ? LIMIT 1",
                    (session_id, BACKGROUND_ACTION_SOURCE),
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
                        session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?, 'plugin', ?, ?)
                    """,
                    (session_id, new_seq, run_id, EventType.CONTINUATION_STARTED, f"plugin.{cont_record.owner}", revision_id, disp_payload),
                )

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
                self._connection.execute(
                    """
                    INSERT INTO checkpoints (
                        session_id, kind, revision_id, run_id, last_event_sequence, payload_json, updated_at
                    ) VALUES (?, 'ready', ?, ?, ?, ?, ?)
                    ON CONFLICT (session_id) DO UPDATE SET
                        kind = excluded.kind,
                        revision_id = excluded.revision_id,
                        run_id = excluded.run_id,
                        last_event_sequence = excluded.last_event_sequence,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, revision_id, run_id, new_seq, checkpoint_json, now),
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

    def claim_background_action(self, *, workspace: str, claim_identity: Mapping[str, object]) -> ContinuationRecord | None:
        """Claim ONE pending background action of this workspace; its driver runs it, not a model turn.

        Only rows whose core-controlled `source` marks them background are eligible; ordinary
        continuations (whose payload may contain anything) are never touched. The `claim_identity`
        envelope records who holds the claim so another process can tell a live claim from a
        stale one without a heartbeat framework.
        """
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                row = self._connection.execute(
                    """
                    SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                           idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                    FROM continuations
                    WHERE source = ? AND status = 'pending'
                      AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (BACKGROUND_ACTION_SOURCE, workspace),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return None
                now = int(time.time() * 1000)
                root_id = row[7]
                if not isinstance(root_id, str) or not root_id:
                    raise StoreConflict(f"background action {row[0]!r} has no captured root authorization")
                payload = own_json_object(json.loads(row[9]))
                pending = payload.get("action")
                action = dict(pending) if isinstance(pending, Mapping) else {}
                if "root_action_budget" not in payload:
                    raise StoreConflict(f"background action {row[0]!r} has no captured root budget")
                root_budget = payload["root_action_budget"]
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
                envelope = {
                    "claim": json_wire_object(claim_identity),
                    "action": action,
                    "root_action_budget": root_budget,
                }
                if "tool_wait_authority" in payload:
                    envelope["tool_wait_authority"] = payload["tool_wait_authority"]
                claimed = self._connection.execute(
                    """
                    UPDATE continuations SET status = 'claimed', updated_at = ?, payload_json = ?
                    WHERE continuation_id = ? AND status = 'pending'
                    """,
                    (now, canonical_json(envelope), row[0]),
                )
                if claimed.rowcount == 0:
                    raise StoreConflict(f"background action {row[0]!r} was claimed concurrently")
                self._connection.execute("COMMIT")
                in_transaction = False
                return ContinuationRecord(
                    continuation_id=row[0],
                    owner=row[1],
                    session_id=row[2],
                    source=row[3],
                    run_id=row[4],
                    revision_id=row[5],
                    idempotency_key=row[6],
                    status="claimed",
                    payload=envelope,
                    created_at=row[10],
                    updated_at=now,
                    root_id=row[7],
                    role=str(row[12]),
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def complete_background_action(self, action_id: str, *, status: Literal["completed", "failed", "cancelled"], error: str | None = None) -> None:
        """Settle a pending or claimed background action into its terminal state.

        A failed settle records the driver's error as core envelope metadata, leaving
        the owner's action payload byte-identical: business payloads are arbitrary JSON
        and must never be rewritten or misread as execution errors.
        """
        with self._lock:
            self._check_open()
            if status not in ("completed", "failed", "cancelled"):
                raise ValueError(f"background action completion must be terminal, got {status!r}")
            if error is not None and status == "failed":
                row = self._connection.execute(
                    "SELECT payload_json FROM continuations WHERE continuation_id = ? AND source = ?",
                    (action_id, BACKGROUND_ACTION_SOURCE),
                ).fetchone()
                if row is not None:
                    try:
                        envelope = dict(json.loads(row[0]))
                    except ValueError:
                        envelope = {}
                    envelope["error"] = error
                    self._connection.execute(
                        "UPDATE continuations SET payload_json = ? WHERE continuation_id = ?",
                        (canonical_json(envelope), action_id),
                    )
            cursor = self._connection.execute(
                """
                UPDATE continuations SET status = ?, updated_at = ?
                WHERE continuation_id = ? AND source = ? AND status IN ('pending', 'claimed')
                """,
                (status, int(time.time() * 1000), action_id, BACKGROUND_ACTION_SOURCE),
            )
            if cursor.rowcount == 0:
                raise StoreConflict(f"background action {action_id!r} is not settleable")

    def background_action(self, action_id: str) -> ContinuationRecord | None:
        """One background action row by id, or None when it is not a background row."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                       idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                FROM continuations
                WHERE continuation_id = ? AND source = ?
                """,
                (action_id, BACKGROUND_ACTION_SOURCE),
            ).fetchone()
            if row is None:
                return None
            return ContinuationRecord(
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

    def background_action_id_for_idempotency(self, session_id: str, owner: str, idempotency_key: str) -> str | None:
        """The durable background action id already recorded for an idempotency key."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT continuation_id FROM continuations
                WHERE session_id = ? AND owner = ? AND idempotency_key = ? AND source = ?
                """,
                (session_id, owner, idempotency_key, BACKGROUND_ACTION_SOURCE),
            ).fetchone()
            return None if row is None else str(row[0])

    def pending_background_actions(self, session_id: str) -> tuple[ContinuationRecord, ...]:
        """Every pending or claimed background action of one session, oldest first."""
        with self._lock:
            self._check_open()
            rows = self._connection.execute(
                """
                SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                       idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                FROM continuations
                WHERE session_id = ? AND source = ? AND status IN ('pending', 'claimed')
                ORDER BY created_at ASC
                LIMIT 64
                """,
                (session_id, BACKGROUND_ACTION_SOURCE),
            ).fetchall()
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
