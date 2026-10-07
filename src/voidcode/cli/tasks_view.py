"""Delegated background-task payloads, plain rows, and next-step guidance."""

from __future__ import annotations

import shlex
from pathlib import Path

from ..runtime.background.models import (
    BackgroundTaskObservability,
    BackgroundTaskState,
    SchemaValidation,
    StoredBackgroundTaskSummary,
)
from ..runtime.background.routing import SubagentRoutingIdentity
from ..runtime.contracts import BackgroundTaskResult
from .output import format_named_record


def _append_observability_and_routing(
    fields: list[tuple[str, object]],
    observability: BackgroundTaskObservability | None,
    routing: SubagentRoutingIdentity | None,
) -> None:
    if observability is not None:
        fields.append(("waiting_reason", observability.waiting_reason))
        if observability.queue_position is not None:
            fields.append(("queue_position", observability.queue_position))
        if observability.terminal_reason is not None:
            fields.append(("terminal_reason", observability.terminal_reason))
        if observability.concurrency is not None:
            fields.append(("active_worker_slots", observability.concurrency.active_worker_slots))
            fields.append(("concurrency_limit", observability.concurrency.limit))
            fields.append(("queued_total", observability.concurrency.queued_total))
        if observability.retry is not None:
            fields.append(("retry_count", observability.retry.retry_count))
            fields.append(("retry_backoff_seconds", observability.retry.backoff_seconds))
    if routing is not None:
        fields.append(("delegation_mode", routing.mode))
        if routing.subagent_type is not None:
            fields.append(("subagent_type", routing.subagent_type))
        if routing.description is not None:
            fields.append(("description", routing.description))
        if routing.command is not None:
            fields.append(("command", routing.command))


def _background_task_fields(task: BackgroundTaskState) -> list[tuple[str, object]]:
    fields: list[tuple[str, object]] = [
        ("id", task.task.id),
        ("status", task.status),
        ("parent_session_id", task.parent_session_id),
        ("requested_child_session_id", task.request.session_id),
        ("child_session_id", task.child_session_id),
        ("approval_request_id", task.approval_request_id),
        ("question_request_id", task.question_request_id),
        ("result_available", task.result_available),
    ]
    if task.keep_alive:
        fields.append(("keep_alive", True))
    if task.steer_prompt is not None:
        fields.append(("steer_prompt", task.steer_prompt))
    if task.cancellation_cause is not None:
        fields.append(("cancellation_cause", task.cancellation_cause))
    if task.error is not None:
        fields.append(("error", task.error))
    _append_observability_and_routing(fields, task.observability, task.routing_identity)
    return fields


def _background_task_routing_payload(routing: SubagentRoutingIdentity | None) -> dict[str, object] | None:
    if routing is None:
        return None
    return {
        key: value
        for key, value in {
            "mode": routing.mode,
            "subagent_type": routing.subagent_type,
            "description": routing.description,
            "command": routing.command,
        }.items()
        if value is not None
    }


def _background_task_observability_payload(
    task_or_result: BackgroundTaskState | BackgroundTaskResult | StoredBackgroundTaskSummary,
) -> dict[str, object] | None:
    observability = task_or_result.observability
    if observability is None:
        return None
    return observability.as_payload()


def _background_task_error_type(error: str | None) -> str | None:
    if error is None:
        return None
    normalized = error.lower()
    if any(token in normalized for token in ("provider", "model", "api key", "unreachable")):
        return "provider"
    if any(token in normalized for token in ("tool", "write", "read", "shell_exec", "permission")):
        return "tool"
    return "runtime"


