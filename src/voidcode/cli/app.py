from __future__ import annotations

import argparse
import builtins as _builtins
import json
import shlex
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Protocol, cast

import click

from .. import __version__
from ..acp.stdio import StdioAcpServer
from ..agent.builtin import list_top_level_selectable_agent_manifests
from ..cli_support import (
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
from ..command.loader import load_command_registry
from ..command.registry import CommandRegistry
from ..doctor import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
    CapabilityDoctor,
    DoctorCheckType,
    create_doctor_for_config,
    create_report,
    format_report,
    format_report_json,
)
from ..provider.snapshot import resolved_provider_snapshot
from ..runtime.bundle import (
    SessionBundleError,
    SessionBundleFormat,
    SessionBundleOptions,
    write_session_bundle,
)
from ..runtime.config import (
    RUNTIME_CONFIG_FILE_NAME,
    RuntimeConfig,
    load_runtime_config,
    serialize_provider_fallback_config,
    serialize_runtime_agent_config,
)
from ..runtime.config_schema import (
    format_starter_runtime_config_json,
    generate_starter_runtime_config,
    runtime_config_json_schema,
    write_runtime_config_payload,
)
from ..runtime.contracts import (
    BackgroundTaskResult,
    CapabilityStatusSnapshot,
    NoPendingQuestionError,
    ProviderInspectResult,
    ProviderModelMetadata,
    ProviderReadinessResult,
    RuntimeHookPresetSnapshot,
    RuntimeProviderContextSnapshot,
    RuntimeRequest,
    RuntimeSessionDebugSnapshot,
    RuntimeSessionRevertMarker,
    RuntimeStreamChunk,
    validate_runtime_request_metadata,
)
from ..runtime.events import EventEnvelope, redact_reasoning_payload
from ..runtime.permission import PermissionDecision, PermissionResolution
from ..runtime.question import QuestionResponse
from ..runtime.service import VoidCodeRuntime
from ..runtime.session import SessionState, StoredSessionSummary
from ..runtime.task import BackgroundTaskState, StoredBackgroundTaskSummary
from ..server import serve, web

print = _builtins.print

Handler = Callable[[argparse.Namespace], int]


class _CliUsageError(Exception):
    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _namespace(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


def _click_path(value: str | Path) -> Path:
    return value if isinstance(value, Path) else Path(value)


def _click_optional_path(value: str | Path | None) -> Path | None:
    if value is None or isinstance(value, Path):
        return value
    return Path(value)


def _json_option_enabled(value: bool | None) -> bool:
    return bool(value)


def _build_click_command_context(
    *,
    command: str | None = None,
    subcommand: str | None = None,
    **values: object,
) -> argparse.Namespace:
    payload = dict(values)
    if command is not None:
        payload["command"] = command
    if subcommand is not None and command is not None:
        payload[f"{command}_command"] = subcommand
    return _namespace(**payload)


def _invoke_handler_from_click(handler: Handler, args: argparse.Namespace) -> int:
    try:
        return handler(args)
    except _CliUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except SystemExit as exc:
        return _handle_cli_system_exit(exc)
    except RuntimeError as exc:
        message = f"error: {exc}"
        print(message, file=sys.stderr)
        return _classify_cli_error(message)


def _parse_question_responses(
    *,
    response: tuple[str, ...] = (),
    response_json: str | None = None,
) -> tuple[QuestionResponse, ...]:
    if response_json is not None and response:
        raise _CliUsageError("--response and --response-json cannot be used together")
    if response_json is not None:
        raw_payload = json.loads(response_json)
        if not isinstance(raw_payload, list) or not raw_payload:
            raise ValueError("--response-json must be a non-empty JSON array")
        raw_items = cast(list[object], raw_payload)
        parsed: list[QuestionResponse] = []
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                raise ValueError(f"--response-json[{index}] must be an object")
            item = cast(dict[str, object], raw_item)
            raw_header = item.get("header")
            if not isinstance(raw_header, str) or not raw_header.strip():
                raise ValueError(f"--response-json[{index}].header must be a non-empty string")
            raw_answers = item.get("answers")
            if not isinstance(raw_answers, list) or not raw_answers:
                raise ValueError(f"--response-json[{index}].answers must be a non-empty array")
            answers: list[str] = []
            for answer_index, raw_answer in enumerate(raw_answers):
                if not isinstance(raw_answer, str) or not raw_answer.strip():
                    raise ValueError(
                        f"--response-json[{index}].answers[{answer_index}] "
                        "must be a non-empty string"
                    )
                answers.append(raw_answer)
            parsed.append(QuestionResponse(header=raw_header, answers=tuple(answers)))
        return tuple(parsed)
    if not response:
        raise _CliUsageError("at least one --response or --response-json must be provided")
    return (QuestionResponse(header="response", answers=tuple(response)),)


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
    show_thinking = cast(bool, getattr(args, "show_thinking", False))
    cli_reasoning_effort = cast(str | None, getattr(args, "reasoning_effort", None))
    config = load_runtime_config(
        workspace,
        approval_mode=cast(PermissionDecision | None, getattr(args, "approval_mode", None)),
        reasoning_effort=cli_reasoning_effort,
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
        if cli_reasoning_effort is not None:
            metadata["reasoning_effort"] = cli_reasoning_effort
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
                show_thinking=show_thinking,
            )
        except KeyboardInterrupt:
            print("Interrupted current run.", file=sys.stderr)
            return 130
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None

        blocked_event = _pending_blocked_event(result.session, _last_event(result))
        if json_output:
            print_json(
                _runtime_stream_payload(
                    result,
                    workspace=workspace,
                    show_thinking=show_thinking,
                )
            )
            if not interactive and blocked_event is not None:
                return _blocked_exit_code(blocked_event)
            if result.session.status == "failed":
                return EXIT_RUNTIME_ERROR
        elif not interactive:
            if blocked_event is not None:
                _print_noninteractive_blocked(result, blocked_event)
                return _blocked_exit_code(blocked_event)
            _print_plain_runtime_output(result.output)
            _print_runtime_failure_footer(runtime, result, workspace=workspace)
            if result.session.status == "failed":
                return EXIT_RUNTIME_ERROR
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
    show_thinking: bool = False,
) -> RuntimeStreamResult:
    result = _consume_runtime_stream(
        runtime.run_stream(request),
        emit_events=emit_events,
        show_thinking=show_thinking,
        on_interrupt=lambda session_id, run_id: runtime.cancel_session(
            session_id,
            run_id=run_id,
            reason="cli KeyboardInterrupt",
        ),
    )

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
            show_thinking=show_thinking,
            on_interrupt=lambda session_id, run_id: runtime.cancel_session(
                session_id,
                run_id=run_id,
                reason="cli KeyboardInterrupt",
            ),
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
    show_thinking: bool = False,
    on_interrupt: Callable[[str, str | None], object] | None = None,
) -> RuntimeStreamResult:
    output: str | None = None
    final_session: SessionState | None = None
    events: list[EventEnvelope] = []

    try:
        for chunk in chunks:
            final_session = chunk.session
            if chunk.event is not None:
                if emit_events:
                    print(
                        format_event(
                            chunk.event.event_type,
                            chunk.event.source,
                            chunk.event.payload,
                            show_thinking=show_thinking,
                        ),
                        flush=True,
                    )
                events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output
    except KeyboardInterrupt:
        if final_session is not None and on_interrupt is not None:
            on_interrupt(
                final_session.session.id,
                _run_id_from_session_metadata(final_session.metadata),
            )
        raise

    if final_session is None:
        raise ValueError("runtime stream emitted no chunks")

    return RuntimeStreamResult(output=output, session=final_session, events=tuple(events))


