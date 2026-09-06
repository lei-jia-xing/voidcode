from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .context.window import (
    ContextProjection,
    RuntimeContextWindow,
    continuity_state_from_metadata_payload,
)
from .contracts import (
    DELEGATION_METADATA_KEYS,
    PLAN_STATE_METADATA_KEYS,
    RUNTIME_STATE_METADATA_KEYS,
    SKILL_SNAPSHOT_METADATA_KEYS,
    AcpStateMetadata,
    ContextCompactedStateMetadata,
    ContextProjectionMetadata,
    ContextTransformAppliedStateMetadata,
    PendingToolIntentMetadata,
    PersistedDelegationMetadata,
    PlanStateMetadata,
    RuntimeResponse,
    RuntimeStateMetadata,
    SkillSnapshotMetadata,
    TodosStateMetadata,
    UnknownSessionError,
)
from .permission import DelegationGovernance
from .permission_policy import (
    pending_approval_from_response,
    pending_question_from_response,
)
from .session import (
    SessionState,
    session_metadata_for_persistence,
    validate_session_workspace,
)
from .skills import snapshot_from_payload
from .todos import (
    runtime_todo_phases_from_payload,
    runtime_todo_state_from_payload,
    todo_event_payload,
    todo_state_payload,
)

if TYPE_CHECKING:
    from .acp import AcpAdapterState
    from .storage import SessionStore

logger = logging.getLogger(__name__)

_DELEGATION_GOVERNANCE = DelegationGovernance()

# Allowed values for the current-schema ``plan_state.status`` field. A
# ``plan_state`` payload must include ``status``; the other fields are optional.
_PLAN_STATE_STATUSES = frozenset(
    {
        "waiting",
        "waiting_approval",
        "waiting_question",
        "in_progress",
        "completed",
        "interrupted",
        "failed",
    }
)


def _reject_unknown_metadata_keys(
    payload: dict[str, object],
    *,
    allowed_keys: frozenset[str],
    structure_name: str,
) -> None:
    unknown_keys = sorted(key for key in payload if key not in allowed_keys)
    if unknown_keys:
        raise ValueError(f"persisted {structure_name} field '{unknown_keys[0]}' is not supported")


def _validate_runtime_state_metadata_types(payload: dict[str, object]) -> None:
    if "run_id" in payload:
        run_id = payload["run_id"]
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("persisted runtime_state field 'run_id' must be a non-empty string")
    for field in (
        "acp",
        "context_projection",
        "context_projection_summary",
        "todos",
        "pending_tool_intent",
        "context_compacted",
        "context_transform_applied",
    ):
        if field in payload and not isinstance(payload[field], dict):
            raise ValueError(f"persisted runtime_state field '{field}' must be an object")


def _validate_plan_state_metadata_types(payload: dict[str, object]) -> None:
    if "status" not in payload:
        raise ValueError("persisted plan_state is missing required field 'status'")
    for field in ("status", "approval_request_id", "blocked_tool", "last_error"):
        if field in payload and not isinstance(payload[field], str):
            raise ValueError(f"persisted plan_state field '{field}' must be a string")
    if payload["status"] not in _PLAN_STATE_STATUSES:
        joined = ", ".join(sorted(_PLAN_STATE_STATUSES))
        raise ValueError(f"persisted plan_state field 'status' must be one of: {joined}")


