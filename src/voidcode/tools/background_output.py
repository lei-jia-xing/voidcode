from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ..runtime.contracts import BackgroundTaskResult, RuntimeSessionResult
from ..runtime.task import is_background_task_terminal
from .contracts import ToolCall, ToolDefinition, ToolResult


class BackgroundOutputRuntime(Protocol):
    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult: ...

    def session_result(self, *, session_id: str) -> RuntimeSessionResult: ...


class _BackgroundOutputArgs(BaseModel):
    task_id: str
    block: bool = False
    timeout: int = 60000
    full_session: bool = False
    message_limit: int = 20

    @field_validator("message_limit", mode="after")
    @classmethod
    def _validate_message_limit(cls, value: int) -> int:
        return min(max(value, 1), 100)

    @field_validator("task_id", mode="after")
    @classmethod
    def _validate_task_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("task_id must be a non-empty string")
        return stripped


class BackgroundOutputTool:
    definition = ToolDefinition(
        name="background_output",
        description="Read background task status and optionally child session results.",
        input_schema={
            "task_id": {"type": "string"},
            "block": {"type": "boolean"},
            "timeout": {"type": "integer"},
            "full_session": {"type": "boolean"},
            "message_limit": {"type": "integer"},
        },
        read_only=True,
    )

    def __init__(self, *, runtime: BackgroundOutputRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _BackgroundOutputArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError("background_output requires a non-empty task_id") from exc

        deadline = time.monotonic() + max(args.timeout, 1) / 1000
        result = self._runtime.load_background_task_result(
            args.task_id,
            emit_result_read_hook=not args.block,
        )
        block_timed_out = False
        while args.block and not is_background_task_terminal(result.status):
            if time.monotonic() >= deadline:
                block_timed_out = True
                break
            time.sleep(0.05)
            result = self._runtime.load_background_task_result(
                args.task_id,
                emit_result_read_hook=False,
            )
        if args.block:
            result = self._runtime.load_background_task_result(
                args.task_id,
                emit_result_read_hook=True,
            )
        safe_summary = _background_result_safe_summary(result)
        message_payload = {
            **result.delegated_message.as_payload(),
            "summary_output": safe_summary,
        }

        payload: dict[str, object] = {
            "task_id": result.task_id,
            "status": result.status,
            "parent_session_id": result.parent_session_id,
            "child_session_id": result.child_session_id,
            "retrieval_instruction": f'background_output(task_id="{result.task_id}")',
            "approval_blocked": result.approval_blocked,
            "summary_output": safe_summary,
            "error": result.error,
            "result_available": result.result_available,
            "delegation": result.delegated_execution.as_payload(),
            "message": message_payload,
            "handoff_summary": _background_task_handoff_summary(result=result),
            "block_timed_out": block_timed_out,
        }
        content = (
            safe_summary or result.error or f"Background task {result.task_id}: {result.status}"
        )
        empty_child_output = False

        if args.full_session and result.child_session_id is not None:
            session_result = self._runtime.session_result(session_id=result.child_session_id)
            empty_child_output = (
                session_result.status == "completed" and session_result.output == ""
            )
            safe_summary = _background_session_safe_summary(
                result=result,
                session_result=session_result,
            )
            transcript_events = session_result.transcript[: args.message_limit]
            transcript = [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "source": event.source,
                }
                for event in transcript_events
            ]
            output_available = session_result.output is not None
            full_session_reference = f"session:{session_result.session.session.id}"
            payload["session"] = {
                "session_id": session_result.session.session.id,
                "child_session_id": session_result.session.session.id,
                "status": session_result.status,
                "summary": session_result.summary,
                "error": session_result.error,
                "last_event_sequence": session_result.last_event_sequence,
                "message_limit": args.message_limit,
                "transcript_count": len(transcript),
                "transcript_truncated": len(session_result.transcript) > len(transcript),
                "transcript": transcript,
                "output_available": output_available,
                "full_output_preserved": output_available,
                "full_session_reference": full_session_reference,
                "retrieval_hint": (
                    "Use sessions resume "
                    f"{session_result.session.session.id} or "
                    f"background_output(task_id='{result.task_id}', "
                    "full_session=true) from an operator context to inspect full child output."
                ),
            }
            payload["summary_output"] = safe_summary
            payload["message"] = {
                **result.delegated_message.as_payload(),
                "summary_output": safe_summary,
            }
            content = _background_session_digest(
                result=result,
                session_result=session_result,
                safe_summary=safe_summary,
                transcript_count=len(transcript),
                transcript_truncated=len(session_result.transcript) > len(transcript),
                output_available=output_available,
                full_session_reference=full_session_reference,
            )

        guidance = _background_output_guidance(
            result=result,
            content=content,
            empty_child_output=empty_child_output,
            block_timed_out=block_timed_out,
        )
        if guidance is not None:
            payload["guidance"] = guidance
            content = f"{content}\n\nGuidance: {guidance}"

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data=payload,
            reference=_background_result_reference(result),
        )


