from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Protocol, cast

from . import __version__
from .acp.stdio import StdioAcpServer
from .agent.builtin import list_top_level_selectable_agent_manifests
from .cli_support import (
    EXIT_APPROVAL_DENIED,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_INVALID_COMMAND,
    EXIT_INVALID_RESOURCE,
    EXIT_PROVIDER_ERROR,
    EXIT_RUNTIME_ERROR,
    EXIT_SUCCESS,
    RuntimeStreamResult,
    format_event,
    print_json,
    serialize_command_definition,
    serialize_command_summary,
    serialize_event,
    serialize_session_state,
    serialize_stored_session_summary,
)
from .command.loader import load_command_registry
from .command.registry import CommandRegistry
from .doctor import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
    CapabilityDoctor,
    DoctorCheckType,
    create_doctor_for_config,
    create_report,
    format_report,
    format_report_json,
)
from .provider.snapshot import resolved_provider_snapshot
from .runtime.config import (
    RUNTIME_CONFIG_FILE_NAME,
    RuntimeConfig,
    load_runtime_config,
    serialize_provider_fallback_config,
    serialize_runtime_agent_config,
)
from .runtime.config_schema import (
    format_starter_runtime_config_json,
    generate_starter_runtime_config,
    runtime_config_json_schema,
    write_runtime_config_payload,
)
from .runtime.contracts import (
    BackgroundTaskResult,
    CapabilityStatusSnapshot,
    ProviderInspectResult,
    ProviderModelMetadata,
    ProviderReadinessResult,
    RuntimeRequest,
    RuntimeSessionDebugSnapshot,
    RuntimeStreamChunk,
    validate_runtime_request_metadata,
)
from .runtime.events import EventEnvelope
from .runtime.permission import PermissionDecision, PermissionResolution
from .runtime.service import VoidCodeRuntime
from .runtime.session import SessionState, StoredSessionSummary
from .runtime.task import BackgroundTaskState, StoredBackgroundTaskSummary
from .server import serve, web

Handler = Callable[[argparse.Namespace], int]


class TuiAppProtocol(Protocol):
    def run(self) -> None: ...


def _close_runtime(runtime: object) -> None:
    exit_method = getattr(runtime, "__exit__", None)
    if callable(exit_method):
        exit_method(None, None, None)