def background_task_next_steps(
    *,
    task_id: str,
    status: str,
    workspace: Path,
    child_session_id: str | None,
    approval_request_id: str | None,
    question_request_id: str | None,
    result_available: bool,
    error: str | None,
) -> list[str]:
    workspace_text = workspace.as_posix()
    workspace_arg = f"--workspace {shlex.quote(workspace_text)}"
    steps: list[str] = []
    if approval_request_id is not None and child_session_id is not None:
        steps.append(
            "Resolve approval: "
            f"voidcode sessions resume {child_session_id} {workspace_arg} "
            f"--approval-request-id {approval_request_id} --approval-decision allow"
        )
        steps.append(f"Cancel delegated task: voidcode tasks cancel {task_id} {workspace_arg}")
    elif question_request_id is not None and child_session_id is not None:
        steps.append(
            "Answer question: "
            f"voidcode sessions answer {child_session_id} {workspace_arg} "
            f"--question-request-id {question_request_id} --response <answer>"
        )
        steps.append(f"Inspect waiting child session: voidcode sessions debug {child_session_id} {workspace_arg}")
        steps.append(f"Cancel delegated task: voidcode tasks cancel {task_id} {workspace_arg}")
    elif status in {"queued", "running"}:
        steps.append(f"Refresh state: voidcode tasks status {task_id} {workspace_arg}")
        steps.append(f"Read partial result view: voidcode tasks output {task_id} {workspace_arg}")
        steps.append(f"Cancel delegated task: voidcode tasks cancel {task_id} {workspace_arg}")
    elif status == "idle":
        steps.append(f'Dispatch the next worker turn: voidcode tasks steer {task_id} "<prompt>" {workspace_arg}')
        steps.append(f"Refresh state: voidcode tasks status {task_id} {workspace_arg}")
        steps.append(f"Cancel delegated task: voidcode tasks cancel {task_id} {workspace_arg}")
    elif status == "completed":
        steps.append(f"Read output: voidcode tasks output {task_id} {workspace_arg}")
        if child_session_id is not None:
            steps.append(f"Replay child session: voidcode sessions resume {child_session_id} {workspace_arg}")
    elif status == "failed":
        error_type = _background_task_error_type(error)
        if result_available:
            steps.append(f"Inspect failure output: voidcode tasks output {task_id} {workspace_arg}")
        if child_session_id is not None:
            steps.append(f"Resume child context: voidcode sessions resume {child_session_id} {workspace_arg}")
        if error_type == "provider":
            steps.append("Check provider configuration: voidcode provider inspect <provider>")
        elif error_type == "tool":
            steps.append("Inspect the child session events to find the failed tool call and approval state.")
        else:
            steps.append("Inspect runtime events and retry explicitly from the parent flow if needed.")
        steps.append(f"Retry delegated task: voidcode tasks retry {task_id} {workspace_arg}")
    elif status == "cancelled":
        steps.append(f"Inspect final task state: voidcode tasks status {task_id} {workspace_arg}")
        steps.append(f"Retry delegated task: voidcode tasks retry {task_id} {workspace_arg}")
    elif status == "interrupted":
        if result_available:
            steps.append(f"Inspect interrupted output: voidcode tasks output {task_id} {workspace_arg}")
        steps.append(f"Retry delegated task: voidcode tasks retry {task_id} {workspace_arg}")
    return steps


def background_task_state_payload(task: BackgroundTaskState, *, workspace: Path) -> dict[str, object]:
    error = task.error
    cancellation_cause = task.cancellation_cause
    error_type = _background_task_error_type(error)
    next_steps = background_task_next_steps(
        task_id=task.task.id,
        status=task.status,
        workspace=workspace,
        child_session_id=task.child_session_id,
        approval_request_id=task.approval_request_id,
        question_request_id=task.question_request_id,
        result_available=task.result_available,
        error=error,
    )
    payload: dict[str, object] = {
        "task_id": task.task.id,
        "status": task.status,
        "parent_session_id": task.parent_session_id,
        "requested_child_session_id": task.request.session_id,
        "child_session_id": task.child_session_id,
        "approval_request_id": task.approval_request_id,
        "question_request_id": task.question_request_id,
        "approval_blocked": task.approval_request_id is not None,
        "result_available": task.result_available,
        "keep_alive": task.keep_alive,
        "steer_prompt": task.steer_prompt,
        "cancellation_cause": cancellation_cause,
        "error": error,
        "error_type": error_type,
        "routing": _background_task_routing_payload(task.routing_identity),
        "observability": _background_task_observability_payload(task),
        "output_schema": task.output_schema,
        "schema_mode": task.schema_mode,
        "structured_output": task.structured_output,
        "schema_validation": _background_task_schema_validation_payload(task.schema_validation),
        "next_steps": next_steps,
    }
    return payload