def _validate_delegation_metadata_types(payload: dict[str, object]) -> None:
    required_fields = {"mode"}
    missing_fields = sorted(required_fields - payload.keys())
    if missing_fields:
        raise ValueError("persisted delegation is missing required field(s): " + ", ".join(missing_fields))
    for field in ("subagent_type", "description", "command", "selected_preset", "selected_execution_engine", "parallel_group_id"):
        if field in payload and not isinstance(payload[field], str):
            raise ValueError(f"persisted delegation field '{field}' must be a string")
    if payload["mode"] not in {"sync", "background"}:
        raise ValueError("persisted delegation field 'mode' must be one of: sync, background")
    for field in ("depth", "remaining_spawn_budget", "parallel_group_size"):
        if field in payload:
            value = payload[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"persisted delegation field '{field}' must be a non-negative integer")
    if "output_schema" in payload and not isinstance(payload["output_schema"], dict):
        raise ValueError("persisted delegation field 'output_schema' must be an object")
    if "schema_mode" in payload and payload["schema_mode"] not in {"permissive", "strict"}:
        raise ValueError("persisted delegation field 'schema_mode' must be one of: permissive, strict")


def parse_runtime_state_metadata(raw: object) -> RuntimeStateMetadata:
    """Parse the present ``session.metadata["runtime_state"]`` payload.

    Unknown keys and invalid field types are rejected. Nested sections are
    optional, and the returned mapping is a shallow copy of the input.
    """
    if not isinstance(raw, dict):
        raise ValueError("persisted runtime_state must be an object")
    payload = dict(raw)
    _reject_unknown_metadata_keys(
        payload,
        allowed_keys=RUNTIME_STATE_METADATA_KEYS,
        structure_name="runtime_state",
    )
    _validate_runtime_state_metadata_types(payload)
    return cast(RuntimeStateMetadata, payload)


def parse_plan_state_metadata(raw: object) -> PlanStateMetadata:
    """Parse the present ``session.metadata["plan_state"]`` payload.

    The current schema requires ``status`` and rejects unknown keys, invalid
    field types, and status values outside the declared set. Other fields are
    optional, and the returned mapping is a shallow copy of the input.
    """
    if not isinstance(raw, dict):
        raise ValueError("persisted plan_state must be an object")
    payload = dict(raw)
    _reject_unknown_metadata_keys(
        payload,
        allowed_keys=PLAN_STATE_METADATA_KEYS,
        structure_name="plan_state",
    )
    _validate_plan_state_metadata_types(payload)
    return cast(PlanStateMetadata, payload)


def parse_delegation_metadata(raw: object) -> PersistedDelegationMetadata:
    """Parse the present ``session.metadata["delegation"]`` payload.

    The current schema requires ``mode`` and rejects unknown keys, invalid
    field types, and invalid enum or non-negative-integer values. Other fields
    are optional, and the returned mapping is a shallow copy of the input.
    """
    if not isinstance(raw, dict):
        raise ValueError("persisted delegation must be an object")
    payload = dict(raw)
    _reject_unknown_metadata_keys(
        payload,
        allowed_keys=DELEGATION_METADATA_KEYS,
        structure_name="delegation",
    )
    _validate_delegation_metadata_types(payload)
    return cast(PersistedDelegationMetadata, payload)


def parse_skill_snapshot_metadata(raw: object) -> SkillSnapshotMetadata:
    """Parse the versioned, hashed ``session.metadata["skill_snapshot"]`` payload.

    Unknown top-level keys are rejected before ``snapshot_from_payload``
    validates the required fields, ``snapshot_version``, field types, and
    ``snapshot_hash``.
    """
    if not isinstance(raw, dict):
        raise ValueError("persisted skill_snapshot must be an object")
    payload = dict(raw)
    _reject_unknown_metadata_keys(
        payload,
        allowed_keys=SKILL_SNAPSHOT_METADATA_KEYS,
        structure_name="skill_snapshot",
    )
    _ = snapshot_from_payload(payload)
    return cast(SkillSnapshotMetadata, payload)


def _runtime_state_payload(metadata: Mapping[str, object]) -> RuntimeStateMetadata:
    """Return the strictly parsed runtime state, or an empty mapping when absent."""
    if "runtime_state" not in metadata:
        return {}
    return parse_runtime_state_metadata(metadata["runtime_state"])


def runtime_state_run_id(metadata: Mapping[str, object]) -> str | None:
    run_id = _runtime_state_payload(metadata).get("run_id")
    return run_id if isinstance(run_id, str) else None


