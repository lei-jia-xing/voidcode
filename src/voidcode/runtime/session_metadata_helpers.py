from __future__ import annotations

from typing import cast

from .session import SessionState
from .todos import (
    runtime_todos_from_tool_payload,
    todo_event_payload,
    todo_state_payload,
)


def plan_state_from_metadata(
    metadata: dict[str, object],
    *,
    status: str | None = None,
    approval_request_id: str | None = None,
    blocked_tool: str | None = None,
    error: str | None = None,
) -> dict[str, object] | None:
    existing_plan_state = metadata.get("plan_state")
    if not isinstance(existing_plan_state, dict):
        return None
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
    raw_runtime_state = session.metadata.get("runtime_state")
    runtime_state = (
        dict(cast(dict[str, object], raw_runtime_state))
        if isinstance(raw_runtime_state, dict)
        else {}
    )
    continuity_payload_raw = context_window_payload.get("continuity_state")
    continuity_payload = (
        cast(dict[str, object], continuity_payload_raw)
        if isinstance(continuity_payload_raw, dict)
        else None
    )
    summary_anchor = context_window_payload.get("summary_anchor")
    summary_source = context_window_payload.get("summary_source")
    continuity_summary_payload = (
        {
            "anchor": summary_anchor,
            "source": summary_source,
            "distillation_source": (
                continuity_payload.get("distillation_source", "deterministic")
                if continuity_payload is not None
                else "deterministic"
            ),
        }
        if isinstance(summary_anchor, str)
        else None
    )
    return SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata={
            **session.metadata,
            "context_window": context_window_payload,
            "runtime_state": {
                **runtime_state,
                **({"continuity": continuity_payload} if continuity_payload is not None else {}),
                **(
                    {"continuity_summary": continuity_summary_payload}
                    if continuity_summary_payload is not None
                    else {}
                ),
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
    runtime_state = (
        dict(cast(dict[str, object], raw_runtime_state))
        if isinstance(raw_runtime_state, dict)
        else {}
    )
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


__all__ = [
    "plan_state_from_metadata",
    "session_with_context_window_payload_metadata",
    "session_with_todo_state",
]
