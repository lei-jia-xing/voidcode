from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from ...runtime.background_task_models import BackgroundTaskState, is_background_task_terminal
from ...runtime.contracts import (
    BackgroundTaskGroupResult,
    BackgroundTaskResult,
    RuntimeSessionResult,
    UnknownSessionError,
)
from .._pydantic_args import format_validation_error
from ..contracts import ToolCall, ToolDefinition, ToolResult
from ..runtime_context import current_runtime_tool_context


class BackgroundOutputRuntime(Protocol):
    def authorize_background_task_owner(self, task_id: str, *, parent_session_id: str | None) -> None: ...

    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult: ...

    def wait_for_background_task(self, task_id: str, *, timeout_seconds: float) -> BackgroundTaskState: ...

    def load_background_task_group_result(
        self,
        *,
        task_ids: tuple[str, ...] = (),
        parallel_group_id: str | None = None,
        parent_session_id: str | None = None,
        emit_result_read_hook: bool = True,
    ) -> object: ...

    def wait_for_background_task_group(
        self,
        *,
        task_ids: tuple[str, ...] = (),
        parallel_group_id: str | None = None,
        parent_session_id: str | None = None,
        timeout_seconds: float,
        emit_result_read_hook: bool = True,
    ) -> object: ...

    def session_result(self, *, session_id: str) -> RuntimeSessionResult: ...


class _BackgroundOutputArgs(BaseModel):
    task_id: str | None = None
    task_ids: list[str] | None = None
    parallel_group_id: str | None = None
    block: bool = False
    # Blocking waits use runtime lifecycle notifications and express timeout in milliseconds.
    # timeout is ignored for non-blocking reads; block=true requires at least one second.
    timeout: int = 60000
    full_session: bool = False
    message_limit: int = 20

    @model_validator(mode="after")
    def _validate_selectors(self) -> _BackgroundOutputArgs:
        selector_count = sum(value is not None for value in (self.task_id, self.task_ids, self.parallel_group_id))
        if selector_count != 1:
            raise ValueError("provide exactly one of task_id, task_ids, or parallel_group_id")
        if self.task_ids is not None:
            if not self.task_ids:
                raise ValueError("task_ids must contain at least one task id")
            if len(self.task_ids) > 100:
                raise ValueError("task_ids may contain at most 100 ids")
            if len(set(self.task_ids)) != len(self.task_ids):
                raise ValueError("task_ids must not contain duplicates")
        if self.block and self.timeout < 1000:
            raise ValueError("timeout must be at least 1000 milliseconds when block=true; wait for the completion reminder instead of polling")
        return self

    @field_validator("task_id", "parallel_group_id", mode="after")
    @classmethod
    def _validate_selector_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("selector strings must be non-empty")
        return stripped

    @field_validator("task_ids", mode="after")
    @classmethod
    def _validate_task_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for index, item in enumerate(value):
            stripped = item.strip()
            if not stripped:
                raise ValueError(f"task_ids[{index}] must be a non-empty string")
            normalized.append(stripped)
        return normalized

    @field_validator("timeout", mode="after")
    @classmethod
    def _validate_timeout(cls, value: int) -> int:
        if value < 0:
            raise ValueError("timeout must be a non-negative integer number of milliseconds")
        return value

    @field_validator("message_limit", mode="after")
    @classmethod
    def _validate_message_limit(cls, value: int) -> int:
        return min(max(value, 1), 100)