def runtime_state_acp(metadata: Mapping[str, object]) -> AcpStateMetadata | None:
    value = _runtime_state_payload(metadata).get("acp")
    return value if isinstance(value, dict) else None


def runtime_state_todos(metadata: Mapping[str, object]) -> TodosStateMetadata | None:
    value = _runtime_state_payload(metadata).get("todos")
    if value is None:
        return None
    return cast(TodosStateMetadata, runtime_todo_state_from_payload(value))


def runtime_state_pending_tool_intent(metadata: Mapping[str, object]) -> PendingToolIntentMetadata | None:
    value = _runtime_state_payload(metadata).get("pending_tool_intent")
    return value if isinstance(value, dict) else None


def runtime_state_context_compacted(metadata: Mapping[str, object]) -> ContextCompactedStateMetadata | None:
    value = _runtime_state_payload(metadata).get("context_compacted")
    return value if isinstance(value, dict) else None


def runtime_state_context_transform_applied(metadata: Mapping[str, object]) -> ContextTransformAppliedStateMetadata | None:
    value = _runtime_state_payload(metadata).get("context_transform_applied")
    return value if isinstance(value, dict) else None


def runtime_state_context_projection(metadata: Mapping[str, object]) -> ContextProjectionMetadata | None:
    value = _runtime_state_payload(metadata).get("context_projection")
    return value if isinstance(value, dict) else None


def runtime_state_context_projection_summary(metadata: Mapping[str, object]) -> dict[str, str] | None:
    value = _runtime_state_payload(metadata).get("context_projection_summary")
    return value if isinstance(value, dict) else None


def runtime_state_value(metadata: Mapping[str, object], key: str) -> object | None:
    """Read a named field from the strictly parsed ``runtime_state`` payload."""
    return _runtime_state_payload(metadata).get(key)


def _acp_state_payload(acp_state: AcpAdapterState) -> dict[str, object]:
    """Serialize ``AcpAdapterState`` to the current ``runtime_state.acp`` payload."""
    return {
        "mode": acp_state.mode,
        "configured_enabled": acp_state.configuration.configured_enabled,
        "status": acp_state.status,
        "available": acp_state.available,
        "last_error": acp_state.last_error,
        "last_request_type": acp_state.last_request_type,
        "last_request_id": acp_state.last_request_id,
        "last_event_type": acp_state.last_event_type,
        "last_delegation": (acp_state.last_delegation.as_payload() if acp_state.last_delegation is not None else None),
    }


def _runtime_state_payload_with_updates(
    metadata: Mapping[str, object],
    *,
    updates: Mapping[str, object] | None = None,
    removed: frozenset[str] = frozenset(),
) -> RuntimeStateMetadata:
    """Merge updates and removals into a normalized, strictly validated copy.

    An absent ``runtime_state`` section is normalized to an empty mapping. A
    present section must be an object, and the merged result must satisfy the
    current runtime-state schema.
    """
    raw_runtime_state = metadata.get("runtime_state")
    if raw_runtime_state is None:
        runtime_state: dict[str, object] = {}
    elif isinstance(raw_runtime_state, dict):
        runtime_state = dict(raw_runtime_state)
    else:
        raise ValueError("persisted runtime_state must be an object")
    if updates:
        runtime_state.update(updates)
    for key in removed:
        runtime_state.pop(key, None)
    return parse_runtime_state_metadata(runtime_state)


def runtime_state_metadata_payload(
    *,
    run_id: str | None = None,
    acp_state: AcpAdapterState,
) -> RuntimeStateMetadata:
    """Build a strictly validated ``runtime_state`` payload for a new run.

    ``run_id`` is included only when non-``None``; the ACP state is always
    included.
    """
    payload = {
        **({"run_id": run_id} if run_id is not None else {}),
        "acp": _acp_state_payload(acp_state),
    }
    return parse_runtime_state_metadata(payload)