def _run_id_from_session_metadata(metadata: dict[str, object]) -> str | None:
    runtime_state = metadata.get("runtime_state")
    if not isinstance(runtime_state, dict):
        return None
    typed_runtime_state = cast(dict[str, object], runtime_state)
    raw_run_id = typed_runtime_state.get("run_id")
    return raw_run_id if isinstance(raw_run_id, str) and raw_run_id else None


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
    show_thinking: bool = False,
) -> int:
    typed_result = cast("RuntimeResponseLike", result)

    for event in typed_result.events[event_offset:]:
        print(
            format_event(
                event.event_type,
                event.source,
                event.payload,
                show_thinking=show_thinking,
            ),
            flush=True,
        )

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


def _print_runtime_failure_footer(
    runtime: VoidCodeRuntime,
    result: RuntimeStreamResult,
    *,
    workspace: Path,
) -> None:
    if result.session.status != "failed":
        return
    failed_event = next(
        (event for event in reversed(result.events) if event.event_type == "runtime.failed"),
        None,
    )
    if failed_event is None:
        return
    try:
        snapshot = runtime.session_debug_snapshot(session_id=result.session.session.id)
    except ValueError:
        snapshot = None
    workspace_arg = f"--workspace {shlex.quote(str(workspace))}"
    provider = failed_event.payload.get("provider")
    model = failed_event.payload.get("model")
    provider_error_kind = failed_event.payload.get("provider_error_kind")
    last_tool = snapshot.last_tool if snapshot is not None else None
    resumable = snapshot.resumable if snapshot is not None else False
    print("", file=sys.stderr, flush=True)
    print("VoidCode runtime failure summary", file=sys.stderr, flush=True)
    print(f"  session: {result.session.session.id}", file=sys.stderr, flush=True)
    print(f"  status: {result.session.status}", file=sys.stderr, flush=True)
    if isinstance(provider, str) and provider:
        print(f"  provider: {provider}", file=sys.stderr, flush=True)
    if isinstance(model, str) and model:
        print(f"  model: {model}", file=sys.stderr, flush=True)
    if isinstance(provider_error_kind, str) and provider_error_kind:
        print(f"  provider_error_kind: {provider_error_kind}", file=sys.stderr, flush=True)
    print(f"  resumable: {str(resumable).lower()}", file=sys.stderr, flush=True)
    if last_tool is not None:
        print(f"  last_successful_tool: {last_tool.tool_name}", file=sys.stderr, flush=True)
    print(
        f"  debug: voidcode sessions debug {result.session.session.id} {workspace_arg}",
        file=sys.stderr,
        flush=True,
    )
    if resumable:
        print(
            f"  resume: voidcode sessions resume {result.session.session.id} {workspace_arg}",
            file=sys.stderr,
            flush=True,
        )


def _runtime_stream_payload(
    result: RuntimeStreamResult,
    *,
    workspace: Path,
    show_thinking: bool = False,
) -> dict[str, object]:
    blocked_event = _pending_blocked_event(result.session, _last_event(result))
    payload: dict[str, object] = {
        "workspace": str(workspace),
        "session": serialize_session_state(result.session),
        "output": result.output,
        "events": [serialize_event(event, show_thinking=show_thinking) for event in result.events],
    }
    if result.session.status == "failed":
        payload["status"] = "failed"
        error = _runtime_failed_error(result)
        if error is not None:
            payload["error"] = error
    if blocked_event is not None:
        payload["blocked"] = _blocked_payload(result, blocked_event)
    return payload


