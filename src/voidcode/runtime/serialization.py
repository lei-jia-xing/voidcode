from __future__ import annotations

from typing import cast

from .contracts import (
    RuntimeHookPresetSnapshot,
    RuntimeProviderContextSnapshot,
    RuntimeSessionDebugEvent,
    RuntimeSessionDebugSnapshot,
    RuntimeSessionRevertMarker,
)
from .events import redact_reasoning_payload, runtime_policy_observability_payload
from .session import SessionRef, SessionState


def serialize_session_debug_snapshot(
    snapshot: RuntimeSessionDebugSnapshot,
    *,
    show_thinking: bool = False,
) -> dict[str, object]:
    runtime_policy = _runtime_policy_debug_payload(snapshot)
    return {
        "session": _serialize_session_state(snapshot.session),
        **({"runtime_policy": runtime_policy} if runtime_policy is not None else {}),
        "prompt": snapshot.prompt,
        "persisted_status": snapshot.persisted_status,
        "current_status": snapshot.current_status,
        "active": snapshot.active,
        "resumable": snapshot.resumable,
        "replayable": snapshot.replayable,
        "terminal": snapshot.terminal,
        "resume_checkpoint_kind": snapshot.resume_checkpoint_kind,
        "pending_approval": (
            {
                "request_id": snapshot.pending_approval.request_id,
                "tool_name": snapshot.pending_approval.tool_name,
                "target_summary": snapshot.pending_approval.target_summary,
                "reason": snapshot.pending_approval.reason,
                "policy_mode": snapshot.pending_approval.policy_mode,
                "arguments": snapshot.pending_approval.arguments,
                "owner_session_id": snapshot.pending_approval.owner_session_id,
                "owner_parent_session_id": snapshot.pending_approval.owner_parent_session_id,
                "delegated_task_id": snapshot.pending_approval.delegated_task_id,
            }
            if snapshot.pending_approval is not None
            else None
        ),
        "pending_question": (
            {
                "request_id": snapshot.pending_question.request_id,
                "tool_name": snapshot.pending_question.tool_name,
                "question_count": snapshot.pending_question.question_count,
                "headers": list(snapshot.pending_question.headers),
            }
            if snapshot.pending_question is not None
            else None
        ),
        "revert_marker": serialize_revert_marker(snapshot.revert_marker),
        "last_event_sequence": snapshot.last_event_sequence,
        "last_relevant_event": serialize_session_debug_event(
            snapshot.last_relevant_event,
            show_thinking=show_thinking,
        ),
        "last_failure_event": serialize_session_debug_event(
            snapshot.last_failure_event,
            show_thinking=show_thinking,
        ),
        "failure": (
            {
                "classification": snapshot.failure.classification,
                "message": snapshot.failure.message,
            }
            if snapshot.failure is not None
            else None
        ),
        "last_tool": (
            {
                "tool_name": snapshot.last_tool.tool_name,
                "status": snapshot.last_tool.status,
                "summary": snapshot.last_tool.summary,
                "arguments": snapshot.last_tool.arguments,
                "artifact": getattr(snapshot.last_tool, "artifact", {}),
                "sequence": snapshot.last_tool.sequence,
            }
            if snapshot.last_tool is not None
            else None
        ),
        "provider_context": serialize_provider_context_snapshot(snapshot.provider_context),
        "hook_presets": serialize_hook_preset_snapshot(snapshot.hook_presets),
        "suggested_operator_action": snapshot.suggested_operator_action,
        "operator_guidance": snapshot.operator_guidance,
    }


def _runtime_policy_debug_payload(
    snapshot: RuntimeSessionDebugSnapshot,
) -> dict[str, object] | None:
    runtime_policy = snapshot.session.metadata.get("runtime_policy")
    if not isinstance(runtime_policy, dict):
        return None
    return runtime_policy_observability_payload(cast(dict[str, object], runtime_policy))


def serialize_hook_preset_snapshot(
    snapshot: RuntimeHookPresetSnapshot | None,
) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "refs": list(snapshot.refs),
        "kinds": list(snapshot.kinds),
        "source": snapshot.source,
        "count": snapshot.count,
    }


def serialize_provider_context_snapshot(
    snapshot: RuntimeProviderContextSnapshot | None,
) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "provider": snapshot.provider,
        "model": snapshot.model,
        "segment_count": snapshot.segment_count,
        "message_count": snapshot.message_count,
        "context_window": snapshot.context_window,
        "segments": [
            {
                "index": segment.index,
                "role": segment.role,
                "source": segment.source,
                "content": segment.content,
                "content_truncated": segment.content_truncated,
                "tool_call_id": segment.tool_call_id,
                "tool_name": segment.tool_name,
                "tool_arguments": segment.tool_arguments,
                "metadata": segment.metadata,
            }
            for segment in snapshot.segments
        ],
        "provider_messages": [
            {
                "index": message.index,
                "role": message.role,
                "source": message.source,
                "content": message.content,
                "content_truncated": message.content_truncated,
                "tool_call_id": message.tool_call_id,
                "tool_calls": list(message.tool_calls),
            }
            for message in snapshot.provider_messages
        ],
        "policy_decision": (
            {
                "mode": snapshot.policy_decision.mode,
                "action": snapshot.policy_decision.action,
                "blocked": snapshot.policy_decision.blocked,
                "diagnostic_count": snapshot.policy_decision.diagnostic_count,
                "diagnostic_codes": list(snapshot.policy_decision.diagnostic_codes),
                "blocking_diagnostic_codes": list(snapshot.policy_decision.blocking_diagnostic_codes),
                "message": snapshot.policy_decision.message,
            }
            if snapshot.policy_decision is not None
            else None
        ),
        "diagnostics": [
            {
                "severity": diagnostic.severity,
                "code": diagnostic.code,
                "message": diagnostic.message,
                "source": diagnostic.source,
                "segment_indices": list(diagnostic.segment_indices),
                "suggested_fix": diagnostic.suggested_fix,
                "details": diagnostic.details,
                "policy_action": diagnostic.policy_action,
                "policy_blocking": diagnostic.policy_blocking,
            }
            for diagnostic in snapshot.diagnostics
        ],
    }


def serialize_session_debug_event(
    event: RuntimeSessionDebugEvent | None,
    *,
    show_thinking: bool = False,
) -> dict[str, object] | None:
    if event is None:
        return None
    return {
        "sequence": event.sequence,
        "event_type": event.event_type,
        "source": event.source,
        "payload": redact_reasoning_payload(
            event.event_type,
            event.payload,
            show_thinking=show_thinking,
        ),
    }


def serialize_revert_marker(
    marker: RuntimeSessionRevertMarker | None,
) -> dict[str, object] | None:
    if marker is None:
        return None
    return {"sequence": marker.sequence, "active": marker.active}


def _serialize_session_ref(session_ref: SessionRef) -> dict[str, object]:
    payload: dict[str, object] = {"id": session_ref.id}
    if session_ref.parent_id is not None:
        payload["parent_id"] = session_ref.parent_id
    return payload


def _serialize_session_state(session: SessionState) -> dict[str, object]:
    return {
        "session": _serialize_session_ref(session.session),
        "status": session.status,
        "turn": session.turn,
        "metadata": session.metadata,
    }