class BackgroundOutputTool:
    definition = ToolDefinition(
        name="background_output",
        description=(
            "Read background task status and optionally bounded child session results. "
            "Provide exactly one selector: task_id, task_ids, or parallel_group_id. "
            "block=true waits through the runtime lifecycle API; timeout is milliseconds and "
            "must be at least 1000ms for a blocking wait."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Single background task selector; mutually exclusive with the aggregate selectors.",
                },
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 100,
                    "uniqueItems": True,
                    "description": "Explicit task ids to aggregate; mutually exclusive with task_id and parallel_group_id.",
                },
                "parallel_group_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Runtime-owned parallel group selector; mutually exclusive with task_id and task_ids.",
                },
                "block": {
                    "type": "boolean",
                    "description": "When true, wait once through the runtime lifecycle API; when false, return an immediate snapshot.",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Milliseconds for block=true only; block=true requires at least 1000ms. Ignored for non-blocking reads.",
                },
                "full_session": {
                    "type": "boolean",
                    "description": (
                        "For task_id only, include bounded child session metadata and transcript preview; "
                        "aggregate selectors remain no-transcript projections."
                    ),
                },
                "message_limit": {
                    "type": "integer",
                    "description": "Maximum bounded transcript events for full_session; clamped to 1-100.",
                },
            },
            "oneOf": [
                {"required": ["task_id"], "not": {"anyOf": [{"required": ["task_ids"]}, {"required": ["parallel_group_id"]}]}},
                {"required": ["task_ids"], "not": {"anyOf": [{"required": ["task_id"]}, {"required": ["parallel_group_id"]}]}},
                {"required": ["parallel_group_id"], "not": {"anyOf": [{"required": ["task_id"]}, {"required": ["task_ids"]}]}},
            ],
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
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        # Group reads are deliberately runtime-context owned. The runtime
        # validates every selected task against this parent before loading any
        # result, so a model cannot inspect another session's children.
        if args.task_id is None:
            context = current_runtime_tool_context()
            if context is None:
                raise RuntimeError("background_output group reads require an active runtime tool invocation context")
            selected_ids = tuple(args.task_ids or ())
            group_id = args.parallel_group_id
            load_group = getattr(self._runtime, "load_background_task_group_result", None)
            wait_group = getattr(self._runtime, "wait_for_background_task_group", None)
            if not callable(load_group) or not callable(wait_group):
                raise RuntimeError("background_output group reads require runtime group result support")
            timeout_seconds = max(args.timeout, 0) / 1000
            group_timed_out = False
            group = cast(
                BackgroundTaskGroupResult,
                load_group(
                    task_ids=selected_ids,
                    parallel_group_id=group_id,
                    parent_session_id=context.session_id,
                    emit_result_read_hook=not args.block,
                ),
            )
            if args.block and not group.complete:
                group = cast(
                    BackgroundTaskGroupResult,
                    wait_group(
                        task_ids=selected_ids,
                        parallel_group_id=group_id,
                        parent_session_id=context.session_id,
                        timeout_seconds=timeout_seconds,
                        emit_result_read_hook=False,
                    ),
                )
                group_timed_out = group.timed_out
            group = cast(
                BackgroundTaskGroupResult,
                load_group(
                    task_ids=selected_ids,
                    parallel_group_id=group_id,
                    parent_session_id=context.session_id,
                    emit_result_read_hook=True,
                ),
            )
            if group_timed_out:
                group = BackgroundTaskGroupResult(
                    parallel_group_id=group.parallel_group_id,
                    expected_task_count=group.expected_task_count,
                    results=group.results,
                    timed_out=True,
                )
            return _background_group_tool_result(group)

        assert args.task_id is not None
        timeout_seconds = max(args.timeout, 0) / 1000
        context = current_runtime_tool_context()
        if context is not None:
            self._runtime.authorize_background_task_owner(
                args.task_id,
                parent_session_id=context.session_id,
            )
        result = self._runtime.load_background_task_result(
            args.task_id,
            emit_result_read_hook=not args.block,
        )
        block_timed_out = False
        if args.block and not is_background_task_terminal(result.status):
            waited = self._runtime.wait_for_background_task(args.task_id, timeout_seconds=timeout_seconds)
            block_timed_out = not is_background_task_terminal(waited.status)
            result = self._runtime.load_background_task_result(
                args.task_id,
                emit_result_read_hook=True,
            )
            if block_timed_out and is_background_task_terminal(result.status):
                block_timed_out = False
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
            "duration_seconds": result.duration_seconds,
            "tool_call_count": result.tool_call_count,
            "retrieval_instruction": f'background_output(task_id="{result.task_id}")',
            "approval_blocked": result.approval_blocked,
            "summary_output": safe_summary,
            "error": result.error,
            "result_available": result.result_available,
            "delegation": result.delegated_execution.as_payload(),
            "message": message_payload,
            "handoff_summary": _background_task_handoff_summary(result=result),
            "structured_output": result.structured_output,
            "schema_validation": (None if result.schema_validation is None else result.schema_validation.as_payload()),
            "progress": [dict(section) for section in result.progress],
            "block_timed_out": block_timed_out,
        }
        content = safe_summary or result.error or f"Background task {result.task_id}: {result.status}"
        empty_child_output = False

        if args.full_session and result.child_session_id is not None:
            try:
                session_result = self._runtime.session_result(session_id=result.child_session_id)
            except UnknownSessionError:
                session_result = None
            if session_result is not None:
                empty_child_output = _session_result_has_empty_output(session_result)
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
                provider_failure_details = _provider_failure_details_from_session_result(session_result)
                if provider_failure_details is not None:
                    payload["provider_failure"] = provider_failure_details
                content = _background_session_digest(
                    result=result,
                    session_result=session_result,
                    safe_summary=safe_summary,
                    transcript_count=len(transcript),
                    transcript_truncated=len(session_result.transcript) > len(transcript),
                    output_available=output_available,
                    full_session_reference=full_session_reference,
                    empty_child_output=empty_child_output,
                )

        guidance = _background_output_guidance(
            result=result,
            content=content,
            empty_child_output=empty_child_output,
            block_timed_out=block_timed_out,
        )
        payload["empty_child_output"] = empty_child_output
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