def _handle_run_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    request_text = cast(str, args.request)
    json_output = cast(bool, getattr(args, "json", False))
    config = load_runtime_config(
        workspace,
        approval_mode=cast(PermissionDecision | None, getattr(args, "approval_mode", None)),
        reasoning_effort=cast(str | None, getattr(args, "reasoning_effort", None)),
    )
    runtime = VoidCodeRuntime(workspace=workspace, config=config)
    try:
        metadata: dict[str, object] = {}
        if getattr(args, "agent", None) is not None:
            metadata["agent"] = {"preset": cast(str, args.agent)}
        if getattr(args, "skills", None):
            metadata["skills"] = cast(list[str], args.skills)
        if getattr(args, "max_steps", None) is not None:
            metadata["max_steps"] = cast(int, args.max_steps)
        if getattr(args, "reasoning_effort", None) is not None:
            metadata["reasoning_effort"] = cast(str, args.reasoning_effort)
        if getattr(args, "provider_stream", None) is not None:
            metadata["provider_stream"] = cast(bool, args.provider_stream)
        request = RuntimeRequest(
            prompt=request_text,
            session_id=cast(str | None, args.session_id),
            metadata=validate_runtime_request_metadata(metadata),
        )
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
        try:
            result = _run_with_inline_approval(
                runtime,
                request,
                interactive=interactive,
                emit_events=interactive and not json_output,
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None

        blocked_event = _pending_blocked_event(result.session, _last_event(result))
        if json_output:
            print_json(_runtime_stream_payload(result, workspace=workspace))
            if not interactive and blocked_event is not None:
                return _blocked_exit_code(blocked_event)
        elif not interactive:
            if blocked_event is not None:
                _print_noninteractive_blocked(result, blocked_event)
                return _blocked_exit_code(blocked_event)
            _print_plain_runtime_output(result.output)
    finally:
        _close_runtime(runtime)
    return EXIT_SUCCESS


def _handle_acp_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    config = load_runtime_config(
        workspace,
        approval_mode=cast(PermissionDecision | None, getattr(args, "approval_mode", None)),
    )
    runtime = VoidCodeRuntime(workspace=workspace, config=config)
    try:
        server = StdioAcpServer(runtime=runtime, workspace=workspace)
        return server.serve()
    finally:
        _close_runtime(runtime)


def _run_with_inline_approval(
    runtime: VoidCodeRuntime,
    request: RuntimeRequest,
    *,
    interactive: bool,
    emit_events: bool,
) -> RuntimeStreamResult:
    result = _consume_runtime_stream(runtime.run_stream(request), emit_events=emit_events)

    while interactive:
        approval_event = _pending_approval_event(result.session, _last_event(result))
        if approval_event is None:
            break
        resumed_result = _consume_runtime_stream(
            runtime.resume_stream(
                session_id=result.session.session.id,
                approval_request_id=_approval_request_id(approval_event),
                approval_decision=_prompt_for_approval(approval_event),
            ),
            emit_events=emit_events,
        )
        result = RuntimeStreamResult(
            output=resumed_result.output,
            session=resumed_result.session,
            events=(*result.events, *resumed_result.events),
        )

    if interactive and not emit_events:
        return result
    if interactive:
        _print_runtime_output(result.output)

    return result


def _consume_runtime_stream(
    chunks: Iterator[RuntimeStreamChunk],
    *,
    emit_events: bool,
) -> RuntimeStreamResult:
    output: str | None = None
    final_session: SessionState | None = None
    events: list[EventEnvelope] = []

    for chunk in chunks:
        final_session = chunk.session
        if chunk.event is not None:
            if emit_events:
                print(
                    format_event(chunk.event.event_type, chunk.event.source, chunk.event.payload),
                    flush=True,
                )
            events.append(chunk.event)
        if chunk.kind == "output":
            output = chunk.output

    if final_session is None:
        raise ValueError("runtime stream emitted no chunks")

    return RuntimeStreamResult(output=output, session=final_session, events=tuple(events))


def _last_event(result: RuntimeStreamResult) -> EventEnvelope | None:
    return result.events[-1] if result.events else None


def _pending_approval_event(
    session: SessionState,
    event: EventEnvelope | None,
) -> EventEnvelope | None:
    if session.status != "waiting":
        return None
    if event is None or event.event_type != "runtime.approval_requested":
        return None
    return event


def _pending_question_event(
    session: SessionState,
    event: EventEnvelope | None,
) -> EventEnvelope | None:
    if session.status != "waiting":
        return None
    if event is None or event.event_type != "runtime.question_requested":
        return None
    return event


def _pending_blocked_event(
    session: SessionState,
    event: EventEnvelope | None,
) -> EventEnvelope | None:
    return _pending_approval_event(session, event) or _pending_question_event(session, event)


def _blocked_exit_code(event: EventEnvelope) -> int:
    if event.event_type == "runtime.approval_requested":
        return EXIT_APPROVAL_DENIED
    return EXIT_RUNTIME_ERROR


def _approval_request_id(event: EventEnvelope) -> str:
    return str(event.payload["request_id"])


def _prompt_for_approval(event: EventEnvelope) -> PermissionResolution:
    tool = str(event.payload["tool"])
    target_summary = event.payload.get("target_summary")
    if isinstance(target_summary, str) and target_summary:
        prompt = f"Approve {tool} for {target_summary}? [y/N]: "
    else:
        prompt = f"Approve {tool}? [y/N]: "
    sys.stderr.write(prompt)
    sys.stderr.flush()
    response = sys.stdin.readline()
    normalized = response.strip().lower()
    if normalized in {"y", "yes"}:
        return "allow"
    return "deny"


def _print_runtime_response(
    result: object,
    *,
    event_offset: int = 0,
    include_result: bool = True,
) -> int:
    typed_result = cast("RuntimeResponseLike", result)

    for event in typed_result.events[event_offset:]:
        print(format_event(event.event_type, event.source, event.payload), flush=True)

    if include_result:
        _print_runtime_output(typed_result.output)
    return len(typed_result.events)


def _print_runtime_output(output: str | None) -> None:
    print("RESULT", flush=True)
    print(output or "", end="", flush=True)
    if output and not output.endswith("\n"):
        print(flush=True)


def _print_plain_runtime_output(output: str | None) -> None:
    if output is None:
        return
    print(output, end="", flush=True)
    if not output.endswith("\n"):
        print(flush=True)


def _runtime_stream_payload(result: RuntimeStreamResult, *, workspace: Path) -> dict[str, object]:
    blocked_event = _pending_blocked_event(result.session, _last_event(result))
    payload: dict[str, object] = {
        "workspace": str(workspace),
        "session": serialize_session_state(result.session),
        "output": result.output,
        "events": [serialize_event(event) for event in result.events],
    }
    if blocked_event is not None:
        payload["blocked"] = _blocked_payload(result, blocked_event)
    return payload


def _blocked_payload(result: RuntimeStreamResult, event: EventEnvelope) -> dict[str, object]:
    if event.event_type == "runtime.approval_requested":
        return {
            "kind": "approval_required",
            "session_id": result.session.session.id,
            "request_id": _approval_request_id(event),
            "tool": event.payload.get("tool"),
            "target_summary": event.payload.get("target_summary"),
        }
    return {
        "kind": "question_required",
        "session_id": result.session.session.id,
        "request_id": str(event.payload["request_id"]),
        "tool": event.payload.get("tool"),
        "question_count": event.payload.get("question_count"),
        "questions": event.payload.get("questions"),
    }


def _print_noninteractive_blocked(result: RuntimeStreamResult, event: EventEnvelope) -> None:
    if event.event_type == "runtime.question_requested":
        print(
            "error: question response required"
            f" for {event.payload.get('tool')}; resume session {result.session.session.id} "
            f"with question request {event.payload.get('request_id')}",
            file=sys.stderr,
            flush=True,
        )
        return
    tool = event.payload.get("tool")
    target_summary = event.payload.get("target_summary")
    target_suffix = f" for {target_summary}" if isinstance(target_summary, str) else ""
    print(
        "error: approval required"
        f" for {tool}{target_suffix}; resume session {result.session.session.id} "
        f"with approval request {_approval_request_id(event)}",
        file=sys.stderr,
        flush=True,
    )


def _handle_sessions_list_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    json_output = cast(bool, getattr(args, "json", False))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        sessions = runtime.list_sessions()
    finally:
        _close_runtime(runtime)

    if json_output:
        print_json(
            {
                "workspace": str(workspace),
                "sessions": [serialize_stored_session_summary(session) for session in sessions],
            }
        )
        return EXIT_SUCCESS

    for session in sessions:
        print(_format_session_summary(session))

    return EXIT_SUCCESS


def _format_session_summary(session: StoredSessionSummary) -> str:
    return (
        f"SESSION id={session.session.id} status={session.status} "
        f"turn={session.turn} updated_at={session.updated_at} prompt={session.prompt!r}"
    )


def _format_named_record(prefix: str, fields: Sequence[tuple[str, object]]) -> str:
    suffix = " ".join(f"{key}={value}" for key, value in fields)
    return f"{prefix} {suffix}" if suffix else prefix


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
    if task.cancellation_cause is not None:
        fields.append(("cancellation_cause", task.cancellation_cause))
    if task.error is not None:
        fields.append(("error", task.error))
    routing = task.routing_identity
    if routing is not None:
        fields.append(("delegation_mode", routing.mode))
        if routing.category is not None:
            fields.append(("category", routing.category))
        if routing.subagent_type is not None:
            fields.append(("subagent_type", routing.subagent_type))
        if routing.description is not None:
            fields.append(("description", routing.description))
        if routing.command is not None:
            fields.append(("command", routing.command))
    return fields


def _background_task_routing_payload(routing: object | None) -> dict[str, object] | None:
    if routing is None:
        return None
    return {
        key: value
        for key, value in {
            "mode": getattr(routing, "mode", None),
            "category": getattr(routing, "category", None),
            "subagent_type": getattr(routing, "subagent_type", None),
            "description": getattr(routing, "description", None),
            "command": getattr(routing, "command", None),
        }.items()
        if value is not None
    }


def _background_task_error_type(error: str | None) -> str | None:
    if error is None:
        return None
    normalized = error.lower()
    if any(token in normalized for token in ("provider", "model", "api key", "unreachable")):
        return "provider"
    if any(
        token in normalized
        for token in ("tool", "write_file", "read_file", "shell_exec", "permission")
    ):
        return "tool"
    return "runtime"


def _background_task_next_steps(
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
    workspace_arg = f"--workspace {shlex.quote(str(workspace))}"
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
            "Inspect waiting child session before answering questions: "
            f"voidcode sessions debug {child_session_id} {workspace_arg}"
        )
        steps.append(f"Cancel delegated task: voidcode tasks cancel {task_id} {workspace_arg}")
    elif status in {"queued", "running"}:
        steps.append(f"Refresh state: voidcode tasks status {task_id} {workspace_arg}")
        steps.append(f"Read partial result view: voidcode tasks output {task_id} {workspace_arg}")
        steps.append(f"Cancel delegated task: voidcode tasks cancel {task_id} {workspace_arg}")
    elif status == "completed":
        steps.append(f"Read output: voidcode tasks output {task_id} {workspace_arg}")
        if child_session_id is not None:
            steps.append(
                f"Replay child session: voidcode sessions resume {child_session_id} {workspace_arg}"
            )
    elif status == "failed":
        error_type = _background_task_error_type(error)
        if result_available:
            steps.append(f"Inspect failure output: voidcode tasks output {task_id} {workspace_arg}")
        if child_session_id is not None:
            steps.append(
                f"Resume child context: voidcode sessions resume {child_session_id} {workspace_arg}"
            )
        if error_type == "provider":
            steps.append("Check provider configuration: voidcode provider inspect <provider>")
        elif error_type == "tool":
            steps.append(
                "Inspect the child session events to find the failed tool call and approval state."
            )
        else:
            steps.append(
                "Inspect runtime events and retry explicitly from the parent flow if needed."
            )
    elif status == "cancelled":
        steps.append(f"Inspect final task state: voidcode tasks status {task_id} {workspace_arg}")
    return steps


def _background_task_state_payload(
    task: BackgroundTaskState, *, workspace: Path
) -> dict[str, object]:
    error = getattr(task, "error", None)
    cancellation_cause = getattr(task, "cancellation_cause", None)
    error_type = _background_task_error_type(error)
    next_steps = _background_task_next_steps(
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
        "cancellation_cause": cancellation_cause,
        "error": error,
        "error_type": error_type,
        "routing": _background_task_routing_payload(task.routing_identity),
        "next_steps": next_steps,
    }
    return payload


def _background_task_result_payload(
    result: BackgroundTaskResult, *, workspace: Path
) -> dict[str, object]:
    cancellation_cause = getattr(result, "cancellation_cause", None)
    error_type = _background_task_error_type(result.error)
    next_steps = _background_task_next_steps(
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
        "next_steps": next_steps,
    }


def _background_task_summary_payload(task: StoredBackgroundTaskSummary) -> dict[str, object]:
    error = getattr(task, "error", None)
    return {
        "task_id": task.task.id,
        "status": task.status,
        "child_session_id": task.session_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "prompt": task.prompt,
        "error": error,
        "error_type": _background_task_error_type(error),
    }


def _print_background_task_guidance(payload: dict[str, object]) -> None:
    error_type = payload.get("error_type")
    if error_type is not None:
        print(f"ERROR type={error_type} summary={payload.get('error')!r}")
    next_steps = payload.get("next_steps")
    if isinstance(next_steps, list) and next_steps:
        print("NEXT")
        for index, step in enumerate(cast(list[str], next_steps), start=1):
            print(f"  {index}. {step}")


def _format_background_task_state(task: BackgroundTaskState) -> str:
    return _format_named_record("TASK", _background_task_fields(task))


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
    cancellation_cause = getattr(result, "cancellation_cause", None)
    if cancellation_cause is not None:
        fields.append(("cancellation_cause", cancellation_cause))
    routing = result.routing
    if routing is not None:
        fields.append(("delegation_mode", routing.mode))
        if routing.category is not None:
            fields.append(("category", routing.category))
        if routing.subagent_type is not None:
            fields.append(("subagent_type", routing.subagent_type))
        if routing.description is not None:
            fields.append(("description", routing.description))
        if routing.command is not None:
            fields.append(("command", routing.command))
    return fields


def _format_background_task_result(result: BackgroundTaskResult) -> str:
    return _format_named_record("TASK", _background_task_result_fields(result))


def _format_background_task_summary(task: StoredBackgroundTaskSummary) -> str:
    fields: list[tuple[str, object]] = [
        ("id", task.task.id),
        ("status", task.status),
        ("child_session_id", task.session_id),
        ("created_at", task.created_at),
        ("updated_at", task.updated_at),
        ("prompt", repr(task.prompt)),
    ]
    error = getattr(task, "error", None)
    if error is not None:
        fields.append(("error", error))
    return _format_named_record("TASK", fields)


def _serialize_session_debug_snapshot(snapshot: RuntimeSessionDebugSnapshot) -> dict[str, object]:
    session_payload: dict[str, object] = {"id": snapshot.session.session.id}
    if snapshot.session.session.parent_id is not None:
        session_payload["parent_id"] = snapshot.session.session.parent_id
    return {
        "session": {
            "session": session_payload,
            "status": snapshot.session.status,
            "turn": snapshot.session.turn,
            "metadata": snapshot.session.metadata,
        },
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
        "last_event_sequence": snapshot.last_event_sequence,
        "last_relevant_event": (
            {
                "sequence": snapshot.last_relevant_event.sequence,
                "event_type": snapshot.last_relevant_event.event_type,
                "source": snapshot.last_relevant_event.source,
                "payload": snapshot.last_relevant_event.payload,
            }
            if snapshot.last_relevant_event is not None
            else None
        ),
        "last_failure_event": (
            {
                "sequence": snapshot.last_failure_event.sequence,
                "event_type": snapshot.last_failure_event.event_type,
                "source": snapshot.last_failure_event.source,
                "payload": snapshot.last_failure_event.payload,
            }
            if snapshot.last_failure_event is not None
            else None
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
                "sequence": snapshot.last_tool.sequence,
            }
            if snapshot.last_tool is not None
            else None
        ),
        "suggested_operator_action": snapshot.suggested_operator_action,
        "operator_guidance": snapshot.operator_guidance,
    }


