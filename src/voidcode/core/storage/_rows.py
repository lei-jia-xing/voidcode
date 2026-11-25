"""Typed SQLite row shapes and their decoders.

One `NamedTuple` per SELECT shape plus one decoder that turns it into the domain record.
Column order is the SQL contract: it is written positionally exactly once, where the row
crosses the SQLite boundary, and every later access is a field read. Fields the schema
constrains (STRICT tables, NOT NULL, CHECK) are trusted as stored; no per-field
`isinstance` fallback re-checks what the database already enforced.

Payload read-back contract (one deliberate freeze, not a redundant copy): every decoded
record payload is normalized once through `own_json_object`, so JSON arrays become tuples
and objects become read-only mappings at the storage boundary. This is observable: plugin
observers stage `asdict(fact)` from SDK fact dataclasses whose sequence fields are tuples,
and `test_sdk_facts` requires the stored observation to compare equal to the live fact
after read-back. Without the normalization a JSON array decodes as a list and
`tuple != list`. `SessionInfo.pending_approval` is not frozen here because the SDK
`SessionInfo.__post_init__` owns that snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, NamedTuple

from ...sdk.json_values import own_json_object
from ...sdk.plugin import StateScope
from ...sdk.services import PluginBudget, SessionInfo
from ._values import (
    ActionConfirmationRecord,
    Checkpoint,
    CheckpointKind,
    ConfirmationStatus,
    ContinuationRecord,
    ContinuationStatus,
    MessageRole,
    PluginStateRow,
    SessionRecord,
    SessionStatus,
    StorageError,
    StoreConflict,
    StoredEvent,
    StoredValueError,
    WaitingAskPark,
    WakeupRecord,
    WindowProjectionRecord,
)


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
