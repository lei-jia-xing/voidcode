from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from ...tools.contracts import ToolDiagnostics, ToolResult, ToolResultStatus
from ...tools.output import sanitize_tool_result_data
from ..context.continuity import verified_checkpoint_session_metadata
from ..contracts import RuntimeResponse
from ..permission import PendingApproval
from ..permission_policy import request_event_and_resolution_state
from ..question import PendingQuestion


@dataclass(frozen=True, slots=True)
class ApprovalResumeCheckpointState:
    prompt: str
    session_metadata: dict[str, object]
    tool_results: tuple[ToolResult, ...]


@dataclass(frozen=True, slots=True)
class PersistedResumeCheckpointEnvelope:
    kind: str
    version: int
    payload: dict[str, object]


def validate_pending_approval_matches_recorded_request(
    *,
    stored: RuntimeResponse,
    pending: PendingApproval,
    checkpoint: dict[str, object] | None,
) -> None:
    request_event, resolved = request_event_and_resolution_state(
        stored.events,
        request_kind="approval",
        request_id=pending.request_id,
    )
    if resolved:
        raise ValueError("approval request was already resolved; stale approval replay is not allowed")
    if request_event is None:
        if checkpoint is None:
            raise ValueError("persisted pending approval has no matching approval request event")
        if checkpoint.get("pending_approval_request_id") != pending.request_id:
            raise ValueError("persisted approval resume checkpoint request id does not match pending approval")
        if checkpoint.get("pending_approval_tool_name") != pending.tool_name or checkpoint.get("pending_approval_arguments") != pending.arguments:
            raise ValueError("persisted pending approval no longer matches the recorded approval request payload")
        if checkpoint.get("pending_approval_owner_session_id") != pending.owner_session_id:
            raise ValueError("persisted pending approval owner_session_id does not match the recorded approval request")
        if checkpoint.get("pending_approval_owner_parent_session_id") != pending.owner_parent_session_id:
            raise ValueError("persisted pending approval owner_parent_session_id does not match the recorded approval request")
        if checkpoint.get("pending_approval_delegated_task_id") != pending.delegated_task_id:
            raise ValueError("persisted pending approval delegated_task_id does not match the recorded approval request")
        checkpoint_sequence = checkpoint.get("pending_approval_request_event_sequence")
        if pending.request_event_sequence is not None and checkpoint_sequence is not None and checkpoint_sequence != pending.request_event_sequence:
            raise ValueError("persisted pending approval sequence does not match the recorded approval request")
        return
    if pending.request_event_sequence is not None and request_event.sequence != pending.request_event_sequence:
        raise ValueError("persisted pending approval sequence does not match the recorded approval request")
    payload = request_event.payload
    if payload.get("tool") != pending.tool_name or payload.get("arguments") != pending.arguments:
        raise ValueError("persisted pending approval no longer matches the recorded approval request payload")
    if payload.get("owner_session_id") != pending.owner_session_id:
        raise ValueError("persisted pending approval owner_session_id does not match the recorded approval request")
    if payload.get("owner_parent_session_id") != pending.owner_parent_session_id:
        raise ValueError("persisted pending approval owner_parent_session_id does not match the recorded approval request")
    if payload.get("delegated_task_id") != pending.delegated_task_id:
        raise ValueError("persisted pending approval delegated_task_id does not match the recorded approval request")


def validate_pending_question_matches_recorded_request(
    *,
    stored: RuntimeResponse,
    pending: PendingQuestion,
    checkpoint: dict[str, object] | None,
) -> None:
    request_event, resolved = request_event_and_resolution_state(
        stored.events,
        request_kind="question",
        request_id=pending.request_id,
    )
    if resolved:
        raise ValueError("question request was already answered; stale question replay is not allowed")
    expected_questions = [
        {
            "header": prompt.header,
            "question": prompt.question,
            "multiple": prompt.multiple,
            "options": [
                {
                    "label": option.label,
                    "description": option.description,
                }
                for option in prompt.options
            ],
        }
        for prompt in pending.prompts
    ]
    if request_event is None:
        if checkpoint is None:
            raise ValueError("persisted pending question has no matching question request event")
        if checkpoint.get("pending_question_request_id") != pending.request_id:
            raise ValueError("persisted question resume checkpoint request id does not match pending question")
        if checkpoint.get("pending_question_tool_name") != pending.tool_name:
            raise ValueError("persisted pending question tool does not match the recorded question request")
        if checkpoint.get("pending_question_prompts") != expected_questions:
            raise ValueError("persisted pending question no longer matches the recorded question request payload")
        return
    payload = request_event.payload
    if payload.get("tool") != pending.tool_name:
        raise ValueError("persisted pending question tool does not match the recorded question request")
    if payload.get("questions") != expected_questions:
        raise ValueError("persisted pending question no longer matches the recorded question request payload")