def _handle_sessions_resume_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    approval_decision = cast(PermissionResolution | None, getattr(args, "approval_decision", None))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            result = runtime.resume(
                session_id,
                approval_request_id=cast(str | None, getattr(args, "approval_request_id", None)),
                approval_decision=approval_decision,
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    _print_runtime_response(result)
    return 0


def _handle_sessions_debug_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            snapshot = runtime.session_debug_snapshot(session_id=session_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    print(json.dumps(_serialize_session_debug_snapshot(snapshot), sort_keys=True))
    return 0


def _handle_tasks_status_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    task_id = cast(str, args.task_id)
    json_output = cast(bool, getattr(args, "json", False))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            task = runtime.load_background_task(task_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    payload = _background_task_state_payload(task, workspace=workspace)
    if json_output:
        print_json({"workspace": str(workspace), "task": payload})
        return 0
    print(_format_background_task_state(task))
    _print_background_task_guidance(payload)
    return 0


def _handle_tasks_output_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    task_id = cast(str, args.task_id)
    json_output = cast(bool, getattr(args, "json", False))
    runtime = VoidCodeRuntime(workspace=workspace)
    task_result: BackgroundTaskResult | None = None
    session_output: str | None = None
    try:
        try:
            task_result = runtime.load_background_task_result(task_id)
            if task_result.result_available and task_result.child_session_id is not None:
                try:
                    session_output = runtime.session_result(
                        session_id=task_result.child_session_id
                    ).output
                except ValueError:
                    session_output = None
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    assert task_result is not None
    fallback_output = (
        task_result.summary_output if task_result.summary_output is not None else task_result.error
    )
    output = session_output if session_output is not None else fallback_output
    payload = _background_task_result_payload(task_result, workspace=workspace)
    if json_output:
        print_json({"workspace": str(workspace), "task": payload, "output": output})
        return 0
    print(_format_background_task_result(task_result))
    _print_background_task_guidance(payload)
    _print_runtime_output(output)
    return 0


def _handle_tasks_cancel_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    task_id = cast(str, args.task_id)
    json_output = cast(bool, getattr(args, "json", False))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            task = runtime.cancel_background_task(task_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    payload = _background_task_state_payload(task, workspace=workspace)
    if json_output:
        print_json({"workspace": str(workspace), "task": payload})
        return 0
    print(_format_background_task_state(task))
    _print_background_task_guidance(payload)
    return 0


def _handle_tasks_list_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    parent_session_id = cast(str | None, getattr(args, "parent_session_id", None))
    json_output = cast(bool, getattr(args, "json", False))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            tasks = (
                runtime.list_background_tasks_by_parent_session(parent_session_id=parent_session_id)
                if parent_session_id is not None
                else runtime.list_background_tasks()
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    if json_output:
        print_json(
            {
                "workspace": str(workspace),
                "parent_session_id": parent_session_id,
                "tasks": [_background_task_summary_payload(task) for task in tasks],
            }
        )
        return 0

    for task in tasks:
        print(_format_background_task_summary(task))
    return 0


def _handle_storage_diagnostics_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        diagnostics = runtime.storage_diagnostics()
    finally:
        _close_runtime(runtime)
    print_json({"workspace": str(workspace), "storage": diagnostics})
    return EXIT_SUCCESS


def _handle_storage_prune_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            counts = runtime.prune_runtime_storage(
                keep_sessions=cast(int | None, args.keep_sessions),
                keep_background_tasks=cast(int | None, args.keep_background_tasks),
                older_than=cast(int | None, args.older_than),
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)
    print_json({"workspace": str(workspace), "pruned": counts})
    return EXIT_SUCCESS


def _handle_storage_reset_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        result = runtime.reset_runtime_storage()
    finally:
        _close_runtime(runtime)
    print_json({"workspace": str(workspace), "storage": result})
    return EXIT_SUCCESS


def _handle_server_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    config = load_runtime_config(
        workspace,
        approval_mode=cast(PermissionDecision | None, getattr(args, "approval_mode", None)),
    )
    server_entry = cast(Callable[..., None], args.server_entry)
    if hasattr(args, "open_browser"):
        server_entry(
            workspace=workspace,
            host=cast(str, args.host),
            port=cast(int, args.port),
            config=config,
            open_browser=cast(bool, args.open_browser),
        )
    else:
        server_entry(
            workspace=workspace,
            host=cast(str, args.host),
            port=cast(int, args.port),
            config=config,
        )

    return 0


def _handle_config_show_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    if not workspace.exists() or not workspace.is_dir():
        raise SystemExit(f"error: workspace does not exist: {workspace}")

    session_id = cast(str | None, getattr(args, "session_id", None))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            effective_config = runtime.effective_runtime_config(session_id=session_id)
            readiness = runtime.provider_readiness(session_id=session_id)
            categories = runtime.effective_category_model_config(session_id=session_id)
            agents = runtime.effective_agent_model_config(session_id=session_id)
            status = runtime.current_status()
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    print_json(
        {
            "workspace": str(workspace),
            "session_id": session_id,
            "approval_mode": effective_config.approval_mode,
            "model": effective_config.model,
            "execution_engine": effective_config.execution_engine,
            "max_steps": effective_config.max_steps,
            "reasoning_effort": getattr(effective_config, "reasoning_effort", None),
            "agent": serialize_runtime_agent_config(getattr(effective_config, "agent", None)),
            "agents": agents,
            "categories": categories,
            "provider_fallback": serialize_provider_fallback_config(
                getattr(effective_config, "provider_fallback", None)
            ),
            "resolved_provider": resolved_provider_snapshot(
                getattr(effective_config, "resolved_provider", None)
            ),
            "provider_readiness": _provider_readiness_payload(readiness),
            "context_budget": {
                "context_window": readiness.context_window,
                "max_output_tokens": readiness.max_output_tokens,
            },
            "mcp": _mcp_status_payload(status.mcp),
        }
    )
    return EXIT_SUCCESS


def _mcp_status_payload(snapshot: CapabilityStatusSnapshot) -> dict[str, object]:
    state = snapshot.state
    error = snapshot.error
    details = snapshot.details
    return {
        "state": state,
        "error": error,
        "details": details,
    }


def _handle_mcp_list_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    if not workspace.exists() or not workspace.is_dir():
        raise SystemExit(f"error: workspace does not exist: {workspace}")

    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        status = runtime.current_status()
    finally:
        _close_runtime(runtime)

    payload = {
        "workspace": str(workspace),
        "mcp": _mcp_status_payload(status.mcp),
    }
    if cast(bool, args.json):
        print_json(payload)
        return EXIT_SUCCESS

    details = status.mcp.details
    print(
        _format_named_record(
            "MCP",
            [
                ("state", status.mcp.state),
                ("mode", details.get("mode", "disabled")),
                ("configured", details.get("configured", False)),
                ("configured_enabled", details.get("configured_enabled", False)),
                ("configured_server_count", details.get("configured_server_count", 0)),
                ("running_server_count", details.get("running_server_count", 0)),
                ("failed_server_count", details.get("failed_server_count", 0)),
            ],
        )
    )
    servers = cast(list[object], details.get("servers", []))
    for item in servers:
        server = cast(dict[str, object], item)
        print(
            _format_named_record(
                "MCP_SERVER",
                [
                    ("name", server.get("server")),
                    ("status", server.get("status")),
                    ("scope", server.get("scope")),
                    ("transport", server.get("transport")),
                    ("command", repr(server.get("command", []))),
                    ("stage", server.get("stage")),
                    ("error", repr(server.get("error"))),
                ],
            )
        )
    return EXIT_SUCCESS


def _handle_commands_list_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    registry = _load_cli_command_registry(args, workspace=workspace)
    commands = registry.list(
        include_hidden=cast(bool, args.include_hidden),
        include_disabled=cast(bool, args.include_disabled),
    )

    if cast(bool, args.json):
        print_json(
            {
                "workspace": str(workspace),
                "commands": [serialize_command_summary(command) for command in commands],
            }
        )
        return EXIT_SUCCESS

    for command in commands:
        print(
            _format_named_record(
                "COMMAND",
                [
                    ("name", f"/{command.name}"),
                    ("source", command.source),
                    ("enabled", command.enabled),
                    ("description", repr(command.description)),
                ],
            )
        )
    return EXIT_SUCCESS


def _handle_commands_show_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    registry = _load_cli_command_registry(args, workspace=workspace)
    command_name = cast(str, args.name)
    command = registry.get(command_name)
    if command is None:
        raise SystemExit(f"error: unknown command: /{command_name.removeprefix('/')}")
    if command.hidden and not cast(bool, args.include_hidden):
        raise SystemExit(f"error: unknown command: /{command.name}")
    if not command.enabled and not cast(bool, args.include_disabled):
        raise SystemExit(f"error: command is disabled: /{command.name}")

    payload = serialize_command_definition(command)
    if cast(bool, args.json):
        print_json(payload)
        return EXIT_SUCCESS

    print(f"/{command.name}")
    print(f"Source: {command.source}")
    print(f"Enabled: {command.enabled}")
    print(f"Description: {command.description}")
    if command.path is not None:
        print(f"Path: {command.path}")
    print("Template:")
    print(command.template, end="" if command.template.endswith("\n") else "\n")
    return EXIT_SUCCESS


def _load_cli_command_registry(args: argparse.Namespace, *, workspace: Path) -> CommandRegistry:
    user_commands_dir = cast(Path | None, getattr(args, "user_commands_dir", None))
    try:
        return load_command_registry(workspace=workspace, user_commands_dir=user_commands_dir)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None


def _handle_config_schema_command(args: argparse.Namespace) -> int:
    _ = args
    print(json.dumps(runtime_config_json_schema(), indent=2, sort_keys=True))
    return 0


def _handle_config_init_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    if not workspace.exists() or not workspace.is_dir():
        raise SystemExit(f"error: workspace does not exist: {workspace}")

    try:
        payload = generate_starter_runtime_config(
            approval_mode=cast(str, args.approval_mode),
            model=cast(str | None, getattr(args, "model", None)),
            execution_engine=cast(str | None, getattr(args, "execution_engine", None)),
            max_steps=cast(int | None, getattr(args, "max_steps", None)),
            reasoning_effort=cast(str | None, getattr(args, "reasoning_effort", None)),
            include_examples=cast(bool, args.with_examples),
        )
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None
    if cast(bool, args.print):
        print(format_starter_runtime_config_json(payload), end="")
        return 0

    config_path = workspace.resolve() / RUNTIME_CONFIG_FILE_NAME
    if config_path.exists() and not cast(bool, args.force):
        raise SystemExit(
            f"error: runtime config already exists: {config_path}; pass --force to overwrite"
        )
    written_path = write_runtime_config_payload(workspace, payload)
    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "config_path": str(written_path),
                "next_command": f"voidcode doctor --workspace {workspace}",
                "first_task_command": f'voidcode run "read README.md" --workspace {workspace}',
            }
        )
    )
    return 0


def _handle_provider_models_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    provider = cast(str, args.provider)
    refresh = cast(bool, args.refresh)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            if refresh:
                _ = runtime.refresh_provider_models(provider)
            result = runtime.provider_models_result(provider)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    payload: dict[str, object] = {
        "workspace": str(workspace),
        "provider": provider,
        "refreshed": refresh,
        "models": list(result.models),
        "model_metadata": {
            model: _provider_model_metadata_payload(metadata)
            for model, metadata in result.model_metadata.items()
        },
        "source": result.source,
        "last_refresh_status": result.last_refresh_status,
        "last_error": result.last_error,
        "discovery_mode": result.discovery_mode,
    }
    if refresh and result.source == "fallback":
        print(
            "WARN provider.models.refresh "
            f"provider={provider} source=fallback reason={result.last_error}",
            file=sys.stderr,
            flush=True,
        )

    print(json.dumps(payload))
    return 0


def _provider_model_metadata_payload(
    metadata: ProviderModelMetadata,
) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "context_window": metadata.context_window,
            "max_input_tokens": metadata.max_input_tokens,
            "max_output_tokens": metadata.max_output_tokens,
            "supports_tools": metadata.supports_tools,
            "supports_vision": metadata.supports_vision,
            "supports_streaming": metadata.supports_streaming,
            "supports_reasoning": metadata.supports_reasoning,
            "supports_json_mode": metadata.supports_json_mode,
            "cost_per_input_token": metadata.cost_per_input_token,
            "cost_per_output_token": metadata.cost_per_output_token,
            "cost_per_cache_read_token": metadata.cost_per_cache_read_token,
            "cost_per_cache_write_token": metadata.cost_per_cache_write_token,
            "supports_reasoning_effort": metadata.supports_reasoning_effort,
            "default_reasoning_effort": metadata.default_reasoning_effort,
            "supports_interleaved_reasoning": metadata.supports_interleaved_reasoning,
            "modalities_input": list(metadata.modalities_input)
            if metadata.modalities_input is not None
            else None,
            "modalities_output": list(metadata.modalities_output)
            if metadata.modalities_output is not None
            else None,
            "model_status": metadata.model_status,
        }.items()
        if value is not None
    }