def session_with_run_id(
    session: SessionState,
    *,
    run_id: str | None,
) -> SessionState:
    """Return ``session`` with a normalized ``runtime_state.run_id``.

    When ``run_id`` is ``None``, normalize persisted metadata without adding a
    ``run_id`` field; otherwise merge the identifier through strict validation.
    """
    persisted = session_metadata_for_persistence(session.metadata)
    if run_id is None:
        return _session_with_metadata(session, persisted)
    runtime_state = _runtime_state_payload_with_updates(persisted, updates={"run_id": run_id})
    return _session_with_metadata(session, {**persisted, "runtime_state": runtime_state})


def session_with_context_compacted_state(
    session: SessionState,
    *,
    summary_anchor: str | None,
    original_tool_result_count: int,
    retained_tool_result_count: int,
) -> SessionState:
    """Persist the context-compaction marker through strict runtime-state validation."""
    runtime_state = _runtime_state_payload_with_updates(
        session.metadata,
        updates={
            "context_compacted": {
                "last_summary_anchor": summary_anchor,
                "last_original_tool_result_count": original_tool_result_count,
                "last_retained_tool_result_count": retained_tool_result_count,
                "last_emitted_run_id": runtime_state_run_id(session.metadata),
            },
        },
    )
    return _session_with_metadata(session, {**session.metadata, "runtime_state": runtime_state})


def session_with_context_transform_applied_state(
    session: SessionState,
    *,
    fingerprints: tuple[str, ...],
) -> SessionState:
    """Persist context-transform fingerprints through strict runtime-state validation."""
    current_run_id = runtime_state_run_id(session.metadata)
    transform_state = runtime_state_context_transform_applied(session.metadata) or {}
    last_run_id = transform_state.get("last_emitted_run_id")
    last_run_id = last_run_id if isinstance(last_run_id, str) else None
    existing_fingerprints: set[str] = set()
    if current_run_id is None or last_run_id == current_run_id:
        raw_existing = transform_state.get("last_emitted_fingerprints")
        if isinstance(raw_existing, list):
            existing_fingerprints = {item for item in raw_existing if isinstance(item, str) and item.strip()}
    existing_fingerprints.update(fingerprints)
    runtime_state = _runtime_state_payload_with_updates(
        session.metadata,
        updates={
            "context_transform_applied": {
                "last_emitted_fingerprints": sorted(existing_fingerprints),
                "last_emitted_run_id": current_run_id,
            },
        },
    )
    return _session_with_metadata(session, {**session.metadata, "runtime_state": runtime_state})


def session_without_tool_intent(session: SessionState) -> SessionState:
    """Return ``session`` without ``runtime_state.pending_tool_intent``.

    If the field is absent, return the same object to preserve no-op identity;
    otherwise remove it through strict runtime-state validation.
    """
    if "pending_tool_intent" not in _runtime_state_payload(session.metadata):
        return session
    runtime_state = _runtime_state_payload_with_updates(
        session.metadata,
        removed=frozenset({"pending_tool_intent"}),
    )
    return _session_with_metadata(session, {**session.metadata, "runtime_state": runtime_state})


