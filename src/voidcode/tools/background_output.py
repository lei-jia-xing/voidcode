from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ..runtime.contracts import BackgroundTaskResult, RuntimeSessionResult
from .contracts import ToolCall, ToolDefinition, ToolResult


class BackgroundOutputRuntime(Protocol):
    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult: ...

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
        result = self._runtime.load_background_task_result(args.task_id)
        block_timed_out = False
        while args.block and result.status not in {"completed", "failed", "cancelled"}:
            if time.monotonic() >= deadline:
                block_timed_out = True
                break
            time.sleep(0.05)
            result = self._runtime.load_background_task_result(args.task_id)

        payload: dict[str, object] = {
            "task_id": result.task_id,
            "status": result.status,
            "parent_session_id": result.parent_session_id,
            "child_session_id": result.child_session_id,
            "approval_blocked": result.approval_blocked,
            "summary_output": result.summary_output,
            "error": result.error,
            "result_available": result.result_available,
            "delegation": result.delegated_execution.as_payload(),
            "message": result.delegated_message.as_payload(),
            "block_timed_out": block_timed_out,
        }
        content = (
            result.summary_output
            or result.error
            or f"Background task {result.task_id}: {result.status}"
        )
        empty_child_output = False

        if args.full_session and result.child_session_id is not None:
            session_result = self._runtime.session_result(session_id=result.child_session_id)
            empty_child_output = (
                session_result.status == "completed" and session_result.output == ""
            )
            transcript_events = session_result.transcript[: args.message_limit]
            transcript = [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "source": event.source,
                    "payload": dict(event.payload),
                }
                for event in transcript_events
            ]
            payload["session"] = {
                "session_id": session_result.session.session.id,
                "child_session_id": session_result.session.session.id,
                "status": session_result.status,
                "summary": session_result.summary,
                "output": session_result.output,
                "error": session_result.error,
                "last_event_sequence": session_result.last_event_sequence,
                "message_limit": args.message_limit,
                "transcript_count": len(transcript),
                "transcript_truncated": len(session_result.transcript) > len(transcript),
                "transcript": transcript,
            }
            content = session_result.output or session_result.summary or content

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
