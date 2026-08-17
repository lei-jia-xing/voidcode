from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .context_window import (
    ContextProjection,
    RuntimeContextWindow,
    continuity_state_from_metadata_payload,
)
from .contracts import RuntimeResponse, UnknownSessionError
from .permission import DelegationGovernance
from .permission_policy import (
    pending_approval_from_response,
    pending_question_from_response,
)
from .session import SessionState, validate_session_workspace
from .todos import (
    runtime_todos_equal,
    runtime_todos_from_state_payload,
    runtime_todos_from_tool_payload,
    todo_event_payload,
    todo_state_payload,
)

if TYPE_CHECKING:
    from .acp import AcpAdapterState
    from .storage import SessionStore

logger = logging.getLogger(__name__)

_DELEGATION_GOVERNANCE = DelegationGovernance()


def _coerce_int_like(value: object | None, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


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

    return plan_state


def session_with_context_window_payload_metadata(
    session: SessionState,
    context_window_payload: dict[str, object],
) -> SessionState:
    if "continuity_state" in context_window_payload:
        raise ValueError("legacy continuity_state context metadata is no longer supported")
    raw_runtime_state = session.metadata.get("runtime_state")
    if raw_runtime_state is not None and not isinstance(raw_runtime_state, dict):
        raise ValueError("persisted runtime_state must be an object")
    runtime_state = dict(cast(dict[str, object], raw_runtime_state or {}))
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
    return SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata={
            **metadata,
            "context_window": context_window_payload,
            "runtime_state": {
                **runtime_state,
                **({"context_projection": continuity_payload} if continuity_payload is not None else {}),
                **({"context_projection_summary": continuity_summary_payload} if continuity_summary_payload is not None else {}),
            },
        },
    )


def session_with_todo_state(
    session: SessionState,
    *,
    raw_todos: object,
    revision: int,
) -> tuple[SessionState, dict[str, object]]:
    raw_runtime_state = session.metadata.get("runtime_state")
    runtime_state = dict(cast(dict[str, object], raw_runtime_state)) if isinstance(raw_runtime_state, dict) else {}
    todos = runtime_todos_from_tool_payload(raw_todos, updated_at=revision)
    state_payload = todo_state_payload(todos, revision=revision)
    runtime_state["todos"] = state_payload
    next_session = SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata={
            **session.metadata,
            "runtime_state": runtime_state,
        },
    )
    event_payload = todo_event_payload(
        session_id=session.session.id,
        todos=todos,
        revision=revision,
    )
    return next_session, event_payload


def todo_state_matches_payload(
    session: SessionState,
    *,
    raw_todos: object,
    revision: int,
) -> bool:
    raw_runtime_state = session.metadata.get("runtime_state")
    if not isinstance(raw_runtime_state, dict):
        return False
    runtime_state = cast(dict[str, object], raw_runtime_state)
    raw_todo_state = runtime_state.get("todos")
    if not isinstance(raw_todo_state, dict):
        return False
    typed_todo_state = cast(dict[str, object], raw_todo_state)
    current = runtime_todos_from_state_payload(typed_todo_state.get("todos"))
    return runtime_todos_equal(current, raw_todos=raw_todos, updated_at=revision)


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
            plan_state = {"status": status}
            if approval_request_id is not None:
                plan_state["approval_request_id"] = approval_request_id
            if blocked_tool is not None:
                plan_state["blocked_tool"] = blocked_tool
            if error is not None:
                plan_state["last_error"] = error
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
    if metadata is None:
        return 0
    raw_delegation = metadata.get("delegation")
    if not isinstance(raw_delegation, dict):
        return 0
    delegation = cast(dict[str, object], raw_delegation)
    return max(0, _coerce_int_like(delegation.get("depth"), 0))


def remaining_spawn_budget_from_metadata(metadata: dict[str, object] | None) -> int:
    if metadata is None:
        return _DELEGATION_GOVERNANCE.spawn_budget
    raw_delegation = metadata.get("delegation")
    if not isinstance(raw_delegation, dict):
        return _DELEGATION_GOVERNANCE.spawn_budget
    delegation = cast(dict[str, object], raw_delegation)
    remaining = _coerce_int_like(
        delegation.get("remaining_spawn_budget"),
        _DELEGATION_GOVERNANCE.spawn_budget,
    )
    return max(0, remaining)


def continuity_state_from_session_metadata(
    session_metadata: dict[str, object],
) -> ContextProjection | None:
    runtime_state = session_metadata.get("runtime_state")
    if not isinstance(runtime_state, dict):
        return None
    runtime_state_payload = cast(dict[str, object], runtime_state)
    continuity = runtime_state_payload.get("context_projection")
    if not isinstance(continuity, dict):
        return None
    return continuity_state_from_metadata_payload(cast(dict[str, object], continuity))


def _runtime_state_metadata_with_acp_state(
    metadata: dict[str, object],
    acp_state: AcpAdapterState,
) -> dict[str, object]:
    runtime_state = metadata.get("runtime_state")
    if runtime_state is None:
        runtime_state_metadata: dict[str, object] = {}
    elif isinstance(runtime_state, dict):
        runtime_state_metadata = dict(cast(dict[str, object], runtime_state))
    else:
        runtime_state_metadata = {}
    runtime_state_metadata["acp"] = {
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
    runtime_state = session.metadata.get("runtime_state")
    state = dict(cast(dict[str, object], runtime_state)) if isinstance(runtime_state, dict) else {}
    pending = dict(intent)
    state["pending_tool_intent"] = pending
    metadata = {**session.metadata, "runtime_state": state}
    try:
        store.update_session_metadata(
            workspace=workspace,
            session_id=session.session.id,
            metadata=metadata,
        )
    except UnknownSessionError:
        # The initial run snapshot may not have committed yet.
        logger.debug("tool intent persistence deferred for new session %s", session.session.id)


def clear_tool_execution_intent(
    store: SessionStore,
    workspace: Path,
    session: SessionState,
) -> None:
    try:
        persisted_session = store.load_session(
            workspace=workspace,
            session_id=session.session.id,
        ).session
    except UnknownSessionError:
        logger.debug("tool intent cleanup deferred for new session %s", session.session.id)
        return
    validate_session_workspace(persisted_session, session_id=session.session.id, workspace=workspace)
    runtime_state = persisted_session.metadata.get("runtime_state")
    if not isinstance(runtime_state, dict) or "pending_tool_intent" not in runtime_state:
        return
    state = dict(cast(dict[str, object], runtime_state))
    state.pop("pending_tool_intent", None)
    try:
        store.update_session_metadata(
            workspace=workspace,
            session_id=session.session.id,
            metadata={**persisted_session.metadata, "runtime_state": state},
        )
    except UnknownSessionError:
        logger.debug("tool intent cleanup deferred for new session %s", session.session.id)


def waiting_reason_from_session(session: SessionState) -> str:
    plan_state = session.metadata.get("plan_state")
    if not isinstance(plan_state, dict):
        return "waiting"
    plan_state_payload = cast(dict[str, object], plan_state)
    status = plan_state_payload.get("status")
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
    "clear_tool_execution_intent",
    "continuity_state_from_session_metadata",
    "delegation_depth_from_metadata",
    "persist_tool_execution_intent",
    "plan_state_from_metadata",
    "remaining_spawn_budget_from_metadata",
    "resume_waiting_reason",
    "session_model_identity",
    "session_with_context_window_metadata",
    "session_with_context_window_payload_metadata",
    "session_with_current_acp_metadata",
    "session_with_plan_state",
    "session_with_todo_state",
    "todo_state_matches_payload",
    "waiting_reason_from_session",
]