_MAX_GROUP_CONTENT_CHARS = 4000
_MAX_GROUP_VALUE_CHARS = 4000


def _bounded_text(value: str | None, *, limit: int = _MAX_GROUP_VALUE_CHARS) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _bounded_structured_output(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except TypeError, ValueError:
        return {"unavailable": "structured output could not be serialized"}
    if len(encoded) <= _MAX_GROUP_VALUE_CHARS:
        return value
    return {"truncated": True, "preview": encoded[: _MAX_GROUP_VALUE_CHARS - 1] + "…"}


def _background_group_tool_result(group: BackgroundTaskGroupResult) -> ToolResult:
    """Render only bounded per-task result metadata; never copy transcripts."""
    status = "completed" if group.complete else "running"
    failures = [result for result in group.results if result.status in {"failed", "cancelled", "interrupted"}]
    error = (
        "; ".join(
            f"{result.task_id}: {_bounded_text(result.error or result.cancellation_cause or result.status) or result.status}" for result in failures
        )
        or None
    )
    results: list[dict[str, object]] = []
    lines = [
        f"Background task group result: {group.parallel_group_id or 'explicit task ids'}",
        f"- status: {status}",
        f"- tasks: {len(group.results)}/{group.expected_task_count or len(group.results)}",
    ]
    if group.timed_out:
        lines.append("- wait: timed out; returned current task states")
    for result in group.results:
        summary = _background_result_safe_summary(result)
        item: dict[str, object] = {
            "task_id": result.task_id,
            "status": result.status,
            "summary": _bounded_text(summary),
            "error": _bounded_text(result.error or result.cancellation_cause),
            "structured_output": _bounded_structured_output(result.structured_output),
            "progress": _bounded_structured_output({"sections": list(result.progress)}),
            "result_available": result.result_available,
            "approval_blocked": result.approval_blocked,
            "child_session_id": result.child_session_id,
        }
        results.append(item)
        lines.append(f"- {result.task_id}: {result.status}; summary={summary or 'none'}")
    content = "\n".join(lines)
    content = _bounded_text(content, limit=_MAX_GROUP_CONTENT_CHARS) or "Background task group result"
    payload: dict[str, object] = {
        "parallel_group_id": group.parallel_group_id,
        "task_ids": list(group.task_ids),
        "expected_task_count": group.expected_task_count,
        "status": status,
        "complete": group.complete,
        "timed_out": group.timed_out,
        "counts": group.counts,
        "summary": _bounded_text(content),
        "error": error,
        "structured_output": [item["structured_output"] for item in results if item["structured_output"] is not None],
        "results": results,
        "retrieval_instruction": (
            f'background_output(parallel_group_id="{group.parallel_group_id}")'
            if group.parallel_group_id is not None
            else "background_output(task_ids=[...])"
        ),
        "block_timed_out": group.timed_out,
    }
    return ToolResult(tool_name="background_output", status="ok", content=content, data=payload)


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
            "wait for the runtime completion reminder and report it or continue other work. "
            "Do not call background_output again immediately; only retry after a meaningful state "
            "change, and do not loop indefinitely."
        )
    if result.status == "failed":
        return (
            "The delegated child failed. Inspect the returned error/session details, summarize the "
            "failure for the parent, and do not retry automatically unless the user explicitly asks. "
            "Re-delegate a fresh task if the user requests a retry. After repeated failures, stop "
            "retrying and escalate the failure with the latest error."
        )
    if result.status == "cancelled":
        return "The delegated child was cancelled; do not retry automatically."
    if result.status == "interrupted":
        return (
            "The delegated child was interrupted before completion. Treat this as a terminal runtime "
            "outcome, inspect the returned error/session details, and do not retry automatically "
            "unless the user explicitly asks."
        )
    if not result.result_available:
        return (
            "No child result is available yet. Wait for the runtime completion reminder, report the "
            "current status, or use background_output(block=true) only when intentionally waiting in "
            "this turn; do not call again immediately or loop indefinitely."
        )
    if result.status == "completed" and (empty_child_output or not content.strip()):
        return (
            "The delegated child completed with empty output. Treat this as an empty result, inspect "
            "full_session=true if needed, and continue without hidden retries."
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
    empty_child_output: bool = False,
) -> str:
    if empty_child_output:
        return (
            "The delegated child completed with empty output. Treat this as an empty result, "
            "inspect full_session=true if needed, and continue without hidden retries."
        )
    lines = [
        "Background task result digest:",
        f"- task_id: {result.task_id}",
        f"- status: {result.status}",
        f"- child_session_id: {session_result.session.session.id}",
        f"- duration_seconds: {result.duration_seconds}",
        f"- tool_call_count: {result.tool_call_count}",
        f"- summary: {safe_summary}",
        f"- full_output_preserved: {str(output_available).lower()}",
        f"- transcript_events_listed: {transcript_count}",
        f"- transcript_truncated: {str(transcript_truncated).lower()}",
        f"- retrieval_pointer: {full_session_reference}",
    ]
    if session_result.error:
        lines.append(f"- error: {session_result.error}")
    lines.append("Use the child session reference to retrieve full output; raw child output is not injected into active provider context.")
    return "\n".join(lines)