def _provider_readiness_payload(readiness: ProviderReadinessResult) -> dict[str, object]:
    return {
        "provider": readiness.provider,
        "model": readiness.model,
        "configured": readiness.configured,
        "ok": readiness.ok,
        "status": readiness.status,
        "guidance": readiness.guidance,
        "auth_present": readiness.auth_present,
        "streaming_configured": readiness.streaming_configured,
        "streaming_supported": readiness.streaming_supported,
        "context_window": readiness.context_window,
        "max_output_tokens": readiness.max_output_tokens,
        "fallback_chain": list(readiness.fallback_chain),
    }


def _provider_inspect_payload(
    result: ProviderInspectResult, *, workspace: Path
) -> dict[str, object]:
    return {
        "workspace": str(workspace),
        "provider": {
            "name": result.summary.name,
            "label": result.summary.label,
            "configured": result.summary.configured,
            "current": result.summary.current,
        },
        "models": {
            "provider": result.models.provider,
            "configured": result.models.configured,
            "models": list(result.models.models),
            "model_metadata": {
                model: _provider_model_metadata_payload(metadata)
                for model, metadata in result.models.model_metadata.items()
            },
            "source": result.models.source,
            "last_refresh_status": result.models.last_refresh_status,
            "last_error": result.models.last_error,
            "discovery_mode": result.models.discovery_mode,
        },
        "validation": {
            "provider": result.validation.provider,
            "configured": result.validation.configured,
            "ok": result.validation.ok,
            "status": result.validation.status,
            "message": result.validation.message,
            "source": result.validation.source,
            "last_error": result.validation.last_error,
            "discovery_mode": result.validation.discovery_mode,
            "failure_kind": result.validation.failure_kind,
            "guidance": result.validation.guidance,
        },
        "readiness": (
            _provider_readiness_payload(result.readiness) if result.readiness is not None else None
        ),
        "current_model": result.current_model,
        "current_model_metadata": (
            None
            if result.current_model_metadata is None
            else _provider_model_metadata_payload(result.current_model_metadata)
        ),
    }