def _runtime_failed_error(result: RuntimeStreamResult) -> str | None:
    failed_event = next(
        (event for event in reversed(result.events) if event.event_type == "runtime.failed"),
        None,
    )
    if failed_event is None:
        return None
    error = failed_event.payload.get("error")
    return error if isinstance(error, str) and error else None


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
    observability = getattr(task, "observability", None)
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


def _background_task_observability_payload(task_or_result: object) -> dict[str, object] | None:
    observability = getattr(task_or_result, "observability", None)
    if observability is None:
        return None
    return cast(dict[str, object], observability.as_payload())


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
        "observability": _background_task_observability_payload(task),
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
        "observability": _background_task_observability_payload(result),
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
        "observability": _background_task_observability_payload(task),
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
    observability = getattr(result, "observability", None)
    if observability is not None:
        fields.append(("waiting_reason", observability.waiting_reason))
        if observability.queue_position is not None:
            fields.append(("queue_position", observability.queue_position))
        if observability.terminal_reason is not None:
            fields.append(("terminal_reason", observability.terminal_reason))
        if observability.concurrency is not None:
            concurrency = observability.concurrency
            fields.append(("active_worker_slots", concurrency.active_worker_slots))
            fields.append(("concurrency_limit", concurrency.limit))
            fields.append(("queued_total", concurrency.queued_total))
        if observability.retry is not None:
            retry = observability.retry
            fields.append(("retry_count", retry.retry_count))
            fields.append(("retry_backoff_seconds", retry.backoff_seconds))
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
    observability = getattr(task, "observability", None)
    if observability is not None:
        fields.append(("waiting_reason", observability.waiting_reason))
        if observability.queue_position is not None:
            fields.append(("queue_position", observability.queue_position))
        if observability.concurrency is not None:
            fields.append(("active_worker_slots", observability.concurrency.active_worker_slots))
            fields.append(("queued_total", observability.concurrency.queued_total))
    return _format_named_record("TASK", fields)


def _serialize_session_debug_snapshot(
    snapshot: RuntimeSessionDebugSnapshot,
    *,
    show_thinking: bool = False,
) -> dict[str, object]:
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
        "revert_marker": _serialize_revert_marker(snapshot.revert_marker),
        "last_event_sequence": snapshot.last_event_sequence,
        "last_relevant_event": _serialize_session_debug_event(
            snapshot.last_relevant_event,
            show_thinking=show_thinking,
        ),
        "last_failure_event": _serialize_session_debug_event(
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
        "provider_context": _serialize_provider_context_snapshot(snapshot.provider_context),
        "hook_presets": _serialize_hook_preset_snapshot(snapshot.hook_presets),
        "suggested_operator_action": snapshot.suggested_operator_action,
        "operator_guidance": snapshot.operator_guidance,
    }


def _serialize_hook_preset_snapshot(
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


def _serialize_provider_context_snapshot(
    snapshot: RuntimeProviderContextSnapshot | None,
) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "provider": snapshot.provider,
        "model": snapshot.model,
        "execution_engine": snapshot.execution_engine,
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
                "blocking_diagnostic_codes": list(
                    snapshot.policy_decision.blocking_diagnostic_codes
                ),
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


def _serialize_session_debug_event(
    event: object | None,
    *,
    show_thinking: bool = False,
) -> dict[str, object] | None:
    if event is None:
        return None
    typed_event = cast(EventEnvelope, event)
    return {
        "sequence": typed_event.sequence,
        "event_type": typed_event.event_type,
        "source": typed_event.source,
        "payload": redact_reasoning_payload(
            typed_event.event_type,
            typed_event.payload,
            show_thinking=show_thinking,
        ),
    }


def _handle_sessions_resume_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    dry_run = cast(bool, getattr(args, "dry_run", False))
    approval_decision = cast(PermissionResolution | None, getattr(args, "approval_decision", None))
    show_thinking = cast(bool, getattr(args, "show_thinking", False))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        if dry_run:
            try:
                snapshot = runtime.session_debug_snapshot(session_id=session_id)
            except ValueError as exc:
                raise SystemExit(f"error: {exc}") from None
            print_json(
                {
                    "workspace": str(workspace),
                    "session_id": session_id,
                    "dry_run": True,
                    "debug": _serialize_session_debug_snapshot(snapshot),
                }
            )
            return 0
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

    _print_runtime_response(result, show_thinking=show_thinking)
    return 0


def _handle_sessions_answer_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    question_request_id = cast(str, args.question_request_id)
    show_thinking = cast(bool, getattr(args, "show_thinking", False))
    json_output = cast(bool, getattr(args, "json", False))
    try:
        responses = _parse_question_responses(
            response=cast(tuple[str, ...], getattr(args, "response", ())),
            response_json=cast(str | None, getattr(args, "response_json", None)),
        )
    except _CliUsageError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from None

    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            result = runtime.answer_question(
                session_id,
                question_request_id=question_request_id,
                responses=responses,
            )
        except (ValueError, NoPendingQuestionError) as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    if json_output:
        print_json(
            {
                "workspace": str(workspace),
                "session": serialize_session_state(result.session),
                "events": [
                    serialize_event(event, show_thinking=show_thinking) for event in result.events
                ],
                "output": result.output,
            }
        )
        return 0
    _print_runtime_response(result, show_thinking=show_thinking)
    return 0


def _session_bundle_options_from_args(args: argparse.Namespace) -> SessionBundleOptions:
    if cast(bool, getattr(args, "support", False)):
        return SessionBundleOptions.support_artifact()
    return SessionBundleOptions(
        redact=cast(bool, getattr(args, "redact", True)),
        include_tool_output=cast(bool, getattr(args, "include_tool_output", False)),
        include_raw_provider_messages=cast(
            bool,
            getattr(args, "include_raw_provider_messages", False),
        ),
        include_reasoning_text=cast(bool, getattr(args, "include_reasoning_text", False)),
    )


def _handle_sessions_export_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    output_path = cast(Path | None, getattr(args, "output", None))
    fmt = cast(SessionBundleFormat, getattr(args, "format", "zip"))
    options = _session_bundle_options_from_args(args)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            bundle = runtime.export_session_bundle(session_id=session_id, options=options)
        except (ValueError, SessionBundleError) as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    if output_path is None and fmt == "json":
        print(json.dumps(bundle.to_payload(), sort_keys=True))
        return 0

    if output_path is None:
        output_path = Path(f"{session_id}.vcsession.zip")
    written = write_session_bundle(bundle, path=output_path, fmt=fmt)
    print_json(
        {
            "workspace": str(workspace),
            "session_id": session_id,
            "output": str(written),
            "format": fmt,
            "schema": bundle.to_payload()["schema"],
            "manifest": bundle.to_payload()["manifest"],
        }
    )
    return 0


def _handle_sessions_import_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    bundle_path = cast(Path, args.bundle_path)
    dry_run = cast(bool, getattr(args, "dry_run", False))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            result = runtime.import_session_bundle_file(
                bundle_path=bundle_path,
                dry_run=dry_run,
            )
        except (ValueError, SessionBundleError) as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)
    print_json({"workspace": str(workspace), "import": result.to_payload()})
    return 0


