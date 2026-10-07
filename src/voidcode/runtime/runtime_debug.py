from __future__ import annotations

from ..core.tool_context import ArtifactInvalid, ArtifactMissing, ArtifactPage, ArtifactUnavailable
from ..core.turns import ReportedCall
from ..tools.output import read_tool_output_artifact
from .contracts import (
    RuntimeSessionDebugEvent,
    RuntimeSessionDebugFailure,
    RuntimeSessionDebugToolSummary,
    RuntimeSessionResult,
)
from .events import EventEnvelope
from .fact_codec import reported_call_from_event
from .permission import PendingApproval
from .question import PendingQuestion


def debug_event(event: EventEnvelope | None) -> RuntimeSessionDebugEvent | None:
    if event is None:
        return None
    return RuntimeSessionDebugEvent(
        sequence=event.sequence,
        event_type=event.event_type,
        source=event.source,
        payload=dict(event.payload),
    )


def current_debug_status(
    *,
    result: RuntimeSessionResult,
    active: bool,
    pending_approval: PendingApproval | None,
    pending_question: PendingQuestion | None,
) -> str:
    if active and result.session.status == "running":
        return "running"
    if pending_approval is not None:
        return "waiting_for_approval"
    if pending_question is not None:
        return "waiting_for_question"
    if active and result.session.status == "waiting":
        return "waiting_active"
    return result.session.status


def debug_session_state_inconsistency(
    *,
    result: RuntimeSessionResult,
    pending_approval: PendingApproval | None,
    pending_question: PendingQuestion | None,
    resume_checkpoint: dict[str, object] | None,
) -> str | None:
    raw_kind = resume_checkpoint.get("kind") if resume_checkpoint is not None else None
    checkpoint_kind = raw_kind if isinstance(raw_kind, str) else None
    if result.session.status == "waiting":
        if pending_approval is None and pending_question is None:
            return "waiting session is missing pending approval/question state"
        if pending_approval is not None and checkpoint_kind != "approval_wait":
            return "pending approval does not match the persisted resume checkpoint"
        if pending_question is not None and checkpoint_kind != "question_wait":
            return "pending question does not match the persisted resume checkpoint"
    if result.session.status in {"completed", "failed"}:
        if checkpoint_kind not in {None, "provider_failure_retryable", "terminal"}:
            return "terminal session resume checkpoint does not match persisted terminal state"
        if pending_approval is not None or pending_question is not None:
            return "terminal session still has pending approval/question state"
    return None


def debug_failure(
    *,
    result: RuntimeSessionResult,
    last_failure_event: RuntimeSessionDebugEvent | None,
    last_tool: RuntimeSessionDebugToolSummary | None,
    pending_approval: PendingApproval | None,
    pending_question: PendingQuestion | None,
    resume_checkpoint: dict[str, object] | None,
    persistence_error: str | None,
) -> RuntimeSessionDebugFailure | None:
    if persistence_error is not None:
        return RuntimeSessionDebugFailure(
            classification="session_state_inconsistency",
            message=persistence_error,
        )
    inconsistency_message = debug_session_state_inconsistency(
        result=result,
        pending_approval=pending_approval,
        pending_question=pending_question,
        resume_checkpoint=resume_checkpoint,
    )
    if inconsistency_message is not None:
        return RuntimeSessionDebugFailure(
            classification="session_state_inconsistency",
            message=inconsistency_message,
        )
    message = None
    classification = "runtime_internal_failure"
    if last_failure_event is not None:
        provider_error_kind = last_failure_event.payload.get("provider_error_kind")
        if isinstance(provider_error_kind, str) and provider_error_kind:
            classification = "provider_failure"
        raw_error = last_failure_event.payload.get("error")
        if raw_error is not None:
            message = str(raw_error)
    elif result.error is not None:
        message = result.error
    if message is None:
        if last_tool is not None and last_tool.status == "error":
            return RuntimeSessionDebugFailure(
                classification="tool_execution_failure",
                message=last_tool.summary,
            )
        return None
    lowered = message.lower()
    if "permission denied" in lowered:
        classification = "approval_denied"
    elif pending_approval is not None or "approval" in lowered or "question" in lowered:
        classification = "approval_interruption"
    elif "cancel" in lowered:
        classification = "cancelled"
    elif last_tool is not None and last_tool.status == "error":
        classification = "tool_execution_failure"
    elif "tool" in lowered:
        classification = "tool_execution_failure"
    return RuntimeSessionDebugFailure(classification=classification, message=message)