def _handle_provider_inspect_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    provider = cast(str, args.provider)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            result = runtime.inspect_provider(provider)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    print(json.dumps(_provider_inspect_payload(result, workspace=workspace), sort_keys=True))
    return 0


class EventLikeProtocol(Protocol):
    event_type: str
    source: str
    payload: dict[str, object]


class RuntimeResponseLike(Protocol):
    events: tuple[EventLikeProtocol, ...]
    output: str | None

    session: SessionState


def _handle_tui_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    approval_mode = cast(PermissionDecision | None, getattr(args, "approval_mode", None))

    from .tui import VoidCodeTUI

    app = cast(TuiAppProtocol, VoidCodeTUI(workspace=workspace, approval_mode=approval_mode))
    app.run()
    return 0


def _handle_doctor_command(args: argparse.Namespace) -> int:
    """Run the capability doctor to check external tool readiness."""
    workspace = cast(Path, args.workspace)
    verbose = cast(bool, args.verbose)
    json_output = cast(bool, args.json)

    # Load runtime config to get all capability settings
    config_error: str | None = None
    config: RuntimeConfig | None = None
    results: list[CapabilityCheckResult] = []
    try:
        config = load_runtime_config(workspace)
    except ValueError as exc:
        # Config file has a parse/validation error - report it but continue
        # with minimal checks so the user can still see what's wrong.
        config_error = str(exc)
        doctor = CapabilityDoctor(workspace=workspace)
        doctor.add_executable_check("ast-grep", "ast-grep")
        results = doctor.results
        results.append(
            CapabilityCheckResult(
                status=CapabilityCheckStatus.ERROR,
                name="runtime.config",
                check_type=DoctorCheckType.RUNTIME_CONFIG.value,
                error_message=config_error,
            )
        )
    except Exception:
        # OSError (permissions, path not found) and other unexpected errors
        # should propagate so they are not silently swallowed.
        raise

    if config_error is not None:
        print(f"WARN runtime config error: {config_error}", file=sys.stderr, flush=True)

    if config is not None:
        # Create doctor with full config
        doctor = create_doctor_for_config(workspace, config)
        results = doctor.run_all_checks()

    # Create and format report
    report = create_report(results, workspace=workspace)

    if json_output:
        print(format_report_json(report))
    else:
        print(format_report(report, verbose=verbose))

    # Return 0 only when healthy and runtime config parsed successfully.
    return EXIT_SUCCESS if (report.is_healthy and config_error is None) else EXIT_RUNTIME_ERROR