def checkpoint_state_from_payload(
    *,
    checkpoint_payload: dict[str, object],
    stored_metadata: dict[str, object],
    resume_label: Literal["approval", "question"],
) -> ApprovalResumeCheckpointState:
    prefix = f"persisted {resume_label} resume checkpoint"
    prompt = checkpoint_payload.get("prompt")
    session_metadata = checkpoint_payload.get("session_metadata")
    raw_tool_results = checkpoint_payload.get("tool_results")
    if not isinstance(prompt, str):
        raise ValueError(f"{prefix} prompt must be a string")
    if not isinstance(session_metadata, dict):
        raise ValueError(f"{prefix} session_metadata must be an object")
    recovered_metadata = verified_checkpoint_session_metadata(
        checkpoint_metadata=cast(dict[str, object], session_metadata),
        stored_metadata=stored_metadata,
    )
    if recovered_metadata is None:
        raise ValueError(f"{prefix} session_metadata does not match session")
    if not isinstance(raw_tool_results, list):
        raise ValueError(f"{prefix} tool_results must be a list")
    return ApprovalResumeCheckpointState(
        prompt=prompt,
        session_metadata=recovered_metadata,
        tool_results=tool_results_from_checkpoint(cast(list[object], raw_tool_results)),
    )


def approval_resume_state_from_checkpoint(
    *,
    checkpoint: dict[str, object] | None,
    pending: PendingApproval,
    stored_metadata: dict[str, object],
) -> ApprovalResumeCheckpointState:
    checkpoint_envelope = validated_resume_checkpoint_envelope(
        checkpoint=checkpoint,
        expected_kind="approval_wait",
    )
    checkpoint_payload = checkpoint_envelope.payload
    if checkpoint_payload.get("pending_approval_request_id") != pending.request_id:
        raise ValueError("persisted approval resume checkpoint request id does not match pending approval")
    checkpoint_snapshot_hash = checkpoint_payload.get("skill_snapshot_hash")
    stored_snapshot_payload = cast(
        dict[str, object] | None,
        stored_metadata.get("skill_snapshot"),
    )
    stored_snapshot_hash = stored_snapshot_payload.get("snapshot_hash") if isinstance(stored_snapshot_payload, dict) else None
    if checkpoint_snapshot_hash is not None and stored_snapshot_hash is not None and checkpoint_snapshot_hash != stored_snapshot_hash:
        raise ValueError("persisted approval resume checkpoint skill snapshot hash does not match session")
    return checkpoint_state_from_payload(
        checkpoint_payload=checkpoint_payload,
        stored_metadata=stored_metadata,
        resume_label="approval",
    )


def question_resume_state_from_checkpoint(
    *,
    checkpoint: dict[str, object] | None,
    pending: PendingQuestion,
    stored_metadata: dict[str, object],
) -> ApprovalResumeCheckpointState:
    checkpoint_envelope = validated_resume_checkpoint_envelope(
        checkpoint=checkpoint,
        expected_kind="question_wait",
    )
    checkpoint_payload = checkpoint_envelope.payload
    if checkpoint_payload.get("pending_question_request_id") != pending.request_id:
        raise ValueError("persisted question resume checkpoint request id does not match pending question")
    return checkpoint_state_from_payload(
        checkpoint_payload=checkpoint_payload,
        stored_metadata=stored_metadata,
        resume_label="question",
    )


def validated_resume_checkpoint_envelope(
    *,
    checkpoint: dict[str, object] | None,
    expected_kind: str,
) -> PersistedResumeCheckpointEnvelope:
    if checkpoint is None:
        raise ValueError("persisted resume checkpoint is required")
    kind = checkpoint.get("kind")
    if not isinstance(kind, str):
        raise ValueError("persisted resume checkpoint kind must be a string")
    if kind != expected_kind:
        raise ValueError(f"persisted resume checkpoint kind mismatch: expected {expected_kind!r}, got {kind!r}")
    version = checkpoint.get("version")
    if version != 1:
        raise ValueError(f"persisted resume checkpoint version mismatch: expected 1, got {version!r}")
    return PersistedResumeCheckpointEnvelope(kind=kind, version=1, payload=checkpoint)


def tool_results_from_checkpoint(raw_tool_results: list[object]) -> tuple[ToolResult, ...]:
    parsed: list[ToolResult] = []
    for raw_tool_result in raw_tool_results:
        if not isinstance(raw_tool_result, dict):
            raise ValueError("persisted resume checkpoint tool_results must contain objects")
        payload = cast(dict[str, object], raw_tool_result)
        tool_name = payload.get("tool_name")
        raw_status = payload.get("status")
        if raw_status == "ok":
            status: ToolResultStatus = "ok"
        elif raw_status == "error":
            status = "error"
        else:
            status = None
        data = payload.get("data")
        content = payload.get("content")
        error = payload.get("error")
        diagnostics = payload.get("diagnostics")
        if not isinstance(tool_name, str) or status is None or not isinstance(data, dict):
            raise ValueError("persisted resume checkpoint tool_results are malformed")
        if content is not None and not isinstance(content, str):
            raise ValueError("persisted resume checkpoint tool result content must be a string or null")
        if error is not None and not isinstance(error, str):
            raise ValueError("persisted resume checkpoint tool result error must be a string or null")
        parsed.append(
            ToolResult(
                tool_name=tool_name,
                content=content,
                status=status,
                data=sanitize_tool_result_data(cast(dict[str, object], data)),
                error=error,
                diagnostics=(ToolDiagnostics.from_payload(diagnostics) if diagnostics is not None else None),
                source="checkpoint",
            )
        )
    return tuple(parsed)