def artifact_debug_metadata(payload: dict[str, object]) -> dict[str, object]:
    artifact = payload.get("artifact")
    if not isinstance(artifact, dict):
        return {}
    artifact_metadata = dict(artifact)
    read_result = read_tool_output_artifact(artifact_metadata, offset=0, limit=0)
    if isinstance(read_result, ArtifactPage):
        artifact_metadata["status"] = "available"
        artifact_metadata["artifact_missing"] = False
    elif isinstance(read_result, ArtifactInvalid):
        artifact_metadata["status"] = "invalid"
        artifact_metadata["artifact_missing"] = True
    elif isinstance(read_result, ArtifactUnavailable):
        artifact_metadata["status"] = "missing"
        artifact_metadata["artifact_missing"] = True
    elif isinstance(read_result, ArtifactMissing):
        artifact_metadata["status"] = "artifact_not_found"
        artifact_metadata["artifact_missing"] = True
    artifact_metadata.pop("content", None)
    return artifact_metadata


def last_tool_summary(
    result: RuntimeSessionResult,
) -> RuntimeSessionDebugToolSummary | None:
    for event in reversed(result.transcript):
        if event.event_type != "runtime.tool_completed":
            continue
        payload = event.payload
        tool_name = payload.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        raw_status = payload.get("status")
        status = raw_status if isinstance(raw_status, str) and raw_status else "ok"
        if status not in {"ok", "error"}:
            status = "error" if payload.get("error") is not None else "ok"
        if status == "error":
            raw_diagnostics = payload.get("diagnostics")
            diagnostics = raw_diagnostics if isinstance(raw_diagnostics, dict) else {}
            summary_source = diagnostics.get("summary") or payload.get("error")
        else:
            summary_source = payload.get("content")
        summary = str(summary_source).strip() if summary_source is not None else tool_name
        if len(summary) > 160:
            summary = summary[:157] + "..."
        arguments = payload.get("arguments")
        artifact = artifact_debug_metadata(payload)
        return RuntimeSessionDebugToolSummary(
            tool_name=tool_name,
            status=status,
            summary=summary,
            arguments=(dict(arguments) if isinstance(arguments, dict) else {}),
            artifact=artifact,
            sequence=event.sequence,
        )
    return None


def prompt_from_events(events: tuple[EventEnvelope, ...]) -> str:
    if not events:
        return ""
    prompt = events[0].payload.get("prompt")
    if isinstance(prompt, str):
        return prompt
    return ""


def prompt_and_tool_results_from_debug_events(
    events: tuple[EventEnvelope, ...],
) -> tuple[str, list[ReportedCall]]:
    prompt = prompt_from_events(events)
    reports: list[ReportedCall] = []
    for event in events:
        report = reported_call_from_event(event)
        if report is None:
            if event.event_type == "runtime.tool_completed":
                raise ValueError("runtime.tool_completed event has no canonical report")
            continue
        reports.append(report)
    return prompt, reports


def operator_guidance(
    *,
    current_status: str,
    pending_approval: PendingApproval | None,
    pending_question: PendingQuestion | None,
    active: bool,
    resumable: bool,
    terminal: bool,
    failure: RuntimeSessionDebugFailure | None,
) -> tuple[str, str]:
    if pending_approval is not None:
        return (
            "resolve_approval",
            f"Resolve approval request {pending_approval.request_id} for {pending_approval.tool_name}.",
        )
    if pending_question is not None:
        return (
            "answer_question",
            f"Answer pending question request {pending_question.request_id} before resuming.",
        )
    if failure is not None and failure.classification == "session_state_inconsistency":
        return (
            "inspect_failure",
            "Inspect persisted session state before attempting resume or replay.",
        )
    if active:
        return ("wait", "Session is currently active in the runtime.")
    if terminal and failure is not None:
        if failure.classification == "provider_failure" and current_status == "failed":
            if not resumable:
                return (
                    "inspect_failure",
                    f"Inspect {failure.classification} and rerun if needed.",
                )
            return (
                "resume_provider_failure",
                "Resume this session to continue from the provider failure checkpoint.",
            )
        return ("inspect_failure", f"Inspect {failure.classification} and rerun if needed.")
    if terminal:
        return ("replay", "Session is terminal; replay or inspect transcript if needed.")
    if current_status == "waiting_active":
        return (
            "inspect_wait",
            "Session is waiting but still marked active; inspect runtime ownership.",
        )
    return ("inspect_session", "Inspect the persisted session state.")


__all__ = [
    "artifact_debug_metadata",
    "current_debug_status",
    "debug_event",
    "debug_failure",
    "debug_session_state_inconsistency",
    "last_tool_summary",
    "operator_guidance",
    "prompt_and_tool_results_from_debug_events",
    "prompt_from_events",
]