def background_task_result_payload(result: BackgroundTaskResult, *, workspace: Path) -> dict[str, object]:
    cancellation_cause = result.cancellation_cause
    error_type = _background_task_error_type(result.error)
    next_steps = background_task_next_steps(
        task_id=result.task_id,
        status=result.status,
        workspace=workspace,
        child_session_id=result.child_session_id,
        approval_request_id=result.approval_request_id,
        question_request_id=result.question_request_id,
        result_available=result.result_available,
        error=result.error,
    )
    return {
        "task_id": result.task_id,
        "status": result.status,
        "parent_session_id": result.parent_session_id,
        "requested_child_session_id": result.requested_child_session_id,
        "child_session_id": result.child_session_id,
        "approval_request_id": result.approval_request_id,
        "question_request_id": result.question_request_id,
        "approval_blocked": result.approval_blocked,
        "result_available": result.result_available,
        "summary_output": result.summary_output,
        "error": result.error,
        "error_type": error_type,
        "cancellation_cause": cancellation_cause,
        "routing": _background_task_routing_payload(result.routing),
        "observability": _background_task_observability_payload(result),
        "structured_output": result.structured_output,
        "handoff": None if result.handoff is None else result.handoff.as_payload(),
        "schema_validation": _background_task_schema_validation_payload(result.schema_validation),
        "next_steps": next_steps,
    }


def _background_task_schema_validation_payload(schema_validation: SchemaValidation | None) -> dict[str, object] | None:
    if schema_validation is None:
        return None
    return schema_validation.as_payload()


def background_task_summary_payload(task: StoredBackgroundTaskSummary) -> dict[str, object]:
    error = task.error
    return {
        "task_id": task.task.id,
        "status": task.status,
        "child_session_id": task.session_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "prompt": task.prompt,
        "keep_alive": task.keep_alive,
        "steer_prompt": task.steer_prompt,
        "output_schema": task.output_schema,
        "schema_mode": task.schema_mode,
        "error": error,
        "error_type": _background_task_error_type(error),
        "observability": _background_task_observability_payload(task),
    }


def print_background_task_guidance(payload: dict[str, object]) -> None:
    error_type = payload.get("error_type")
    if error_type is not None:
        print(f"ERROR type={error_type} summary={payload.get('error')!r}")
    next_steps = payload.get("next_steps")
    if isinstance(next_steps, list) and next_steps:
        print("NEXT")
        for index, step in enumerate(next_steps, start=1):
            print(f"  {index}. {step}")


def format_background_task_state(task: BackgroundTaskState) -> str:
    return format_named_record("TASK", _background_task_fields(task))


def _background_task_result_fields(result: BackgroundTaskResult) -> list[tuple[str, object]]:
    fields: list[tuple[str, object]] = [
        ("id", result.task_id),
        ("status", result.status),
        ("parent_session_id", result.parent_session_id),
        ("requested_child_session_id", result.requested_child_session_id),
        ("child_session_id", result.child_session_id),
        ("approval_request_id", result.approval_request_id),
        ("question_request_id", result.question_request_id),
        ("approval_blocked", result.approval_blocked),
        ("result_available", result.result_available),
    ]
    if result.summary_output is not None:
        fields.append(("summary_output", repr(result.summary_output)))
    if result.error is not None:
        fields.append(("error", result.error))
    cancellation_cause = result.cancellation_cause
    if cancellation_cause is not None:
        fields.append(("cancellation_cause", cancellation_cause))
    _append_observability_and_routing(fields, result.observability, result.routing)
    return fields


def format_background_task_result(result: BackgroundTaskResult) -> str:
    return format_named_record("TASK", _background_task_result_fields(result))


def format_background_task_summary(task: StoredBackgroundTaskSummary) -> str:
    fields: list[tuple[str, object]] = [
        ("id", task.task.id),
        ("status", task.status),
        ("child_session_id", task.session_id),
        ("created_at", task.created_at),
        ("updated_at", task.updated_at),
        ("prompt", repr(task.prompt)),
    ]
    error = task.error
    if error is not None:
        fields.append(("error", error))
    observability = task.observability
    if observability is not None:
        fields.append(("waiting_reason", observability.waiting_reason))
        if observability.queue_position is not None:
            fields.append(("queue_position", observability.queue_position))
        if observability.concurrency is not None:
            fields.append(("active_worker_slots", observability.concurrency.active_worker_slots))
            fields.append(("queued_total", observability.concurrency.queued_total))
    return format_named_record("TASK", fields)
