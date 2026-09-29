"""Typed row decoding for the SQLite store.

``sqlite3.Row`` is a runtime mapping with no column types, and typeshed reports
``fetchone``/``fetchall`` as ``Any`` — so every reader used to re-declare its
own columns with a per-site ``cast``. This module is the single decode boundary
instead: each TypedDict is the declared shape of one SELECT against the DDL in
``sqlite.py`` (or one ``PRAGMA`` result), ``fetch_row``/``fetch_rows`` declare
the row factory once, and ``decode_row`` is the one ``cast`` that pins a
fetched row to the shape the caller names.

Nullability mirrors the DDL: a nullable column is ``T | None``, a ``NOT NULL``
column is ``T``. A partial SELECT gets its own narrow TypedDict declaring
exactly the selected columns, so a ``NOT NULL`` column is never marked optional
just because a query happens not to select it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import TypedDict, cast


def fetch_row(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> sqlite3.Row | None:
    """Fetch one row through the store's ``sqlite3.Row`` factory."""
    return connection.execute(sql, parameters).fetchone()


def fetch_rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> list[sqlite3.Row]:
    """Fetch every row through the store's ``sqlite3.Row`` factory."""
    return connection.execute(sql, parameters).fetchall()


def decode_row[T: Mapping[str, object]](row: sqlite3.Row, shape: type[T]) -> T:
    """Pin ``row`` to the declared ``shape`` of the SELECT that produced it.

    ``shape`` must name exactly the selected columns; a mismatch is the one
    mistake this boundary cannot detect at runtime.
    """
    return cast(shape, dict(row))


class SessionMetadataRow(TypedDict):
    metadata_json: str


class SessionListRow(TypedDict):
    session_id: str
    parent_session_id: str | None
    status: str
    turn: int
    prompt: str
    title: str | None
    forked_from_session_id: str | None
    forked_at_sequence: int | None
    updated_at: int


class SessionStatusRow(TypedDict):
    status: str


class SessionStatusMetadataRow(TypedDict):
    status: str
    metadata_json: str


class SessionLoadRow(TypedDict):
    session_id: str
    parent_session_id: str | None
    status: str
    turn: int
    output: str | None
    metadata_json: str


class SessionTitleRow(TypedDict):
    title: str | None


class SessionForkProvenanceRow(TypedDict):
    forked_from_session_id: str | None
    forked_at_sequence: int | None


class SessionLineageRow(TypedDict):
    session_id: str
    forked_from_session_id: str | None
    forked_at_sequence: int | None
    updated_at: int


class SessionForkSourceRow(TypedDict):
    session_id: str
    parent_session_id: str | None
    status: str
    turn: int
    prompt: str
    title: str | None
    metadata_json: str
    last_event_sequence: int


class SessionPromptTitleRow(TypedDict):
    prompt: str
    title: str | None


class SessionCreatedAtRow(TypedDict):
    created_at: int


class SessionCreatedAtUnixMsRow(TypedDict):
    created_at_unix_ms: int | None


class SessionLastEventSequenceRow(TypedDict):
    last_event_sequence: int


class SessionRuntimeStateRow(TypedDict):
    status: str
    pending_approval_json: str | None
    pending_question_json: str | None


class SessionPendingApprovalRow(TypedDict):
    pending_approval_json: str | None


class SessionApprovalRecoveryRow(TypedDict):
    status: str
    pending_approval_json: str | None
    resume_checkpoint_json: str | None


class SessionPendingQuestionRow(TypedDict):
    pending_question_json: str | None


class SessionResumeCheckpointRow(TypedDict):
    resume_checkpoint_json: str | None


class SessionEffectivenessRow(TypedDict):
    session_id: str
    metadata_json: str


class SessionEventRow(TypedDict):
    sequence: int
    event_type: str
    source: str
    payload_json: str


class SessionEventWithSessionRow(TypedDict):
    session_id: str
    sequence: int
    event_type: str
    source: str
    payload_json: str


class BackgroundTaskRow(TypedDict):
    task_id: str
    workspace_id: str
    status: str
    prompt: str
    request_session_id: str | None
    request_parent_session_id: str | None
    request_metadata_json: str
    requested_child_session_id: str | None
    routing_mode: str | None
    routing_subagent_type: str | None
    routing_description: str | None
    routing_command: str | None
    approval_request_id: str | None
    question_request_id: str | None
    cancellation_cause: str | None
    result_available: int
    delegated_reminder_json: str | None
    allocate_session_id: int
    session_id: str | None
    error: str | None
    cancel_requested_at: int | None
    created_at: int
    updated_at: int
    started_at: int | None
    finished_at: int | None
    created_at_unix_ms: int | None
    started_at_unix_ms: int | None
    finished_at_unix_ms: int | None
    keep_alive: int
    steer_prompt: str | None
    output_schema_json: str | None
    schema_mode: str
    structured_output_json: str | None
    schema_validation_json: str | None


class BackgroundTaskSummaryRow(TypedDict):
    task_id: str
    status: str
    prompt: str
    session_id: str | None
    error: str | None
    created_at: int
    updated_at: int
    created_at_unix_ms: int | None
    keep_alive: int
    steer_prompt: str | None
    output_schema_json: str | None
    schema_mode: str


class BackgroundTaskReconcileRow(TypedDict):
    task_id: str
    cancel_requested_at: int | None
    delegated_reminder_json: str | None


class BackgroundTaskStatusCountRow(TypedDict):
    status: str
    count: int


class BackgroundProcessRow(TypedDict):
    process_id: str
    workspace_id: str
    owner_session_id: str | None
    command: str
    cwd: str
    pid: int
    process_group_id: int | None
    process_identity: str | None
    stdout_path: str
    stderr_path: str
    status: str
    exit_code: int | None
    reconciliation_reason: str | None
    created_at: int
    updated_at: int


class StorageSequenceValueRow(TypedDict):
    value: int


class SessionIdRow(TypedDict):
    session_id: str


class TaskIdRow(TypedDict):
    task_id: str


class SqliteMasterNameRow(TypedDict):
    name: str


class TableInfoRow(TypedDict):
    """One ``PRAGMA table_info(<table>)`` row (``cid`` first, ``pk`` last)."""

    cid: int
    name: str
    type: str
    notnull: int
    dflt_value: str | None
    pk: int


class IndexListRow(TypedDict):
    """One ``PRAGMA index_list(<table>)`` row."""

    seq: int
    name: str
    unique: int
    origin: str
    partial: int


class IndexInfoRow(TypedDict):
    """One ``PRAGMA index_info(<index>)`` row."""

    seqno: int
    cid: int
    name: str