def _handle_sessions_debug_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    show_thinking = cast(bool, getattr(args, "show_thinking", False))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            snapshot = runtime.session_debug_snapshot(session_id=session_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    debug_payload = _serialize_session_debug_snapshot(
        snapshot,
        show_thinking=show_thinking,
    )
    print(json.dumps(debug_payload, sort_keys=True))
    return 0


def _serialize_revert_marker(marker: RuntimeSessionRevertMarker | None) -> dict[str, object] | None:
    if marker is None:
        return None
    return {"sequence": marker.sequence, "active": marker.active}


def _handle_sessions_undo_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            marker = runtime.undo_session(session_id=session_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)
    print_json({"session_id": session_id, "revert_marker": _serialize_revert_marker(marker)})
    return 0


def _handle_sessions_revert_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            marker = runtime.revert_session(
                session_id=session_id,
                sequence=cast(int, args.sequence),
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)
    print_json({"session_id": session_id, "revert_marker": _serialize_revert_marker(marker)})
    return 0


def _handle_sessions_unrevert_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            marker = runtime.unrevert_session(session_id=session_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)
    print_json({"session_id": session_id, "revert_marker": _serialize_revert_marker(marker)})
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
            "supports_reasoning_summary": metadata.supports_reasoning_summary,
            "supports_thinking_budget": metadata.supports_thinking_budget,
            "supports_interleaved_reasoning": metadata.supports_interleaved_reasoning,
            "reasoning_visibility": metadata.reasoning_visibility,
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
        "reasoning_controls": getattr(readiness, "reasoning_controls", {}),
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

    from ..tui import VoidCodeTUI

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
        or "no pending question" in normalized
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


_SELECTABLE_AGENT_IDS = tuple(
    manifest.id for manifest in list_top_level_selectable_agent_manifests()
)
_APPROVAL_MODES = ("allow", "deny", "ask")
_APPROVAL_DECISIONS = ("allow", "deny")
_EXECUTION_ENGINES = ("deterministic", "provider")
_BUNDLE_FORMATS = ("zip", "json")
_EXAMPLES = """
Examples:
  voidcode run 'read README.md' --workspace .
  voidcode run 'read README.md' --json --workspace .
  voidcode sessions list --json --workspace .
  voidcode commands list --workspace .
  voidcode commands show /review --json --workspace .
""".strip()


def _workspace_option(help_text: str) -> Callable[[Callable[..., object]], Callable[..., object]]:
    return click.option(
        "--workspace",
        type=click.Path(path_type=Path),
        default=Path.cwd,
        show_default=False,
        help=help_text,
    )


def _json_option(help_text: str) -> Callable[[Callable[..., object]], Callable[..., object]]:
    return click.option("--json", "json_output", is_flag=True, help=help_text)


def _show_thinking_option(
    help_text: str,
) -> Callable[[Callable[..., object]], Callable[..., object]]:
    return click.option("--show-thinking", is_flag=True, help=help_text)


def _command_discovery_options(function: Callable[..., object]) -> Callable[..., object]:
    function = _workspace_option("Workspace root used to discover project-local commands.")(
        function
    )
    return click.option(
        "--user-commands-dir",
        type=click.Path(path_type=Path),
        help="Optional user command directory to merge before project commands.",
    )(function)


def _normalize_click_argv(argv: Sequence[str] | None) -> list[str] | None:
    if argv is None:
        return None
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item != "--skills":
            normalized.append(item)
            index += 1
            continue
        index += 1
        while index < len(argv) and not argv[index].startswith("-"):
            normalized.extend(("--skills", argv[index]))
            index += 1
    return normalized


def _run_click_command(command: click.Command, argv: Sequence[str] | None) -> int:
    try:
        result = command.main(
            args=_normalize_click_argv(argv),
            prog_name="voidcode",
            standalone_mode=False,
        )
        return EXIT_SUCCESS if result is None else cast(int, result)
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except _CliUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except SystemExit as exc:
        return _handle_cli_system_exit(exc)
    except RuntimeError as exc:
        message = f"error: {exc}"
        print(message, file=sys.stderr)
        return _classify_cli_error(message)


@click.group(
    invoke_without_command=True,
    help="Voidcode command-line interface.\n\n" + _EXAMPLES,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "--version", prog_name="voidcode")
@click.pass_context
def root_cli(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@root_cli.command(help="Run the VoidCode interactive Textual UI.")
@_workspace_option("Workspace root used to resolve relative read paths.")
@click.option(
    "--approval-mode",
    type=click.Choice(_APPROVAL_MODES),
    help="Override the runtime approval mode for this invocation.",
)
def tui(workspace: Path, approval_mode: str | None) -> int:
    return _invoke_handler_from_click(
        _handle_tui_command,
        _build_click_command_context(
            command="tui",
            workspace=workspace,
            approval_mode=approval_mode,
        ),
    )


@root_cli.command(help="Run through the local runtime provider or deterministic harness.")
@click.argument("request")
@_workspace_option("Workspace root used to resolve relative read paths.")
@click.option("--session-id", help="Optional session identifier used for persisted runs.")
@click.option(
    "--approval-mode",
    type=click.Choice(_APPROVAL_MODES),
    help="Override the runtime approval mode for this invocation.",
)
@click.option(
    "--agent",
    type=click.Choice(_SELECTABLE_AGENT_IDS),
    help="Select a top-level agent preset for this run.",
)
@click.option("--skills", multiple=True, help="Optional skill names applied for this run.")
@click.option("--max-steps", type=int, help="Optional max graph steps override for this run.")
@click.option(
    "--reasoning-effort",
    help="Optional runtime-owned reasoning-effort hint forwarded to the active provider.",
)
@_show_thinking_option("Show persisted reasoning/thinking text; hidden by default.")
@_json_option("Output a structured JSON payload with session, events, and final output.")
@click.option(
    "--provider-stream/--no-provider-stream",
    default=None,
    help="Enable or disable provider-level streaming for this run.",
)
def run(
    request: str,
    workspace: Path,
    session_id: str | None,
    approval_mode: str | None,
    agent: str | None,
    skills: tuple[str, ...],
    max_steps: int | None,
    reasoning_effort: str | None,
    show_thinking: bool,
    json_output: bool,
    provider_stream: bool | None,
) -> int:
    return _invoke_handler_from_click(
        _handle_run_command,
        _build_click_command_context(
            command="run",
            request=request,
            workspace=workspace,
            session_id=session_id,
            approval_mode=approval_mode,
            agent=agent,
            skills=list(skills),
            max_steps=max_steps,
            reasoning_effort=reasoning_effort,
            show_thinking=show_thinking,
            json=json_output,
            provider_stream=provider_stream,
        ),
    )


@root_cli.command(help="Run the minimal external-facing ACP stdio JSON-RPC facade.")
@_workspace_option("Workspace root used by the ACP-backed runtime session database.")
@click.option(
    "--approval-mode",
    type=click.Choice(_APPROVAL_MODES),
    help="Override the runtime approval mode for this ACP process.",
)
def acp(workspace: Path, approval_mode: str | None) -> int:
    return _invoke_handler_from_click(
        _handle_acp_command,
        _build_click_command_context(
            command="acp",
            workspace=workspace,
            approval_mode=approval_mode,
        ),
    )


def _server_command(
    *,
    command: str,
    workspace: Path,
    host: str,
    port: int,
    approval_mode: str | None,
    server_entry: Callable[..., None],
    open_browser: bool | None = None,
) -> int:
    return _invoke_handler_from_click(
        _handle_server_command,
        _build_click_command_context(
            command=command,
            workspace=workspace,
            host=host,
            port=port,
            approval_mode=approval_mode,
            server_entry=server_entry,
        ),
    )


def _web_server_command(
    *,
    workspace: Path,
    host: str,
    port: int,
    approval_mode: str | None,
    open_browser: bool,
) -> int:
    return _invoke_handler_from_click(
        _handle_server_command,
        _build_click_command_context(
            command="web",
            workspace=workspace,
            host=host,
            port=port,
            approval_mode=approval_mode,
            server_entry=web,
            open_browser=open_browser,
        ),
    )


@root_cli.command(name="serve", help="Serve the local HTTP runtime transport.")
@_workspace_option("Workspace root used by the local runtime and session database.")
@click.option("--host", default="127.0.0.1", help="Host interface for the local transport server.")
@click.option("--port", type=int, default=8000, help="Port for the local transport server.")
@click.option(
    "--approval-mode",
    type=click.Choice(_APPROVAL_MODES),
    help="Override the runtime approval mode for this server process.",
)
def serve_command(workspace: Path, host: str, port: int, approval_mode: str | None) -> int:
    return _server_command(
        command="serve",
        workspace=workspace,
        host=host,
        port=port,
        approval_mode=approval_mode,
        server_entry=serve,
    )


@root_cli.command(
    name="web",
    help="Start the local web launcher entrypoint for the runtime transport.",
)
@_workspace_option("Workspace root used by the local runtime and session database.")
@click.option("--host", default="127.0.0.1", help="Host interface for the local launcher server.")
@click.option("--port", type=int, default=8000, help="Port for the local launcher server.")
@click.option(
    "--approval-mode",
    type=click.Choice(_APPROVAL_MODES),
    help="Override the runtime approval mode for this launcher process.",
)
@click.option(
    "--no-open",
    "open_browser",
    flag_value=False,
    default=True,
    help="Start the web launcher without opening a browser window.",
)
def web_command(
    workspace: Path,
    host: str,
    port: int,
    approval_mode: str | None,
    open_browser: bool,
) -> int:
    return _web_server_command(
        workspace=workspace,
        host=host,
        port=port,
        approval_mode=approval_mode,
        open_browser=open_browser,
    )


@root_cli.group(help="Inspect persisted local sessions.")
def sessions() -> None:
    pass


@sessions.command(name="list", help="List persisted sessions.")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output persisted sessions as JSON.")
def sessions_list(workspace: Path, json_output: bool) -> int:
    return _invoke_handler_from_click(
        _handle_sessions_list_command,
        _build_click_command_context(
            command="sessions",
            subcommand="list",
            workspace=workspace,
            json=json_output,
        ),
    )


@sessions.command(help="Replay a persisted session response.")
@click.argument("session_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@click.option(
    "--approval-request-id",
    help="Optional pending approval request identifier to resolve during resume.",
)
@click.option(
    "--approval-decision",
    type=click.Choice(_APPROVAL_DECISIONS),
    help="Optional approval decision applied to the pending request during resume.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Inspect the persisted session without resuming execution.",
)
@_show_thinking_option("Show persisted reasoning/thinking events during replay; hidden by default.")
def resume(
    session_id: str,
    workspace: Path,
    approval_request_id: str | None,
    approval_decision: str | None,
    dry_run: bool,
    show_thinking: bool,
) -> int:
    if (approval_request_id is None) != (approval_decision is None):
        raise _CliUsageError(
            "--approval-request-id and --approval-decision must be provided together"
        )
    return _invoke_handler_from_click(
        _handle_sessions_resume_command,
        _build_click_command_context(
            command="sessions",
            subcommand="resume",
            session_id=session_id,
            workspace=workspace,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
            dry_run=dry_run,
            show_thinking=show_thinking,
        ),
    )


@sessions.command(help="Answer a pending runtime.question_requested session.")
@click.argument("session_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@click.option(
    "--question-request-id",
    required=True,
    help="Pending question request identifier to answer.",
)
@click.option(
    "--response",
    multiple=True,
    help="Text answer. Repeat for multi-answer simple responses.",
)
@click.option(
    "--response-json",
    help="JSON array of {header, answers} objects for multi-question answers.",
)
@_json_option("Output the resumed runtime response as JSON.")
@_show_thinking_option("Show persisted reasoning/thinking events during replay; hidden by default.")
def answer(
    session_id: str,
    workspace: Path,
    question_request_id: str,
    response: tuple[str, ...],
    response_json: str | None,
    json_output: bool,
    show_thinking: bool,
) -> int:
    return _invoke_handler_from_click(
        _handle_sessions_answer_command,
        _build_click_command_context(
            command="sessions",
            subcommand="answer",
            session_id=session_id,
            workspace=workspace,
            question_request_id=question_request_id,
            response=response,
            response_json=response_json,
            json=json_output,
            show_thinking=show_thinking,
        ),
    )


@sessions.command(name="export", help="Export a portable redacted session bundle.")
@click.argument("session_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@click.option("--output", type=click.Path(path_type=Path), help="Bundle output path.")
@click.option("--format", "fmt", type=click.Choice(_BUNDLE_FORMATS), default="zip")
@click.option("--redact/--no-redact", default=True)
@click.option("--include-tool-output", is_flag=True)
@click.option("--include-raw-provider-messages", is_flag=True)
@click.option("--include-reasoning-text", is_flag=True)
@click.option("--support", is_flag=True)
def sessions_export(
    session_id: str,
    workspace: Path,
    output: Path | None,
    fmt: str,
    redact: bool,
    include_tool_output: bool,
    include_raw_provider_messages: bool,
    include_reasoning_text: bool,
    support: bool,
) -> int:
    return _invoke_handler_from_click(
        _handle_sessions_export_command,
        _build_click_command_context(
            command="sessions",
            subcommand="export",
            session_id=session_id,
            workspace=workspace,
            output=output,
            format=fmt,
            redact=redact,
            include_tool_output=include_tool_output,
            include_raw_provider_messages=include_raw_provider_messages,
            include_reasoning_text=include_reasoning_text,
            support=support,
        ),
    )


@sessions.command(name="import", help="Import a portable session bundle for local inspection.")
@click.argument("bundle_path", type=click.Path(path_type=Path))
@_workspace_option("Workspace root used to resolve the local session database.")
@click.option("--dry-run", is_flag=True)
def sessions_import(bundle_path: Path, workspace: Path, dry_run: bool) -> int:
    return _invoke_handler_from_click(
        _handle_sessions_import_command,
        _build_click_command_context(
            command="sessions",
            subcommand="import",
            bundle_path=bundle_path,
            workspace=workspace,
            dry_run=dry_run,
        ),
    )


@sessions.command(help="Show a minimal runtime-owned debug snapshot for one session.")
@click.argument("session_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output JSON debug snapshot (default).")
@_show_thinking_option(
    "Include reasoning/thinking text in debug event payloads; hidden by default."
)
def debug(session_id: str, workspace: Path, json_output: bool, show_thinking: bool) -> int:
    _ = json_output
    return _invoke_handler_from_click(
        _handle_sessions_debug_command,
        _build_click_command_context(
            command="sessions",
            subcommand="debug",
            session_id=session_id,
            workspace=workspace,
            json=True,
            show_thinking=show_thinking,
        ),
    )


@sessions.command(help="Revert the latest user turn out of provider-facing context.")
@click.argument("session_id")
@_workspace_option("Workspace root used to resolve the local session database.")
def undo(session_id: str, workspace: Path) -> int:
    return _invoke_handler_from_click(
        _handle_sessions_undo_command,
        _build_click_command_context(
            command="sessions", subcommand="undo", session_id=session_id, workspace=workspace
        ),
    )


@sessions.command(help="Revert provider-facing context to an event sequence.")
@click.argument("session_id")
@click.option("--to", "sequence", type=int, required=True)
@_workspace_option("Workspace root used to resolve the local session database.")
def revert(session_id: str, sequence: int, workspace: Path) -> int:
    return _invoke_handler_from_click(
        _handle_sessions_revert_command,
        _build_click_command_context(
            command="sessions",
            subcommand="revert",
            session_id=session_id,
            sequence=sequence,
            workspace=workspace,
        ),
    )


@sessions.command(help="Clear an active conversation revert marker.")
@click.argument("session_id")
@_workspace_option("Workspace root used to resolve the local session database.")
def unrevert(session_id: str, workspace: Path) -> int:
    return _invoke_handler_from_click(
        _handle_sessions_unrevert_command,
        _build_click_command_context(
            command="sessions",
            subcommand="unrevert",
            session_id=session_id,
            workspace=workspace,
        ),
    )


@root_cli.group(help="Inspect delegated background tasks.")
def tasks() -> None:
    pass


@tasks.command(help="Show delegated task lifecycle state.")
@click.argument("task_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output delegated task state as JSON.")
def status(task_id: str, workspace: Path, json_output: bool) -> int:
    return _invoke_handler_from_click(
        _handle_tasks_status_command,
        _build_click_command_context(
            command="tasks",
            subcommand="status",
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        ),
    )


@tasks.command(help="Show delegated task output and correlation details.")
@click.argument("task_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output delegated task result and guidance as JSON.")
def output(task_id: str, workspace: Path, json_output: bool) -> int:
    return _invoke_handler_from_click(
        _handle_tasks_output_command,
        _build_click_command_context(
            command="tasks",
            subcommand="output",
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        ),
    )


@tasks.command(help="Cancel delegated background work.")
@click.argument("task_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output cancelled delegated task state as JSON.")
def cancel(task_id: str, workspace: Path, json_output: bool) -> int:
    return _invoke_handler_from_click(
        _handle_tasks_cancel_command,
        _build_click_command_context(
            command="tasks",
            subcommand="cancel",
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        ),
    )


@tasks.command(name="list", help="List delegated background tasks.")
@_workspace_option("Workspace root used to resolve the local session database.")
@click.option("--parent-session", "parent_session_id")
@_json_option("Output delegated task summaries as JSON.")
def tasks_list(workspace: Path, parent_session_id: str | None, json_output: bool) -> int:
    return _invoke_handler_from_click(
        _handle_tasks_list_command,
        _build_click_command_context(
            command="tasks",
            subcommand="list",
            workspace=workspace,
            parent_session_id=parent_session_id,
            json=json_output,
        ),
    )


@root_cli.group(help="Inspect and maintain the local runtime SQLite store.")
def storage() -> None:
    pass


@storage.command(help="Show SQLite runtime storage policy, checkpoint, size, and row counts.")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output storage diagnostics as JSON (default).")
def diagnostics(workspace: Path, json_output: bool) -> int:
    _ = json_output
    return _invoke_handler_from_click(
        _handle_storage_diagnostics_command,
        _build_click_command_context(
            command="storage", subcommand="diagnostics", workspace=workspace, json=True
        ),
    )


@storage.command(help="Prune terminal sessions and terminal background tasks from local storage.")
@_workspace_option("Workspace root used to resolve the local session database.")
@click.option("--keep-sessions", type=int)
@click.option("--keep-background-tasks", type=int)
@click.option("--older-than", type=int)
def prune(
    workspace: Path,
    keep_sessions: int | None,
    keep_background_tasks: int | None,
    older_than: int | None,
) -> int:
    return _invoke_handler_from_click(
        _handle_storage_prune_command,
        _build_click_command_context(
            command="storage",
            subcommand="prune",
            workspace=workspace,
            keep_sessions=keep_sessions,
            keep_background_tasks=keep_background_tasks,
            older_than=older_than,
        ),
    )


@storage.command(help="Delete the local pre-MVP runtime SQLite database and WAL/SHM files.")
@_workspace_option("Workspace root used to resolve the local session database.")
def reset(workspace: Path) -> int:
    return _invoke_handler_from_click(
        _handle_storage_reset_command,
        _build_click_command_context(command="storage", subcommand="reset", workspace=workspace),
    )


@root_cli.group(help="Inspect effective runtime configuration.")
def config() -> None:
    pass


@config.command(name="show", help="Show effective runtime config for a workspace or session.")
@_workspace_option("Workspace root used to resolve runtime config and sessions.")
@click.option("--session", "session_id")
@_json_option("Output JSON effective config (default).")
def config_show(workspace: Path, session_id: str | None, json_output: bool) -> int:
    _ = json_output
    return _invoke_handler_from_click(
        _handle_config_show_command,
        _build_click_command_context(
            command="config",
            subcommand="show",
            workspace=workspace,
            session_id=session_id,
            json=True,
        ),
    )


@config.command(name="schema", help="Print the JSON Schema for .voidcode.json.")
def config_schema() -> int:
    return _invoke_handler_from_click(
        _handle_config_schema_command,
        _build_click_command_context(command="config", subcommand="schema"),
    )


@config.command(name="init", help="Generate a starter workspace .voidcode.json.")
@_workspace_option("Workspace root where .voidcode.json should be generated.")
@click.option("--approval-mode", type=click.Choice(_APPROVAL_MODES), default="ask")
@click.option("--model")
@click.option("--execution-engine", type=click.Choice(_EXECUTION_ENGINES))
@click.option("--max-steps", type=int)
@click.option("--with-examples", is_flag=True)
@click.option("--print", "print_config", is_flag=True)
@click.option("--force", is_flag=True)
def config_init(
    workspace: Path,
    approval_mode: str,
    model: str | None,
    execution_engine: str | None,
    max_steps: int | None,
    with_examples: bool,
    print_config: bool,
    force: bool,
) -> int:
    return _invoke_handler_from_click(
        _handle_config_init_command,
        _build_click_command_context(
            command="config",
            subcommand="init",
            workspace=workspace,
            approval_mode=approval_mode,
            model=model,
            execution_engine=execution_engine,
            max_steps=max_steps,
            with_examples=with_examples,
            print=print_config,
            force=force,
        ),
    )


@root_cli.group(help="Inspect provider metadata.")
def provider() -> None:
    pass


@provider.command(help="Show or refresh available models for one provider.")
@click.argument("provider_name")
@_workspace_option("Workspace root used to resolve runtime config.")
@click.option("--refresh", is_flag=True)
def models(provider_name: str, workspace: Path, refresh: bool) -> int:
    return _invoke_handler_from_click(
        _handle_provider_models_command,
        _build_click_command_context(
            command="provider",
            subcommand="models",
            provider=provider_name,
            workspace=workspace,
            refresh=refresh,
        ),
    )


@provider.command(help="Show configured status, model limits, and model capabilities.")
@click.argument("provider_name")
@_workspace_option("Workspace root used to resolve runtime config.")
def inspect(provider_name: str, workspace: Path) -> int:
    return _invoke_handler_from_click(
        _handle_provider_inspect_command,
        _build_click_command_context(
            command="provider",
            subcommand="inspect",
            provider=provider_name,
            workspace=workspace,
        ),
    )


@root_cli.group(help="Discover prompt commands available to runtime requests.")
def commands() -> None:
    pass


@commands.command(name="list", help="List enabled prompt commands discovered for a workspace.")
@_command_discovery_options
@click.option("--include-hidden", is_flag=True)
@click.option("--include-disabled", is_flag=True)
@_json_option("Output discovered commands as JSON.")
def commands_list(
    workspace: Path,
    user_commands_dir: Path | None,
    include_hidden: bool,
    include_disabled: bool,
    json_output: bool,
) -> int:
    return _invoke_handler_from_click(
        _handle_commands_list_command,
        _build_click_command_context(
            command="commands",
            subcommand="list",
            workspace=workspace,
            user_commands_dir=user_commands_dir,
            include_hidden=include_hidden,
            include_disabled=include_disabled,
            json=json_output,
        ),
    )


@commands.command(
    name="show",
    help="Show one prompt command definition and rendered template source.",
)
@click.argument("name")
@_command_discovery_options
@click.option("--include-hidden", is_flag=True)
@click.option("--include-disabled", is_flag=True)
@_json_option("Output the command definition as JSON.")
def commands_show(
    name: str,
    workspace: Path,
    user_commands_dir: Path | None,
    include_hidden: bool,
    include_disabled: bool,
    json_output: bool,
) -> int:
    return _invoke_handler_from_click(
        _handle_commands_show_command,
        _build_click_command_context(
            command="commands",
            subcommand="show",
            name=name,
            workspace=workspace,
            user_commands_dir=user_commands_dir,
            include_hidden=include_hidden,
            include_disabled=include_disabled,
            json=json_output,
        ),
    )


@root_cli.group(help="Inspect runtime-managed MCP configuration and health.")
def mcp() -> None:
    pass


@mcp.command(name="list", help="List configured MCP servers and passive runtime status.")
@_workspace_option("Workspace root used to resolve runtime config and MCP state.")
@_json_option("Output MCP status as JSON.")
def mcp_list(workspace: Path, json_output: bool) -> int:
    return _invoke_handler_from_click(
        _handle_mcp_list_command,
        _build_click_command_context(
            command="mcp", subcommand="list", workspace=workspace, json=json_output
        ),
    )


@root_cli.command(help="Check runtime capability readiness (external tools, formatters, LSP, MCP).")
@_workspace_option("Workspace root used to resolve runtime config.")
@click.option("--verbose", "verbose", "-v", is_flag=True)
@_json_option("Output report in JSON format.")
def doctor(workspace: Path, verbose: bool, json_output: bool) -> int:
    return _invoke_handler_from_click(
        _handle_doctor_command,
        _build_click_command_context(
            command="doctor", workspace=workspace, verbose=verbose, json=json_output
        ),
    )


class _CompatChoiceAction:
    def __init__(self, dest: str) -> None:
        self.dest = dest


class _CompatSubparsers:
    def __init__(self, command: click.Command) -> None:
        commands = command.commands if isinstance(command, click.Group) else {}
        self.choices = {name: _CompatParser(child) for name, child in commands.items()}
        self._choices_actions = [_CompatChoiceAction(name) for name in commands]
        self._parser_class = _CompatParser


class _CompatSubparserContainer:
    def __init__(self, command: click.Command) -> None:
        self._group_actions = [_CompatSubparsers(command)]


class _CompatParser:
    def __init__(self, command: click.Command) -> None:
        self._command = command
        self._subparsers = _CompatSubparserContainer(command)


def build_parser() -> _CompatParser:
    return _CompatParser(root_cli)


def main(argv: Sequence[str] | None = None) -> int:
    return _run_click_command(root_cli, argv)
