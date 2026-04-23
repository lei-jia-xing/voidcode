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
        while args.block and result.status not in {"completed", "failed", "cancelled"}:
            if time.monotonic() >= deadline:
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
        }
        content = (
            result.summary_output
            or result.error
            or f"Background task {result.task_id}: {result.status}"
        )

        if args.full_session and result.child_session_id is not None:
            session_result = self._runtime.session_result(session_id=result.child_session_id)
            transcript = [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "source": event.source,
                    "payload": dict(event.payload),
                }
                for event in session_result.transcript[: max(args.message_limit, 1)]
            ]
            payload["session"] = {
                "session_id": session_result.session.session.id,
                "status": session_result.status,
                "summary": session_result.summary,
                "output": session_result.output,
                "error": session_result.error,
                "last_event_sequence": session_result.last_event_sequence,
                "transcript": transcript,
            }
            content = session_result.output or session_result.summary or content

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data=payload,
        )