def _background_output_guidance(
    *,
    result: BackgroundTaskResult,
    content: str,
    empty_child_output: bool = False,
    block_timed_out: bool = False,
) -> str | None:
    if block_timed_out:
        return (
            "Timed out waiting for the delegated child to finish. The returned status is current; "
            "report it or call background_output again later, but do not loop indefinitely."
        )
    if result.status == "failed":
        retry_hint = ""
        if result.child_session_id is not None:
            retry_hint = (
                f" If the user explicitly asks to retry or continue, start a delegated task with "
                f"session_id='{result.child_session_id}' so the child context is preserved."
            )
        return (
            "The delegated child failed. Inspect the returned error/session details, summarize the "
            "failure for the parent, and do not retry automatically unless the user "
            "explicitly asks. "
            "After repeated failures, stop retrying and escalate the failure with the latest error."
            f"{retry_hint}"
        )
    if result.status == "cancelled":
        return "The delegated child was cancelled; do not retry automatically."
    if result.status == "interrupted":
        return (
            "The delegated child was interrupted before completion. Treat this as a terminal "
            "runtime outcome, inspect the returned error/session details, and do not retry "
            "automatically unless the user explicitly asks."
        )
    if not result.result_available:
        return (
            "No child result is available yet. Report the current status or call background_output "
            "again later with block=true; do not loop indefinitely."
        )
    if result.status == "completed" and (empty_child_output or not content.strip()):
        return (
            "The delegated child completed with empty output. Treat this as an empty "
            "result, inspect full_session=true if needed, and continue without hidden "
            "retries."
        )
    return None


def _background_session_digest(
    *,
    result: BackgroundTaskResult,
    session_result: RuntimeSessionResult,
    safe_summary: str,
    transcript_count: int,
    transcript_truncated: bool,
    output_available: bool,
    full_session_reference: str,
) -> str:
    lines = [
        "Background task result digest:",
        f"- task_id: {result.task_id}",
        f"- status: {result.status}",
        f"- child_session_id: {session_result.session.session.id}",
        f"- summary: {safe_summary}",
        f"- full_output_preserved: {str(output_available).lower()}",
        f"- transcript_events_listed: {transcript_count}",
        f"- transcript_truncated: {str(transcript_truncated).lower()}",
        f"- retrieval_pointer: {full_session_reference}",
    ]
    if session_result.error:
        lines.append(f"- error: {session_result.error}")
    lines.append(
        "Use the child session reference to retrieve full output; raw child output is not injected "
        "into active provider context."
    )
    return "\n".join(lines)


def _background_result_safe_summary(result: BackgroundTaskResult) -> str | None:
    if result.child_session_id is None:
        return result.summary_output
    if result.status == "completed":
        return (
            f"Completed child session {result.child_session_id}; full output is preserved outside "
            "active context."
        )
    if result.status == "failed":
        return (
            f"Failed child session {result.child_session_id}; "
            "inspect the child session for details."
        )
    if result.approval_blocked:
        return result.summary_output
    if result.summary_output:
        return (
            f"{result.status.title()} child session {result.child_session_id}; "
            "details preserved by reference."
        )
    return None


def _background_session_safe_summary(
    *,
    result: BackgroundTaskResult,
    session_result: RuntimeSessionResult,
) -> str:
    child_session_id = session_result.session.session.id
    if session_result.status == "completed":
        return (
            f"Completed child session {child_session_id}; full output is preserved outside "
            "active context."
        )
    if session_result.status == "failed":
        return f"Failed child session {child_session_id}; inspect the child session for details."
    if result.approval_blocked and result.summary_output:
        return result.summary_output
    if result.summary_output:
        return (
            f"{result.status.title()} child session {child_session_id}; "
            "details preserved by reference."
        )
    return f"Background task {result.task_id}: {result.status}"


def _background_result_reference(result: BackgroundTaskResult) -> str | None:
    if result.child_session_id is None:
        return None
    return f"session:{result.child_session_id}"


def _background_task_handoff_summary(*, result: BackgroundTaskResult) -> dict[str, object]:
    blocked_reason = result.error or result.cancellation_cause
    if result.status == "cancelled" and blocked_reason is None:
        blocked_reason = "cancelled by parent"
    return {
        "objective": result.delegated_execution.routing.as_payload()
        if result.delegated_execution.routing is not None
        else None,
        "completed_work": result.summary_output if result.status == "completed" else None,
        "open_questions": result.approval_blocked,
        "files_touched": (),
        "verification": (),
        "blocked_reason": blocked_reason,
        "retrieval_instruction": f'background_output(task_id="{result.task_id}")',
    }