def _classify_cli_error(message: str) -> int:
    normalized = message.lower()
    if "unknown command" in normalized or "command is disabled" in normalized:
        return EXIT_INVALID_COMMAND
    if (
        "unknown session" in normalized
        or "unknown task" in normalized
        or "workspace does not exist" in normalized
    ):
        return EXIT_INVALID_RESOURCE
    if "provider" in normalized and (
        "requires a configured model" in normalized
        or "not configured" in normalized
        or "unreachable" in normalized
    ):
        return EXIT_PROVIDER_ERROR
    if (
        normalized.startswith("error: runtime config")
        or ".voidcode.json" in normalized
        or "voidcode/config.json" in normalized
    ):
        return EXIT_CONFIG_ERROR
    return EXIT_RUNTIME_ERROR


def _handle_cli_system_exit(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return EXIT_SUCCESS
    if isinstance(code, int):
        return code
    message = str(code)
    print(message, file=sys.stderr)
    if message.startswith("error:"):
        return _classify_cli_error(message)
    return EXIT_GENERAL_ERROR


def _add_command_discovery_arguments(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to discover project-local commands.",
    )
    _ = parser.add_argument(
        "--user-commands-dir",
        type=Path,
        help="Optional user command directory to merge before project commands.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voidcode",
        description="Voidcode command-line interface.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  voidcode run 'read README.md' --workspace .\n"
            "  voidcode run 'read README.md' --json --workspace .\n"
            "  voidcode sessions list --json --workspace .\n"
            "  voidcode commands list --workspace .\n"
            "  voidcode commands show /review --json --workspace ."
        ),
    )
    _ = parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    tui_parser = subparsers.add_parser(
        "tui",
        help="Run the VoidCode interactive Textual UI.",
    )
    _ = tui_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve relative read paths.",
    )
    _ = tui_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        help="Override the runtime approval mode for this invocation.",
    )
    tui_parser.set_defaults(handler=_handle_tui_command)

    run_parser = subparsers.add_parser(
        "run",
        help=(
            "Run through the local runtime; provider-backed execution is the product path, "
            "and deterministic is an explicit test/dev harness."
        ),
    )
    _ = run_parser.add_argument(
        "request",
        help="Runtime request such as 'read README.md'.",
    )
    _ = run_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve relative read paths.",
    )
    _ = run_parser.add_argument(
        "--session-id",
        help="Optional session identifier used for persisted runs.",
    )
    _ = run_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        help="Override the runtime approval mode for this invocation.",
    )
    _selectable_agent_ids = tuple(
        manifest.id for manifest in list_top_level_selectable_agent_manifests()
    )
    _ = run_parser.add_argument(
        "--agent",
        choices=_selectable_agent_ids,
        help="Select a top-level agent preset for this run.",
    )
    _ = run_parser.add_argument(
        "--skills",
        nargs="+",
        help="Optional skill names applied for this run.",
    )
    _ = run_parser.add_argument(
        "--max-steps",
        type=int,
        help="Optional max graph steps override for this run.",
    )
    _ = run_parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh"),
        help="Optional reasoning effort hint for this run (provider-dependent).",
    )
    _ = run_parser.add_argument(
        "--json",
        action="store_true",
        help="Output a structured JSON payload with session, events, and final output.",
    )
    stream_group = run_parser.add_mutually_exclusive_group()
    _ = stream_group.add_argument(
        "--provider-stream",
        dest="provider_stream",
        action="store_true",
        help="Enable provider-level streaming for this run.",
    )
    _ = stream_group.add_argument(
        "--no-provider-stream",
        dest="provider_stream",
        action="store_false",
        help="Disable provider-level streaming for this run.",
    )
    run_parser.set_defaults(provider_stream=None)
    run_parser.set_defaults(handler=_handle_run_command)

    acp_parser = subparsers.add_parser(
        "acp",
        help="Run the minimal external-facing ACP stdio JSON-RPC facade.",
    )
    _ = acp_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used by the ACP-backed runtime session database.",
    )
    _ = acp_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        help="Override the runtime approval mode for this ACP process.",
    )
    acp_parser.set_defaults(handler=_handle_acp_command)

    serve_parser = subparsers.add_parser(
        "serve",
        help="Serve the local HTTP runtime transport.",
    )
    _ = serve_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used by the local runtime and session database.",
    )
    _ = serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface for the local transport server.",
    )
    _ = serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the local transport server.",
    )
    _ = serve_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        help="Override the runtime approval mode for this server process.",
    )
    serve_parser.set_defaults(handler=_handle_server_command, server_entry=serve)

    web_parser = subparsers.add_parser(
        "web",
        help="Start the local web launcher entrypoint for the runtime transport.",
    )
    _ = web_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used by the local runtime and session database.",
    )
    _ = web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface for the local launcher server.",
    )
    _ = web_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the local launcher server.",
    )
    _ = web_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        help="Override the runtime approval mode for this launcher process.",
    )
    _ = web_parser.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        help="Start the web launcher without opening a browser window.",
    )
    web_parser.set_defaults(open_browser=True)
    web_parser.set_defaults(handler=_handle_server_command, server_entry=web)

    sessions_parser = subparsers.add_parser("sessions", help="Inspect persisted local sessions.")
    sessions_subparsers = sessions_parser.add_subparsers(dest="sessions_command")

    tasks_parser = subparsers.add_parser("tasks", help="Inspect delegated background tasks.")
    tasks_subparsers = tasks_parser.add_subparsers(dest="tasks_command")

    storage_parser = subparsers.add_parser(
        "storage",
        help="Inspect and maintain the local runtime SQLite store.",
    )
    storage_subparsers = storage_parser.add_subparsers(dest="storage_command")

    config_parser = subparsers.add_parser("config", help="Inspect effective runtime configuration.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")

    provider_parser = subparsers.add_parser("provider", help="Inspect provider metadata.")
    provider_subparsers = provider_parser.add_subparsers(dest="provider_command")

    commands_parser = subparsers.add_parser(
        "commands", help="Discover prompt commands available to runtime requests."
    )
    commands_subparsers = commands_parser.add_subparsers(dest="commands_command")

    mcp_parser = subparsers.add_parser(
        "mcp", help="Inspect runtime-managed MCP configuration and health."
    )
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command")

    config_show_parser = config_subparsers.add_parser(
        "show", help="Show effective runtime config for a workspace or session."
    )
    _ = config_show_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve runtime config and sessions.",
    )
    _ = config_show_parser.add_argument(
        "--session",
        dest="session_id",
        help="Optional persisted session identifier used to show resumed effective config.",
    )
    _ = config_show_parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Output JSON effective config (default).",
    )
    config_show_parser.set_defaults(handler=_handle_config_show_command)

    commands_list_parser = commands_subparsers.add_parser(
        "list", help="List enabled prompt commands discovered for a workspace."
    )
    _add_command_discovery_arguments(commands_list_parser)
    _ = commands_list_parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include commands marked hidden in discovery output.",
    )
    _ = commands_list_parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Include disabled commands in discovery output.",
    )
    _ = commands_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output discovered commands as JSON.",
    )
    commands_list_parser.set_defaults(handler=_handle_commands_list_command)

    commands_show_parser = commands_subparsers.add_parser(
        "show", help="Show one prompt command definition and rendered template source."
    )
    _ = commands_show_parser.add_argument(
        "name", help="Command name, with or without leading slash."
    )
    _add_command_discovery_arguments(commands_show_parser)
    _ = commands_show_parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Allow showing hidden commands explicitly by name.",
    )
    _ = commands_show_parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Allow showing disabled commands explicitly by name.",
    )
    _ = commands_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Output the command definition as JSON.",
    )
    commands_show_parser.set_defaults(handler=_handle_commands_show_command)

    mcp_list_parser = mcp_subparsers.add_parser(
        "list", help="List configured MCP servers and passive runtime status."
    )
    _ = mcp_list_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve runtime config and MCP state.",
    )
    _ = mcp_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output MCP status as JSON.",
    )
    mcp_list_parser.set_defaults(handler=_handle_mcp_list_command)

    config_schema_parser = config_subparsers.add_parser(
        "schema", help="Print the JSON Schema for .voidcode.json."
    )
    config_schema_parser.set_defaults(handler=_handle_config_schema_command)

    config_init_parser = config_subparsers.add_parser(
        "init", help="Generate a starter workspace .voidcode.json."
    )
    _ = config_init_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root where .voidcode.json should be generated.",
    )
    _ = config_init_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        default="ask",
        help="Starter approval mode to write.",
    )
    _ = config_init_parser.add_argument(
        "--model",
        help="Optional provider/model reference to include in the generated config.",
    )
    _ = config_init_parser.add_argument(
        "--execution-engine",
        choices=("deterministic", "provider"),
        help="Optional execution engine to include in the generated config.",
    )
    _ = config_init_parser.add_argument(
        "--max-steps",
        type=int,
        help="Optional max step budget to include in the generated config.",
    )
    _ = config_init_parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh"),
        help="Optional reasoning effort hint to include in the generated config.",
    )
    _ = config_init_parser.add_argument(
        "--with-examples",
        action="store_true",
        help="Include minimal tools and skills example blocks.",
    )
    _ = config_init_parser.add_argument(
        "--print",
        action="store_true",
        help="Print the generated config instead of writing it.",
    )
    _ = config_init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .voidcode.json.",
    )
    config_init_parser.set_defaults(handler=_handle_config_init_command)

    provider_models_parser = provider_subparsers.add_parser(
        "models", help="Show or refresh available models for one provider."
    )
    _ = provider_models_parser.add_argument(
        "provider", help="Provider name, e.g. openai or litellm."
    )
    _ = provider_models_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve runtime config.",
    )
    _ = provider_models_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh model list from provider endpoint before printing.",
    )
    provider_models_parser.set_defaults(handler=_handle_provider_models_command)

    provider_inspect_parser = provider_subparsers.add_parser(
        "inspect", help="Show configured status, model limits, and model capabilities."
    )
    _ = provider_inspect_parser.add_argument(
        "provider", help="Provider name, e.g. openai or litellm."
    )
    _ = provider_inspect_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve runtime config.",
    )
    provider_inspect_parser.set_defaults(handler=_handle_provider_inspect_command)

    list_parser = sessions_subparsers.add_parser("list", help="List persisted sessions.")
    _ = list_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output persisted sessions as JSON.",
    )
    list_parser.set_defaults(handler=_handle_sessions_list_command)

    resume_parser = sessions_subparsers.add_parser(
        "resume", help="Replay a persisted session response."
    )
    _ = resume_parser.add_argument("session_id", help="Persisted session identifier to load.")
    _ = resume_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = resume_parser.add_argument(
        "--approval-request-id",
        help="Optional pending approval request identifier to resolve during resume.",
    )
    _ = resume_parser.add_argument(
        "--approval-decision",
        choices=("allow", "deny"),
        help="Optional approval decision applied to the pending request during resume.",
    )
    resume_parser.set_defaults(handler=_handle_sessions_resume_command)

    debug_parser = sessions_subparsers.add_parser(
        "debug", help="Show a minimal runtime-owned debug snapshot for one session."
    )
    _ = debug_parser.add_argument("session_id", help="Persisted session identifier to inspect.")
    _ = debug_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = debug_parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Output JSON debug snapshot (default).",
    )
    debug_parser.set_defaults(handler=_handle_sessions_debug_command)

    tasks_status_parser = tasks_subparsers.add_parser(
        "status", help="Show delegated task lifecycle state."
    )
    _ = tasks_status_parser.add_argument("task_id", help="Delegated background task identifier.")
    _ = tasks_status_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = tasks_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Output delegated task state as JSON.",
    )
    tasks_status_parser.set_defaults(handler=_handle_tasks_status_command)

    tasks_output_parser = tasks_subparsers.add_parser(
        "output", help="Show delegated task output and correlation details."
    )
    _ = tasks_output_parser.add_argument("task_id", help="Delegated background task identifier.")
    _ = tasks_output_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = tasks_output_parser.add_argument(
        "--json",
        action="store_true",
        help="Output delegated task result and guidance as JSON.",
    )
    tasks_output_parser.set_defaults(handler=_handle_tasks_output_command)

    tasks_cancel_parser = tasks_subparsers.add_parser(
        "cancel", help="Cancel delegated background work."
    )
    _ = tasks_cancel_parser.add_argument("task_id", help="Delegated background task identifier.")
    _ = tasks_cancel_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = tasks_cancel_parser.add_argument(
        "--json",
        action="store_true",
        help="Output cancelled delegated task state as JSON.",
    )
    tasks_cancel_parser.set_defaults(handler=_handle_tasks_cancel_command)

    tasks_list_parser = tasks_subparsers.add_parser("list", help="List delegated background tasks.")
    _ = tasks_list_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = tasks_list_parser.add_argument(
        "--parent-session",
        dest="parent_session_id",
        help="Optional parent session identifier used to filter delegated tasks.",
    )
    _ = tasks_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output delegated task summaries as JSON.",
    )
    tasks_list_parser.set_defaults(handler=_handle_tasks_list_command)

    storage_diagnostics_parser = storage_subparsers.add_parser(
        "diagnostics",
        help="Show SQLite runtime storage policy, checkpoint, size, and row counts.",
    )
    _ = storage_diagnostics_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = storage_diagnostics_parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Output storage diagnostics as JSON (default).",
    )
    storage_diagnostics_parser.set_defaults(handler=_handle_storage_diagnostics_command)

    storage_prune_parser = storage_subparsers.add_parser(
        "prune",
        help="Prune terminal sessions and terminal background tasks from local storage.",
    )
    _ = storage_prune_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = storage_prune_parser.add_argument(
        "--keep-sessions",
        type=int,
        help="Keep the newest N sessions and prune older terminal sessions.",
    )
    _ = storage_prune_parser.add_argument(
        "--keep-background-tasks",
        type=int,
        help="Keep the newest N background tasks and prune older terminal tasks.",
    )
    _ = storage_prune_parser.add_argument(
        "--older-than",
        type=int,
        help="Only prune records with updated_at lower than this runtime timestamp.",
    )
    storage_prune_parser.set_defaults(handler=_handle_storage_prune_command)

    storage_reset_parser = storage_subparsers.add_parser(
        "reset",
        help="Delete the local pre-MVP runtime SQLite database and WAL/SHM files.",
    )
    _ = storage_reset_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    storage_reset_parser.set_defaults(handler=_handle_storage_reset_command)

    # Capability doctor command
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check runtime capability readiness (external tools, formatters, LSP, MCP).",
    )
    _ = doctor_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve runtime config.",
    )
    _ = doctor_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show all capabilities including successful ones.",
    )
    _ = doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Output report in JSON format.",
    )
    doctor_parser.set_defaults(handler=_handle_doctor_command)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (
        getattr(args, "command", None) == "sessions"
        and getattr(args, "sessions_command", None) == "resume"
    ):
        has_request_id = getattr(args, "approval_request_id", None) is not None
        has_decision = getattr(args, "approval_decision", None) is not None
        if has_request_id != has_decision:
            parser.error("--approval-request-id and --approval-decision must be provided together")
    handler = cast(Handler | None, getattr(args, "handler", None))
    if handler is None:
        parser.print_help()
        return EXIT_SUCCESS
    try:
        return handler(args)
    except SystemExit as exc:
        return _handle_cli_system_exit(exc)
    except RuntimeError as exc:
        message = f"error: {exc}"
        print(message, file=sys.stderr)
        return _classify_cli_error(message)