def session_metadata_with_runtime_state_updates(
    metadata: dict[str, object],
    *,
    updates: Mapping[str, object] | None = None,
    removed: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Return metadata with runtime-state updates/removals strictly validated.

    The merge is copied through one normalization path so callers do not
    construct a second runtime-state representation.
    """
    runtime_state = _runtime_state_payload_with_updates(metadata, updates=updates, removed=removed)
    return {**metadata, "runtime_state": runtime_state}


def session_model_identity(
    metadata: Mapping[str, object],
) -> tuple[str | None, str | None]:
    """Return ``(model, provider)`` resolved from session metadata, if known.

    ``model`` is the configured model reference (``provider/model`` or a bare
    model name) and ``provider`` the resolved provider id from the active
    provider target. Both are ``None`` when the metadata does not carry them.
    """
    runtime_config = metadata.get("runtime_config")
    if not isinstance(runtime_config, Mapping):
        return None, None
    model = runtime_config.get("model")
    if not isinstance(model, str) or not model:
        model = None
    provider: str | None = None
    resolved_provider = runtime_config.get("resolved_provider")
    if isinstance(resolved_provider, Mapping):
        active_target = resolved_provider.get("active_target")
        if isinstance(active_target, Mapping):
            raw_provider = active_target.get("provider")
            if isinstance(raw_provider, str) and raw_provider:
                provider = raw_provider
            if model is None:
                raw_model = active_target.get("raw_model")
                if isinstance(raw_model, str) and raw_model:
                    model = raw_model
    return model, provider


def plan_state_from_metadata(
    metadata: dict[str, object],
    *,
    status: str | None = None,
    approval_request_id: str | None = None,
    blocked_tool: str | None = None,
    error: str | None = None,
) -> dict[str, object] | None:
    existing_plan_state = metadata.get("plan_state")
    if existing_plan_state is None:
        return None
    if not isinstance(existing_plan_state, dict):
        raise ValueError("persisted plan_state must be an object")
    plan_state: dict[str, object] = dict(cast(dict[str, object], existing_plan_state))

    if status is not None:
        plan_state["status"] = status

    if approval_request_id is not None:
        plan_state["approval_request_id"] = approval_request_id
    else:
        plan_state.pop("approval_request_id", None)

    if blocked_tool is not None:
        plan_state["blocked_tool"] = blocked_tool
    else:
        plan_state.pop("blocked_tool", None)

    if error is not None:
        plan_state["last_error"] = error
    else:
        plan_state.pop("last_error", None)

    return cast(
        dict[str, object],
        parse_plan_state_metadata(plan_state),
    )


def session_with_context_window_payload_metadata(
    session: SessionState,
    context_window_payload: dict[str, object],
) -> SessionState:
    if "continuity_state" in context_window_payload:
        raise ValueError("legacy continuity_state context metadata is no longer supported")
    raw_runtime_state = session.metadata.get("runtime_state")
    if raw_runtime_state is not None and not isinstance(raw_runtime_state, dict):
        raise ValueError("persisted runtime_state must be an object")
    continuity_payload_raw = context_window_payload.get("projection")
    continuity_payload = cast(dict[str, object], continuity_payload_raw) if isinstance(continuity_payload_raw, dict) else None
    summary_anchor = context_window_payload.get("summary_anchor")
    summary_source = context_window_payload.get("summary_source")
    continuity_summary_payload = (
        {
            "anchor": summary_anchor,
            "source": summary_source,
        }
        if isinstance(summary_anchor, str)
        else None
    )
    metadata = dict(session.metadata)
    raw_prompt_activation = context_window_payload.get("prompt_activation")
    if isinstance(raw_prompt_activation, dict):
        prompt_activation = dict(cast(dict[str, object], raw_prompt_activation))
        raw_runtime_policy = metadata.get("runtime_policy")
        runtime_policy = dict(cast(dict[str, object], raw_runtime_policy)) if isinstance(raw_runtime_policy, dict) else {}
        runtime_policy["prompt_activation"] = prompt_activation
        metadata["runtime_policy"] = runtime_policy
    runtime_state = _runtime_state_payload_with_updates(
        metadata,
        updates={
            **({"context_projection": continuity_payload} if continuity_payload is not None else {}),
            **({"context_projection_summary": continuity_summary_payload} if continuity_summary_payload is not None else {}),
        },
    )
    return SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata={
            **metadata,
            "context_window": context_window_payload,
            "runtime_state": runtime_state,
        },
    )


def session_with_todo_state(
    session: SessionState,
    *,
    raw_phases: object,
    revision: int,
) -> tuple[SessionState, dict[str, object]]:
    phases = runtime_todo_phases_from_payload(raw_phases)
    state_payload = todo_state_payload(phases, revision=revision)
    runtime_state = _runtime_state_payload_with_updates(
        session.metadata,
        updates={"todos": state_payload},
    )
    next_session = SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata={**session.metadata, "runtime_state": runtime_state},
    )
    event_payload = todo_event_payload(
        session_id=session.session.id,
        phases=phases,
        revision=revision,
    )
    return next_session, event_payload


def _session_with_metadata(session: SessionState, metadata: dict[str, object]) -> SessionState:
    return SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata=metadata,
    )


def session_with_plan_state(
    session: SessionState,
    *,
    status: str | None = None,
    approval_request_id: str | None = None,
    blocked_tool: str | None = None,
    error: str | None = None,
) -> SessionState:
    plan_state = plan_state_from_metadata(
        session.metadata,
        status=status,
        approval_request_id=approval_request_id,
        blocked_tool=blocked_tool,
        error=error,
    )
    if plan_state is None:
        if status is not None and status.startswith("waiting_"):
            plan_state: dict[str, object] = {"status": status}
            if approval_request_id is not None:
                plan_state["approval_request_id"] = approval_request_id
            if blocked_tool is not None:
                plan_state["blocked_tool"] = blocked_tool
            if error is not None:
                plan_state["last_error"] = error
            plan_state = cast(
                dict[str, object],
                parse_plan_state_metadata(plan_state),
            )
        else:
            return session
    return _session_with_metadata(
        session,
        {
            **session.metadata,
            "plan_state": plan_state,
        },
    )


def session_with_context_window_metadata(
    session: SessionState,
    context_window: RuntimeContextWindow,
) -> SessionState:
    return session_with_context_window_payload_metadata(session, context_window.metadata_payload())


def delegation_depth_from_metadata(metadata: dict[str, object] | None) -> int:
    if metadata is None or "delegation" not in metadata:
        return 0
    delegation = parse_delegation_metadata(metadata["delegation"])
    depth = delegation.get("depth")
    return depth if isinstance(depth, int) else 0


def remaining_spawn_budget_from_metadata(metadata: dict[str, object] | None) -> int:
    if metadata is None or "delegation" not in metadata:
        return _DELEGATION_GOVERNANCE.spawn_budget
    delegation = parse_delegation_metadata(metadata["delegation"])
    remaining = delegation.get("remaining_spawn_budget")
    return remaining if isinstance(remaining, int) else _DELEGATION_GOVERNANCE.spawn_budget


def continuity_state_from_session_metadata(
    session_metadata: dict[str, object],
) -> ContextProjection | None:
    continuity = runtime_state_context_projection(session_metadata)
    if continuity is None:
        return None
    return continuity_state_from_metadata_payload(continuity)


def _runtime_state_metadata_with_acp_state(
    metadata: dict[str, object],
    acp_state: AcpAdapterState,
) -> dict[str, object]:
    runtime_state_metadata = _runtime_state_payload_with_updates(
        metadata,
        updates={"acp": _acp_state_payload(acp_state)},
    )
    return {**metadata, "runtime_state": runtime_state_metadata}


def session_with_current_acp_metadata(
    session: SessionState,
    acp_state: AcpAdapterState,
) -> SessionState:
    return _session_with_metadata(
        session,
        _runtime_state_metadata_with_acp_state(
            session.metadata,
            acp_state,
        ),
    )


def persist_tool_execution_intent(
    store: SessionStore,
    workspace: Path,
    session: SessionState,
    intent: dict[str, object],
) -> None:
    """Persist a pending tool intent within the workspace/session scope."""

    pending = dict(intent)
    runtime_state = _runtime_state_payload_with_updates(
        session.metadata,
        updates={"pending_tool_intent": pending},
    )
    metadata = {**session.metadata, "runtime_state": runtime_state}
    try:
        store.update_session_metadata(
            workspace=workspace,
            session_id=session.session.id,
            metadata=metadata,
        )
    except UnknownSessionError:
        logger.debug("tool intent persistence deferred for new session %s", session.session.id)


def clear_tool_execution_intent(
    store: SessionStore,
    workspace: Path,
    session: SessionState,
) -> SessionState:
    """Clear a pending tool intent in storage and the in-memory session.

    The operation is scoped by workspace and session ID and is a no-op when
    no pending intent is present. Returning the cleared session keeps the
    final runtime response aligned with the SQLite session truth.
    """
    cleared_session = session_without_tool_intent(session)
    try:
        persisted_session = store.load_session(
            workspace=workspace,
            session_id=session.session.id,
        ).session
    except UnknownSessionError:
        logger.debug("tool intent cleanup deferred for new session %s", session.session.id)
        return cleared_session
    validate_session_workspace(persisted_session, session_id=session.session.id, workspace=workspace)
    runtime_state = persisted_session.metadata.get("runtime_state")
    if not isinstance(runtime_state, dict) or "pending_tool_intent" not in runtime_state:
        return cleared_session
    state = _runtime_state_payload_with_updates(
        persisted_session.metadata,
        removed=frozenset({"pending_tool_intent"}),
    )
    try:
        store.update_session_metadata(
            workspace=workspace,
            session_id=session.session.id,
            metadata={**persisted_session.metadata, "runtime_state": state},
        )
    except UnknownSessionError:
        logger.debug("tool intent cleanup deferred for new session %s", session.session.id)
    return cleared_session


def waiting_reason_from_session(session: SessionState) -> str:
    raw_plan_state = session.metadata.get("plan_state")
    if raw_plan_state is None:
        return "waiting"
    plan_state = parse_plan_state_metadata(raw_plan_state)
    status = plan_state.get("status")
    if status == "waiting_approval":
        return "waiting_for_approval"
    if status == "waiting_question":
        return "waiting_for_question"
    return "waiting"


def resume_waiting_reason(response: RuntimeResponse) -> str:
    try:
        pending_approval_from_response(response)
    except ValueError:
        pass
    else:
        return "waiting_for_approval"
    if pending_question_from_response(response) is not None:
        return "waiting_for_question"
    return "waiting"


__all__ = [
    "DELEGATION_METADATA_KEYS",
    "PLAN_STATE_METADATA_KEYS",
    "PersistedDelegationMetadata",
    "PlanStateMetadata",
    "RUNTIME_STATE_METADATA_KEYS",
    "RuntimeStateMetadata",
    "SKILL_SNAPSHOT_METADATA_KEYS",
    "SkillSnapshotMetadata",
    "clear_tool_execution_intent",
    "continuity_state_from_session_metadata",
    "delegation_depth_from_metadata",
    "parse_delegation_metadata",
    "parse_plan_state_metadata",
    "parse_runtime_state_metadata",
    "parse_skill_snapshot_metadata",
    "persist_tool_execution_intent",
    "remaining_spawn_budget_from_metadata",
    "resume_waiting_reason",
    "runtime_state_acp",
    "runtime_state_context_compacted",
    "runtime_state_context_projection",
    "runtime_state_context_projection_summary",
    "runtime_state_context_transform_applied",
    "runtime_state_metadata_payload",
    "runtime_state_pending_tool_intent",
    "runtime_state_run_id",
    "runtime_state_todos",
    "runtime_state_value",
    "session_metadata_with_runtime_state_updates",
    "session_model_identity",
    "session_with_context_compacted_state",
    "session_with_context_transform_applied_state",
    "session_with_context_window_metadata",
    "session_with_context_window_payload_metadata",
    "session_with_current_acp_metadata",
    "session_with_plan_state",
    "session_with_run_id",
    "session_with_todo_state",
    "session_without_tool_intent",
    "waiting_reason_from_session",
]