def _session_result_has_empty_output(session_result: RuntimeSessionResult) -> bool:
    if session_result.status != "completed":
        return False
    output = session_result.output
    return output is None or output == ""


def _background_result_safe_summary(result: BackgroundTaskResult) -> str | None:
    if result.child_session_id is None:
        return result.summary_output
    if result.status == "completed":
        return f"Completed child session {result.child_session_id}; full output is preserved outside active context."
    if result.status == "failed":
        return f"Failed child session {result.child_session_id}; inspect the child session for details."
    if result.approval_blocked:
        return result.summary_output
    if result.summary_output:
        return f"{result.status.title()} child session {result.child_session_id}; details preserved by reference."
    return None


def _background_session_safe_summary(
    *,
    result: BackgroundTaskResult,
    session_result: RuntimeSessionResult,
) -> str:
    child_session_id = session_result.session.session.id
    if session_result.status == "completed":
        return f"Completed child session {child_session_id}; full output is preserved outside active context."
    if session_result.status == "failed":
        return f"Failed child session {child_session_id}; inspect the child session for details."
    if result.approval_blocked and result.summary_output:
        return result.summary_output
    if result.summary_output:
        return f"{result.status.title()} child session {child_session_id}; details preserved by reference."
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
        "objective": result.delegated_execution.routing.as_payload() if result.delegated_execution.routing is not None else None,
        "summary": result.summary_output if result.status == "completed" else None,
        "structured_output": result.structured_output,
        "schema_validation": (None if result.schema_validation is None else result.schema_validation.as_payload()),
        "verification": {
            "duration_seconds": result.duration_seconds,
            "tool_call_count": result.tool_call_count,
        },
        "blocked_reason": blocked_reason,
        "retrieval_instruction": f'background_output(task_id="{result.task_id}")',
    }


def _provider_failure_details_from_session_result(
    session_result: RuntimeSessionResult,
) -> dict[str, object] | None:
    for event in reversed(session_result.transcript):
        if event.event_type != "runtime.failed":
            continue
        payload = event.payload
        provider_error_kind = payload.get("provider_error_kind")
        provider_error_details = payload.get("provider_error_details")
        if not isinstance(provider_error_kind, str) and not isinstance(provider_error_details, dict):
            continue
        details: dict[str, object] = {}
        if isinstance(provider_error_kind, str):
            details["provider_error_kind"] = provider_error_kind
        provider = payload.get("provider")
        if isinstance(provider, str):
            details["provider"] = provider
        model = payload.get("model")
        if isinstance(model, str):
            details["model"] = model
        if isinstance(provider_error_details, dict):
            details["provider_error_details"] = provider_error_details
        return details or None
    return None
