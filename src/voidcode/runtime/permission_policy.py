from __future__ import annotations

from typing import Literal, cast

from ..tools.question import QuestionTool
from .contracts import RuntimeResponse
from .events import (
    RUNTIME_APPROVAL_REQUESTED,
    RUNTIME_QUESTION_ANSWERED,
    RUNTIME_QUESTION_REQUESTED,
    EventEnvelope,
)
from .permission import (
    OperationClass,
    PathScope,
    PendingApproval,
    PermissionDecision,
    PermissionPolicy,
)
from .question import PendingQuestion


def permission_decision_or_none(value: object) -> PermissionDecision | None:
    if value == "allow":
        return "allow"
    if value == "deny":
        return "deny"
    if value == "ask":
        return "ask"
    return None


def path_scope_or_none(value: object) -> PathScope | None:
    if value == "workspace":
        return "workspace"
    if value == "external":
        return "external"
    return None


def operation_class_or_none(value: object) -> OperationClass | None:
    if value == "read":
        return "read"
    if value == "write":
        return "write"
    if value == "execute":
        return "execute"
    return None


def pending_approval_from_response(response: RuntimeResponse) -> PendingApproval:
    approval_event = next(
        (event for event in reversed(response.events) if event.event_type == RUNTIME_APPROVAL_REQUESTED),
        None,
    )
    if approval_event is None:
        raise ValueError("waiting runtime response must include an approval event")
    payload = approval_event.payload
    required = {"request_id", "tool", "arguments", "target_summary", "reason", "policy"}
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"approval event is missing required fields: {missing!r}")
    raw_policy_value = payload["policy"]
    if not isinstance(raw_policy_value, dict):
        raise ValueError("approval event policy must be an object")
    raw_policy = cast(dict[str, object], raw_policy_value)
    raw_policy_mode = raw_policy.get("mode")
    policy_mode = permission_decision_or_none(raw_policy_mode)
    if policy_mode is None:
        raise ValueError(f"invalid approval policy mode: {raw_policy_mode}")
    request_id = payload["request_id"]
    tool_name = payload["tool"]
    arguments = payload["arguments"]
    target_summary = payload["target_summary"]
    reason = payload["reason"]
    if not isinstance(request_id, str) or not isinstance(tool_name, str):
        raise ValueError("approval event request_id and tool must be strings")
    if not isinstance(arguments, dict):
        raise ValueError("approval event arguments must be an object")
    if not isinstance(target_summary, str) or not isinstance(reason, str):
        raise ValueError("approval event target_summary and reason must be strings")
    path_scope = payload.get("path_scope")
    operation_class = payload.get("operation_class")
    return PendingApproval(
        request_id=request_id,
        tool_name=tool_name,
        arguments=cast(dict[str, object], arguments),
        target_summary=target_summary,
        reason=reason,
        policy_mode=policy_mode,
        request_event_sequence=approval_event.sequence,
        owner_session_id=(str(payload["owner_session_id"]) if payload.get("owner_session_id") is not None else None),
        owner_parent_session_id=(str(payload["owner_parent_session_id"]) if payload.get("owner_parent_session_id") is not None else None),
        delegated_task_id=(str(payload["delegated_task_id"]) if payload.get("delegated_task_id") is not None else None),
        path_scope=path_scope_or_none(path_scope),
        operation_class=operation_class_or_none(operation_class),
        canonical_path=(str(payload["canonical_path"]) if payload.get("canonical_path") is not None else None),
        matched_rule=(str(payload["matched_rule"]) if payload.get("matched_rule") is not None else None),
        policy_surface=(str(payload["policy_surface"]) if payload.get("policy_surface") is not None else None),
    )


def request_event_and_resolution_state(
    events: tuple[EventEnvelope, ...],
    *,
    request_kind: Literal["approval", "question"],
    request_id: str,
) -> tuple[EventEnvelope | None, bool]:
    request_event_type = RUNTIME_APPROVAL_REQUESTED if request_kind == "approval" else RUNTIME_QUESTION_REQUESTED
    resolution_event_type = "runtime.approval_resolved" if request_kind == "approval" else RUNTIME_QUESTION_ANSWERED
    request_event: EventEnvelope | None = None
    resolved = False
    for event in events:
        event_request_id = event.payload.get("request_id")
        if event_request_id != request_id:
            continue
        if event.event_type == request_event_type:
            request_event = event
        elif event.event_type == resolution_event_type:
            resolved = True
    return request_event, resolved


def pending_question_from_response(response: RuntimeResponse) -> PendingQuestion | None:
    answered_request_ids = {
        str(event.payload.get("request_id"))
        for event in response.events
        if event.event_type == RUNTIME_QUESTION_ANSWERED and event.payload.get("request_id")
    }
    for event in reversed(response.events):
        if event.event_type != RUNTIME_QUESTION_REQUESTED:
            continue
        payload = event.payload
        request_id = str(payload["request_id"])
        if request_id in answered_request_ids:
            continue
        raw_questions = payload.get("questions")
        if not isinstance(raw_questions, list):
            raise ValueError("waiting runtime response must include question prompts")
        return PendingQuestion(
            request_id=request_id,
            tool_name=str(payload.get("tool", QuestionTool.definition.name)),
            arguments={},
            prompts=QuestionTool.parse_prompts({"questions": cast(list[object], raw_questions)}),
        )
    return None


def permission_policy_for_session(
    *,
    base_policy: PermissionPolicy,
    metadata: dict[str, object] | None,
) -> PermissionPolicy:
    approval_mode: PermissionDecision = base_policy.mode
    if metadata is not None:
        persisted_runtime_config = metadata.get("runtime_config")
        if isinstance(persisted_runtime_config, dict):
            runtime_config = cast(dict[str, object], persisted_runtime_config)
            persisted_approval_mode = runtime_config.get("approval_mode")
            parsed_approval_mode = permission_decision_or_none(persisted_approval_mode)
            if parsed_approval_mode is not None:
                approval_mode = parsed_approval_mode
    return PermissionPolicy(mode=approval_mode)


def waiting_request_id_from_response(
    response: RuntimeResponse,
    *,
    request_kind: Literal["approval", "question"],
) -> str | None:
    if response.session.status != "waiting":
        return None
    target_event_type = RUNTIME_APPROVAL_REQUESTED if request_kind == "approval" else RUNTIME_QUESTION_REQUESTED
    for event in reversed(response.events):
        if event.event_type == target_event_type:
            request_id = event.payload.get("request_id")
            return str(request_id) if request_id is not None else None
    return None


def approval_request_id_from_waiting_response(response: RuntimeResponse) -> str | None:
    return waiting_request_id_from_response(response, request_kind="approval")
