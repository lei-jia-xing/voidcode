from __future__ import annotations

import builtins as _builtins
import json
import os
import shlex
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, TypedDict, TypeGuard, cast

import click

from .. import __version__
from ..acp.stdio import StdioAcpServer
from ..cli_support import (
    EXIT_APPROVAL_DENIED,
    EXIT_CONFIG_ERROR,
    EXIT_INVALID_COMMAND,
    EXIT_INVALID_RESOURCE,
    EXIT_RUNTIME_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    RuntimeStreamResult,
    format_event,
    print_json,
    serialize_command_definition,
    serialize_command_summary,
    serialize_event,
    serialize_memory_record,
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
    serialize_runtime_agent_config,
)
from ..runtime.config_schema import (
    format_starter_runtime_config_json,
    generate_starter_runtime_config,
    runtime_config_json_schema,
    write_runtime_config_payload,
)
from ..runtime.contracts import (
    AgentSummary,
    BackgroundTaskResult,
    CapabilityStatusSnapshot,
    NoPendingQuestionError,
    ProviderInspectResult,
    ProviderModelMetadata,
    ProviderReadinessResult,
    RuntimeHookPresetSnapshot,
    RuntimeMemoryStatusSnapshot,
    RuntimeProviderContextSnapshot,
    RuntimeRequest,
    RuntimeSessionDebugSnapshot,
    RuntimeSessionRevertMarker,
    RuntimeStreamChunk,
    validate_runtime_request_metadata,
)
from ..runtime.events import (
    EventEnvelope,
    redact_reasoning_payload,
    runtime_policy_observability_payload,
)
from ..runtime.memory import MemoryKind, MemoryRecord
from ..runtime.permission import PermissionDecision, PermissionResolution
from ..runtime.question import QuestionResponse
from ..runtime.service import VoidCodeRuntime
from ..runtime.session import SessionState, StoredSessionSummary
from ..runtime.session_metadata_helpers import runtime_state_run_id
from ..runtime.storage import SqliteSessionStore
from ..runtime.task import (
    BackgroundTaskState,
    StoredBackgroundTaskSummary,
)
from ..server import serve, web
from .handler_args import (
    AcpArgs,
    AgentsArgs,
    CommandsArgs,
    ConfigArgs,
    DoctorArgs,
    McpArgs,
    MemoryArgs,
    ProviderArgs,
    RunArgs,
    ServerArgs,
    SessionsArgs,
    StatsArgs,
    StorageArgs,
    TasksArgs,
    TuiArgs,
)

print = _builtins.print


class CliError(Exception):
    """Typed CLI error carrying an explicit exit code and message."""

    def __init__(self, *, code: int = 1, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _RunCommandConfigKwargs(TypedDict, total=False):
    approval_mode: PermissionDecision | None
    model: str
    reasoning_effort: str | None


def _parse_question_responses(
    *,
    response: tuple[str, ...] = (),
    response_json: str | None = None,
) -> tuple[QuestionResponse, ...]:
    if response_json is not None and response:
        raise CliError(
            code=EXIT_USAGE_ERROR,
            message="--response and --response-json cannot be used together",
        )
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
                    raise ValueError(f"--response-json[{index}].answers[{answer_index}] must be a non-empty string")
                answers.append(raw_answer)
            parsed.append(QuestionResponse(header=raw_header, answers=tuple(answers)))
        return tuple(parsed)
    if not response:
        raise CliError(
            code=EXIT_USAGE_ERROR,
            message="at least one --response or --response-json must be provided",
        )
    return (QuestionResponse(header="response", answers=tuple(response)),)


class TuiAppProtocol(Protocol):
    def run(self) -> None: ...


def _close_runtime(runtime: object) -> None:
    exit_method = getattr(runtime, "__exit__", None)
    if callable(exit_method):
        try:
            exit_method(None, None, None)
        except Exception as exc:
            print(f"warning: runtime cleanup error: {exc}", file=sys.stderr)


@contextmanager
def _runtime_session(
    workspace: Path,
    config: RuntimeConfig | None = None,
) -> Iterator[VoidCodeRuntime]:
    """Construct a runtime, yield it, and guarantee cleanup on exit."""
    if config is not None:
        runtime = VoidCodeRuntime(workspace=workspace, config=config)
    else:
        runtime = VoidCodeRuntime(workspace=workspace)
    try:
        yield runtime
    finally:
        _close_runtime(runtime)


def _emit_output(
    args: object,
    payload: object,
    plain_printer: Callable[[], object],
) -> int:
    """Emit handler output as JSON (when --json) or via the plain printer."""
    if getattr(args, "json", False):
        print_json(payload)
    else:
        plain_printer()
    return EXIT_SUCCESS


def _handle_run_command(args: RunArgs) -> int:
    workspace = args.workspace
    request_text = args.request
    json_output = args.json
    trace_output = args.trace
    if json_output and trace_output:
        raise CliError(code=EXIT_USAGE_ERROR, message="--json and --trace cannot be used together")
    show_thinking = args.show_thinking
    cli_reasoning_effort = args.reasoning_effort
    cli_model = args.model
    approval_mode: PermissionDecision | None = cast(PermissionDecision | None, args.approval_mode)
    config_kwargs: _RunCommandConfigKwargs = {
        "approval_mode": approval_mode,
        "reasoning_effort": cli_reasoning_effort,
    }
    if cli_model is not None:
        config_kwargs["model"] = cli_model
    config = load_runtime_config(workspace, **config_kwargs)
    with _runtime_session(workspace, config) as runtime:
        metadata: dict[str, object] = {}
        if args.agent is not None:
            metadata["agent"] = {"preset": args.agent}
        if args.skills:
            metadata["skills"] = list(args.skills)
        runtime_mode = args.runtime_mode
        if runtime_mode is not None:
            metadata["mode"] = runtime_mode
        if args.read_only:
            metadata["read_only"] = True
        if args.max_steps is not None:
            metadata["max_steps"] = args.max_steps
        if cli_reasoning_effort is not None:
            metadata["reasoning_effort"] = cli_reasoning_effort
        provider_stream = args.provider_stream
        if provider_stream is not None:
            metadata["provider_stream"] = args.provider_stream
        elif trace_output:
            metadata["provider_stream"] = True
        request = RuntimeRequest(
            prompt=request_text,
            session_id=args.session_id,
            metadata=validate_runtime_request_metadata(metadata),
        )
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
        try:
            result = _run_with_inline_approval(
                runtime,
                request,
                interactive=interactive,
                emit_events=interactive and not json_output and not trace_output,
                trace_events=trace_output,
                show_thinking=show_thinking,
            )
        except KeyboardInterrupt:
            print("Interrupted current run.", file=sys.stderr)
            return 130
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

        incomplete_stream_message = _incomplete_runtime_stream_message(result)
        if incomplete_stream_message is not None:
            if trace_output:
                print(f"\n✖ Failed: {incomplete_stream_message}", flush=True)
            else:
                print(incomplete_stream_message, file=sys.stderr, flush=True)
            return EXIT_RUNTIME_ERROR

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
        elif trace_output:
            _print_trace_final(result)
            _print_runtime_failure_footer(runtime, result, workspace=workspace)
            if blocked_event is not None:
                _print_trace_blocked(result, blocked_event, workspace=workspace)
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
    return EXIT_SUCCESS


def _handle_acp_command(args: AcpArgs) -> int:
    workspace = args.workspace
    acp_approval_mode: PermissionDecision | None = cast(PermissionDecision | None, args.approval_mode)
    config = load_runtime_config(
        workspace,
        approval_mode=acp_approval_mode,
    )
    with _runtime_session(workspace, config) as runtime:
        server = StdioAcpServer(runtime=runtime, workspace=workspace)
        return server.serve()


def _run_with_inline_approval(
    runtime: VoidCodeRuntime,
    request: RuntimeRequest,
    *,
    interactive: bool,
    emit_events: bool,
    trace_events: bool = False,
    show_thinking: bool = False,
) -> RuntimeStreamResult:
    trace_printer = _TracePrinter(show_thinking=show_thinking) if trace_events else None
    result = _consume_runtime_stream(
        runtime.run_stream(request),
        emit_events=emit_events,
        trace_printer=trace_printer,
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
            trace_printer=trace_printer,
            show_thinking=show_thinking,
            on_interrupt=lambda session_id, run_id: runtime.cancel_session(
                session_id,
                run_id=run_id,
                reason="cli KeyboardInterrupt",
            ),
        )
        merged_events = (*result.events, *resumed_result.events)
        result = RuntimeStreamResult(
            output=resumed_result.output,
            session=resumed_result.session,
            events=merged_events,
        )

    if interactive and (not emit_events or trace_events):
        return result
    if interactive:
        _print_runtime_output(result.output)

    return result


def _consume_runtime_stream(
    chunks: Iterator[RuntimeStreamChunk],
    *,
    emit_events: bool,
    trace_printer: _TracePrinter | None = None,
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
                if trace_printer is not None:
                    trace_printer.handle_event(chunk.event)
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


def _incomplete_runtime_stream_message(result: RuntimeStreamResult) -> str | None:
    if result.session.status in {"completed", "failed", "waiting"}:
        return None
    if _has_permission_denied_tool_result(result.events):
        return None
    if _pending_blocked_event(result.session, _last_event(result)) is not None:
        return None
    last_event = _last_event(result)
    if last_event is not None and last_event.event_type == "runtime.failed":
        return None
    return f"runtime stream ended without a terminal outcome; last session status was {result.session.status}"


def _last_event_is_permission_denied_tool_result(event: EventEnvelope | None) -> bool:
    if event is None or event.event_type != "runtime.tool_completed":
        return False
    return event.payload.get("permission_denied") is True


def _has_permission_denied_tool_result(events: tuple[EventEnvelope, ...]) -> bool:
    return any(_last_event_is_permission_denied_tool_result(event) for event in events)


def _run_id_from_session_metadata(metadata: dict[str, object]) -> str | None:
    run_id = runtime_state_run_id(metadata)
    return run_id if run_id else None


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


class _TracePrinter:
    def __init__(self, *, show_thinking: bool = False) -> None:
        self._show_thinking = show_thinking
        self._model_text_open = False
        self._reasoning_open = False

    def handle_event(self, event: EventEnvelope) -> None:
        payload = redact_reasoning_payload(
            event.event_type,
            event.payload,
            show_thinking=self._show_thinking,
        )
        if event.event_type == "graph.model_turn":
            self._close_open_streams()
            provider = _trace_string(payload.get("provider"))
            model = _trace_string(payload.get("model"))
            turn = payload.get("turn")
            label = " · ".join(part for part in (provider, model) if part)
            if label:
                print(f"\n● Model turn {turn}: {label}", flush=True)
            else:
                print(f"\n● Model turn {turn}", flush=True)
            return
        if event.event_type == "graph.provider_stream":
            channel = _trace_string(payload.get("channel"))
            if channel == "text" and payload.get("kind") in {
                "delta",
                "content",
            }:
                text = _trace_string(payload.get("text"))
                if text:
                    self._close_reasoning_text()
                    print(text, end="", flush=True)
                    self._model_text_open = True
            elif channel == "reasoning" and payload.get("kind") in {"delta", "content"}:
                text = _trace_string(payload.get("text"))
                if text:
                    self._print_reasoning_text(text)
            return
        if event.event_type == "runtime.reasoning_part":
            text = _trace_string(payload.get("text"))
            if text:
                self._print_reasoning_text(text)
            return
        if event.event_type == "graph.tool_request_created":
            self._close_open_streams()
            _print_trace_tool_request(payload)
            return
        if event.event_type == "runtime.tool_started":
            self._close_open_streams()
            _print_trace_tool_started(payload)
            return
        if event.event_type == "runtime.tool_progress":
            self._close_open_streams()
            _print_trace_tool_progress(payload)
            return
        if event.event_type == "runtime.tool_completed":
            self._close_open_streams()
            _print_trace_tool_completed(payload)
            return
        if event.event_type == "runtime.todo_updated":
            self._close_open_streams()
            _print_trace_todos(payload)
            return
        if event.event_type == "runtime.approval_requested":
            self._close_open_streams()
            tool = _trace_string(payload.get("tool")) or "tool"
            target = _trace_string(payload.get("target_summary"))
            suffix = f" for {target}" if target else ""
            print(f"\n⚠ Approval required: {tool}{suffix}", flush=True)
            return
        if event.event_type == "runtime.question_requested":
            self._close_open_streams()
            count = payload.get("question_count")
            print(f"\n? Question required: {count or 1} prompt(s)", flush=True)
            return
        if event.event_type == "runtime.failed":
            self._close_open_streams()
            error = _trace_string(payload.get("error")) or "runtime failed"
            print(f"\n✖ Failed: {error}", flush=True)

    def _close_open_streams(self) -> None:
        self._close_model_text()
        self._close_reasoning_text()

    def _close_model_text(self) -> None:
        if self._model_text_open:
            print(flush=True)
            self._model_text_open = False

    def _close_reasoning_text(self) -> None:
        if self._reasoning_open:
            print(flush=True)
            self._reasoning_open = False

    def _print_reasoning_text(self, text: str) -> None:
        self._close_model_text()
        if not self._reasoning_open:
            print("[thinking] ", end="", flush=True)
            self._reasoning_open = True
        print(text, end="", flush=True)


def _print_trace_tool_request(payload: dict[str, object]) -> None:
    tool = _trace_string(payload.get("tool")) or "tool"
    arguments = payload.get("arguments")
    print(f"\n▸ Tool call: {tool}", flush=True)
    if tool == "shell_exec" and _is_string_keyed_mapping(arguments):
        command = _trace_string(arguments.get("command"))
        if command:
            print(f"  $ {command}", flush=True)
            return
    summary = _trace_tool_summary(payload)
    if summary:
        print(f"  {summary}", flush=True)


def _print_trace_tool_started(payload: dict[str, object]) -> None:
    tool = _trace_string(payload.get("tool")) or "tool"
    summary = _trace_tool_summary(payload)
    if summary:
        print(f"  started {tool}: {summary}", flush=True)
    else:
        print(f"  started {tool}", flush=True)


def _print_trace_tool_progress(payload: dict[str, object]) -> None:
    chunk = _trace_string(payload.get("chunk"))
    if not chunk:
        return
    stream = _trace_string(payload.get("stream")) or "output"
    for line in chunk.rstrip("\n").splitlines() or [""]:
        prefix = "│" if stream == "stdout" else "┃"
        print(f"  {prefix} {line}", flush=True)


def _print_trace_tool_completed(payload: dict[str, object]) -> None:
    tool = _trace_string(payload.get("tool")) or "tool"
    status = _trace_string(payload.get("status")) or "done"
    error = _trace_string(payload.get("error"))
    marker = "✓" if status == "ok" and not error else "✖"
    print(f"  {marker} {tool} {status}", flush=True)
    if error:
        print(f"    {error}", flush=True)


def _print_trace_todos(payload: dict[str, object]) -> None:
    todos = payload.get("todos")
    if not isinstance(todos, list):
        return
    print("\nTODO", flush=True)
    for todo in todos:
        if not _is_string_keyed_mapping(todo):
            continue
        status = _trace_string(todo.get("status")) or "pending"
        marker = "x" if status == "completed" else " "
        content = _trace_string(todo.get("content")) or "(empty todo)"
        print(f"  [{marker}] {content}", flush=True)


def _print_trace_final(result: RuntimeStreamResult) -> None:
    if result.output is None:
        return
    print("\nResult", flush=True)
    print(result.output, end="", flush=True)
    if not result.output.endswith("\n"):
        print(flush=True)


def _print_trace_blocked(
    result: RuntimeStreamResult,
    event: EventEnvelope,
    *,
    workspace: Path,
) -> None:
    workspace_arg = f"--workspace {shlex.quote(str(workspace))}"
    if event.event_type == "runtime.approval_requested":
        print(
            "Resume approval: "
            f"voidcode sessions resume {result.session.session.id} {workspace_arg} "
            f"--approval-request-id {_approval_request_id(event)} --approval-decision allow",
            flush=True,
        )
        return
    request_id = _trace_string(event.payload.get("request_id")) or "<request-id>"
    print(
        "Answer question: "
        f"voidcode sessions answer {result.session.session.id} {workspace_arg} "
        f"--question-request-id {request_id} --response <answer>",
        flush=True,
    )


def _trace_tool_summary(payload: dict[str, object]) -> str | None:
    display = payload.get("display")
    if _is_string_keyed_mapping(display):
        summary = _trace_string(display.get("summary"))
        if summary:
            return summary
    path = _trace_string(payload.get("path"))
    if path:
        return path
    arguments = payload.get("arguments")
    if _is_string_keyed_mapping(arguments):
        for key in ("path", "pattern", "query", "url", "description"):
            value = _trace_string(arguments.get(key))
            if value:
                return value
    return None


def _is_string_keyed_mapping(value: object) -> TypeGuard[dict[str, object]]:
    if not isinstance(value, dict):
        return False
    return all(isinstance(key, str) for key in value)


def _trace_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


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
    except ValueError as exc:
        print(f"warning: session debug snapshot unavailable: {exc}", file=sys.stderr)
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
    summary = failed_event.payload.get("error_summary")
    if isinstance(summary, str) and summary:
        return _format_runtime_error_summary(summary)
    error = failed_event.payload.get("error")
    if not isinstance(error, str) or not error:
        return None
    return _format_runtime_error_summary(error)


def _format_runtime_error_summary(error: str) -> str:
    cleaned = error.removeprefix("Error: ").strip()
    if not cleaned:
        return error
    for prefix in ("Runtime failed:", "runtime failed:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned or error


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


def _handle_sessions_list_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    with _runtime_session(workspace) as runtime:
        # The flat session list is the main-session surface: delegated child
        # sessions belong only to the child-session view and stay reachable
        # through the task/delegated-context endpoints, so exclude them here
        # (mirrors the HTTP transport list filter).
        sessions = [summary for summary in runtime.list_sessions() if summary.session.parent_id is None]

    def _print_sessions() -> None:
        for session in sessions:
            print(_format_session_summary(session))

    return _emit_output(
        args,
        {
            "workspace": str(workspace),
            "sessions": [serialize_stored_session_summary(session) for session in sessions],
        },
        _print_sessions,
    )


def _format_session_summary(session: StoredSessionSummary) -> str:
    return f"SESSION id={session.session.id} status={session.status} turn={session.turn} updated_at={session.updated_at} prompt={session.prompt!r}"


_MEMORY_KINDS: tuple[MemoryKind, ...] = (
    "project",
    "preference",
    "feedback",
    "reference",
    "decision",
)


def _parse_memory_kind(value: str) -> MemoryKind:
    if value in _MEMORY_KINDS:
        return value
    raise CliError(
        code=EXIT_USAGE_ERROR,
        message=f"invalid memory kind: {value}. Expected one of: {', '.join(_MEMORY_KINDS)}",
    )


def _memory_payload(memory: MemoryRecord) -> dict[str, object]:
    return serialize_memory_record(memory)


def _memory_list_payload(memories: Sequence[MemoryRecord]) -> dict[str, object]:
    return {
        "memories": [_memory_payload(memory) for memory in memories],
        "count": len(memories),
    }


def _memory_status_payload(status: RuntimeMemoryStatusSnapshot) -> dict[str, object]:
    return {
        "workspace_id": status.workspace_id,
        "database_path": status.database_path,
        "requires_active_session": False,
        "enabled": status.enabled,
        "scope": status.scope,
        "total_memories": status.total_count,
        "active_memories": status.active_count,
        "deleted_memories": status.deleted_count,
        "recall_enabled": status.recall_enabled,
        "semantic_search": status.semantic_search,
        "sqlite_vec": status.sqlite_vec,
        "keyword_search_available": status.keyword_search_available,
        "semantic_search_available": status.semantic_search_available,
        "sqlite_vec_status": status.sqlite_vec_status,
        "sqlite_vec_detail": status.sqlite_vec_detail,
    }


def _format_memory(memory: MemoryRecord) -> str:
    tags = ",".join(memory.tags) if memory.tags else "-"
    return _format_named_record(
        "MEMORY",
        [
            ("id", memory.id),
            ("kind", memory.kind),
            ("tags", tags),
            ("created_at", memory.created_at),
            ("content", repr(memory.content)),
        ],
    )


def _handle_memory_add_command(args: MemoryArgs) -> int:
    workspace = args.workspace
    content = args.content
    assert content is not None
    assert args.kind is not None
    if not content.strip():
        raise CliError(code=EXIT_USAGE_ERROR, message="memory content cannot be empty")
    kind = _parse_memory_kind(args.kind)
    tags = tuple(args.tag)
    with _runtime_session(workspace) as runtime:
        try:
            memory = runtime.add_memory(content=content, kind=kind, tags=tags)
        except ValueError as exc:
            raise CliError(code=EXIT_USAGE_ERROR, message=str(exc)) from None
    return _emit_output(
        args,
        {"memory": _memory_payload(memory)},
        lambda: print(f"Added memory {memory.id} kind={memory.kind} tags={','.join(memory.tags) or '-'}"),
    )


def _memory_filter_records(
    memories: Sequence[MemoryRecord],
    *,
    kind: str | None,
    tags: tuple[str, ...],
    limit: int | None,
) -> tuple[MemoryRecord, ...]:
    parsed_kind = _parse_memory_kind(kind) if kind is not None else None
    filtered = [memory for memory in memories if (parsed_kind is None or memory.kind == parsed_kind) and all(tag in memory.tags for tag in tags)]
    if limit is not None:
        if limit < 0:
            raise CliError(code=EXIT_USAGE_ERROR, message="limit must be non-negative")
        filtered = filtered[:limit]
    return tuple(filtered)


def _handle_memory_list_command(args: MemoryArgs) -> int:
    workspace = args.workspace
    with _runtime_session(workspace) as runtime:
        memories = runtime.list_memories()
    filtered = _memory_filter_records(
        memories,
        kind=args.kind,
        tags=tuple(args.tag),
        limit=args.limit,
    )

    def _print_memories() -> None:
        if not filtered:
            print("No memories found")
            return
        for memory in filtered:
            print(_format_memory(memory))

    return _emit_output(args, _memory_list_payload(filtered), _print_memories)


def _handle_memory_search_command(args: MemoryArgs) -> int:
    workspace = args.workspace
    query = args.query
    assert query is not None
    with _runtime_session(workspace) as runtime:
        results = runtime.search_memories(query=query)
    filtered = _memory_filter_records(
        tuple(result.record for result in results),
        kind=args.kind,
        tags=tuple(args.tag),
        limit=args.limit,
    )

    def _print_memory_results() -> None:
        if not filtered:
            print("No memories found")
            return
        for memory in filtered:
            print(_format_memory(memory))

    return _emit_output(
        args,
        {"query": query, **_memory_list_payload(filtered)},
        _print_memory_results,
    )


def _handle_memory_show_command(args: MemoryArgs) -> int:
    workspace = args.workspace
    memory_id = args.memory_id
    assert memory_id is not None
    with _runtime_session(workspace) as runtime:
        memory = runtime.get_memory(memory_id)
    if memory is None:
        raise CliError(code=EXIT_INVALID_RESOURCE, message=f"memory not found: {memory_id}")
    return _emit_output(
        args,
        {"memory": _memory_payload(memory)},
        lambda: print(_format_memory(memory)),
    )


def _handle_memory_delete_command(args: MemoryArgs) -> int:
    workspace = args.workspace
    memory_id = args.memory_id
    assert memory_id is not None
    with _runtime_session(workspace) as runtime:
        try:
            memory = runtime.delete_memory(memory_id)
        except ValueError as exc:
            raise CliError(code=EXIT_INVALID_RESOURCE, message=str(exc)) from None
    return _emit_output(
        args,
        {"deleted": True, "id": memory.id},
        lambda: print(f"Deleted memory {memory.id}"),
    )


def _handle_memory_status_command(args: MemoryArgs) -> int:
    workspace = args.workspace
    with _runtime_session(workspace) as runtime:
        status = runtime.memory_status()
    payload = _memory_status_payload(status)
    return _emit_output(
        args,
        payload,
        lambda: print(
            "Memory store status "
            f"workspace={payload['workspace_id']} database={payload['database_path']} "
            f"total={payload['total_memories']} deleted={payload['deleted_memories']} "
            f"keyword_search={str(payload['keyword_search_available']).lower()} "
            f"semantic_search={str(payload['semantic_search_available']).lower()} "
            f"sqlite_vec_status={payload['sqlite_vec_status']} active session: no"
        ),
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
    if task.keep_alive:
        fields.append(("keep_alive", True))
    if task.steer_prompt is not None:
        fields.append(("steer_prompt", task.steer_prompt))
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
    if any(token in normalized for token in ("tool", "write", "read", "shell_exec", "permission")):
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
        steps.append(f"Inspect waiting child session before answering questions: voidcode sessions debug {child_session_id} {workspace_arg}")
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


def _background_task_state_payload(task: BackgroundTaskState, *, workspace: Path) -> dict[str, object]:
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
        "keep_alive": task.keep_alive,
        "steer_prompt": task.steer_prompt,
        "cancellation_cause": cancellation_cause,
        "error": error,
        "error_type": error_type,
        "routing": _background_task_routing_payload(task.routing_identity),
        "observability": _background_task_observability_payload(task),
        "next_steps": next_steps,
    }
    return payload


def _background_task_result_payload(result: BackgroundTaskResult, *, workspace: Path) -> dict[str, object]:
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
        "keep_alive": task.keep_alive,
        "steer_prompt": task.steer_prompt,
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
    runtime_policy = _runtime_policy_debug_payload(snapshot)
    return {
        "session": {
            "session": session_payload,
            "status": snapshot.session.status,
            "turn": snapshot.session.turn,
            "metadata": snapshot.session.metadata,
        },
        **({"runtime_policy": runtime_policy} if runtime_policy is not None else {}),
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


def _runtime_policy_debug_payload(
    snapshot: RuntimeSessionDebugSnapshot,
) -> dict[str, object] | None:
    runtime_policy = snapshot.session.metadata.get("runtime_policy")
    if not isinstance(runtime_policy, dict):
        return None
    return runtime_policy_observability_payload(cast(dict[str, object], runtime_policy))


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
                "blocking_diagnostic_codes": list(snapshot.policy_decision.blocking_diagnostic_codes),
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


def _handle_sessions_resume_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    dry_run = args.dry_run
    approval_decision = args.approval_decision
    approval_decision_typed: PermissionResolution | None = cast(PermissionResolution | None, approval_decision)
    show_thinking = args.show_thinking
    with _runtime_session(workspace) as runtime:
        if dry_run:
            try:
                snapshot = runtime.session_debug_snapshot(session_id=session_id)
            except ValueError as exc:
                raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
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
                approval_request_id=args.approval_request_id,
                approval_decision=approval_decision_typed,
            )
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    _print_runtime_response(result, show_thinking=show_thinking)
    return 0


def _handle_sessions_answer_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    question_request_id = args.question_request_id
    assert question_request_id is not None
    show_thinking = args.show_thinking
    try:
        responses = _parse_question_responses(
            response=args.response,
            response_json=args.response_json,
        )
    except CliError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    with _runtime_session(workspace) as runtime:
        try:
            result = runtime.answer_question(
                session_id,
                question_request_id=question_request_id,
                responses=responses,
            )
        except NoPendingQuestionError as exc:
            raise CliError(code=EXIT_INVALID_RESOURCE, message=str(exc)) from None
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    return _emit_output(
        args,
        {
            "workspace": str(workspace),
            "session": serialize_session_state(result.session),
            "events": [serialize_event(event, show_thinking=show_thinking) for event in result.events],
            "output": result.output,
        },
        lambda: _print_runtime_response(result, show_thinking=show_thinking),
    )


def _session_bundle_options_from_args(args: SessionsArgs) -> SessionBundleOptions:
    if args.support:
        return SessionBundleOptions.support_artifact()
    return SessionBundleOptions(
        redact=args.redact,
        include_tool_output=args.include_tool_output,
        include_raw_provider_messages=args.include_raw_provider_messages,
        include_reasoning_text=args.include_reasoning_text,
    )


def _handle_sessions_export_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    output_path = args.output
    fmt = args.format
    options = _session_bundle_options_from_args(args)
    with _runtime_session(workspace) as runtime:
        try:
            bundle = runtime.export_session_bundle(session_id=session_id, options=options)
        except (ValueError, SessionBundleError) as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    if output_path is None and fmt == "json":
        print(json.dumps(bundle.to_payload(), sort_keys=True))
        return 0

    if output_path is None:
        output_path = Path(f"{session_id}.vcsession.zip")
    written = write_session_bundle(bundle, path=output_path, fmt=cast(SessionBundleFormat | None, fmt))
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


def _handle_sessions_import_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    bundle_path = args.bundle_path
    assert bundle_path is not None
    dry_run = args.dry_run
    with _runtime_session(workspace) as runtime:
        try:
            result = runtime.import_session_bundle_file(
                bundle_path=bundle_path,
                dry_run=dry_run,
            )
        except (ValueError, SessionBundleError) as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_json({"workspace": str(workspace), "import": result.to_payload()})
    return 0


def _handle_sessions_debug_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    show_thinking = args.show_thinking
    with _runtime_session(workspace) as runtime:
        try:
            snapshot = runtime.session_debug_snapshot(session_id=session_id)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

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


def _handle_sessions_undo_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    with _runtime_session(workspace) as runtime:
        try:
            marker = runtime.undo_session(session_id=session_id)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_json({"session_id": session_id, "revert_marker": _serialize_revert_marker(marker)})
    return 0


def _handle_sessions_revert_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    sequence = args.sequence
    assert sequence is not None
    with _runtime_session(workspace) as runtime:
        try:
            marker = runtime.revert_session(
                session_id=session_id,
                sequence=sequence,
            )
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_json({"session_id": session_id, "revert_marker": _serialize_revert_marker(marker)})
    return 0


def _handle_sessions_unrevert_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    with _runtime_session(workspace) as runtime:
        try:
            marker = runtime.unrevert_session(session_id=session_id)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_json({"session_id": session_id, "revert_marker": _serialize_revert_marker(marker)})
    return 0


def _handle_tasks_status_command(args: TasksArgs) -> int:
    workspace = args.workspace
    task_id = args.task_id
    assert task_id is not None
    with _runtime_session(workspace) as runtime:
        try:
            task = runtime.load_background_task(task_id)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    payload = _background_task_state_payload(task, workspace=workspace)

    def _print_task() -> None:
        print(_format_background_task_state(task))
        _print_background_task_guidance(payload)

    return _emit_output(
        args,
        {"workspace": str(workspace), "task": payload},
        _print_task,
    )


def _handle_tasks_output_command(args: TasksArgs) -> int:
    workspace = args.workspace
    task_id = args.task_id
    assert task_id is not None
    with _runtime_session(workspace) as runtime:
        session_output: str | None = None
        try:
            task_result = runtime.load_background_task_result(task_id)
            if task_result.result_available and task_result.child_session_id is not None:
                try:
                    session_output = runtime.session_result(session_id=task_result.child_session_id).output
                except ValueError as exc:
                    print(f"warning: session result output unavailable: {exc}", file=sys.stderr)
                    session_output = None
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    fallback_output = task_result.summary_output if task_result.summary_output is not None else task_result.error
    if session_output is None and fallback_output is not None:
        print("warning: WARN: session output unavailable; using fallback output", file=sys.stderr)
    output = session_output if session_output is not None else fallback_output
    payload = _background_task_result_payload(task_result, workspace=workspace)

    def _print_task_output() -> None:
        print(_format_background_task_result(task_result))
        _print_background_task_guidance(payload)
        _print_runtime_output(output)

    return _emit_output(
        args,
        {"workspace": str(workspace), "task": payload, "output": output},
        _print_task_output,
    )


def _handle_tasks_cancel_command(args: TasksArgs) -> int:
    workspace = args.workspace
    task_id = args.task_id
    assert task_id is not None
    with _runtime_session(workspace) as runtime:
        try:
            task = runtime.cancel_background_task(task_id)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    payload = _background_task_state_payload(task, workspace=workspace)

    def _print_task() -> None:
        print(_format_background_task_state(task))
        _print_background_task_guidance(payload)

    return _emit_output(
        args,
        {"workspace": str(workspace), "task": payload},
        _print_task,
    )


def _handle_tasks_retry_command(args: TasksArgs) -> int:
    workspace = args.workspace
    task_id = args.task_id
    assert task_id is not None
    with _runtime_session(workspace) as runtime:
        try:
            task = runtime.retry_background_task(task_id)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    payload = _background_task_state_payload(task, workspace=workspace)
    payload["retry_of_task_id"] = task_id

    def _print_retry_task() -> None:
        print(_format_background_task_state(task))
        print(f"RETRY previous_task_id={task_id} new_task_id={task.task.id}")
        _print_background_task_guidance(payload)

    return _emit_output(
        args,
        {"workspace": str(workspace), "task": payload},
        _print_retry_task,
    )


def _handle_tasks_steer_command(args: TasksArgs) -> int:
    workspace = args.workspace
    task_id = args.task_id
    prompt = args.prompt
    assert task_id is not None
    assert prompt is not None
    with _runtime_session(workspace) as runtime:
        try:
            task = runtime.steer_background_task(task_id, prompt)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    payload = _background_task_state_payload(task, workspace=workspace)
    payload["steer_prompt"] = prompt

    def _print_steer_task() -> None:
        print(_format_background_task_state(task))
        print(f"STEER task_id={task_id} status={task.status}")
        _print_background_task_guidance(payload)

    return _emit_output(
        args,
        {"workspace": str(workspace), "task": payload},
        _print_steer_task,
    )


def _handle_tasks_list_command(args: TasksArgs) -> int:
    workspace = args.workspace
    parent_session_id = args.parent_session_id
    with _runtime_session(workspace) as runtime:
        try:
            tasks = (
                runtime.list_background_tasks_by_parent_session(parent_session_id=parent_session_id)
                if parent_session_id is not None
                else runtime.list_background_tasks()
            )
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    def _print_tasks() -> None:
        for task in tasks:
            print(_format_background_task_summary(task))

    return _emit_output(
        args,
        {
            "workspace": str(workspace),
            "parent_session_id": parent_session_id,
            "tasks": [_background_task_summary_payload(task) for task in tasks],
        },
        _print_tasks,
    )


def _handle_storage_diagnostics_command(args: StorageArgs) -> int:
    workspace = args.workspace
    with _runtime_session(workspace) as runtime:
        diagnostics = runtime.storage_diagnostics()
    print_json({"workspace": str(workspace), "storage": diagnostics})
    return EXIT_SUCCESS


def _format_rate(value: object) -> str:
    if not isinstance(value, int | float):
        return "-"
    return f"{value * 100:.1f}%"


def _handle_stats_tools_command(args: StatsArgs) -> int:
    workspace = args.workspace
    with _runtime_session(workspace) as runtime:
        report = runtime.tool_effectiveness_report()
    payload = report.to_payload()

    def _print_report() -> None:
        print(
            "TOOL EFFECTIVENESS "
            f"sessions={report.session_count} calls={report.tool_call_count} "
            f"success={report.success_count} errors={report.error_count} "
            f"success_rate={_format_rate(report.success_rate)}"
        )
        print(
            "SIGNALS "
            f"repeated_reads={report.repeated_read_count} followup_reads={report.followup_read_count} "
            f"compactions={report.compaction_count} approvals={report.approval_request_count} "
            f"resumed_runs={report.resumed_run_count} delegated_tasks={report.delegated_task_count}"
        )
        print(
            "TOKENS "
            f"input={report.input_tokens} output={report.output_tokens} "
            f"cache_read={report.cache_read_tokens} cache_write={report.cache_write_tokens} "
            f"uncached_input={report.uncached_input_tokens} cache_hit_rate={_format_rate(report.cache_hit_rate)}"
        )
        if not report.tools:
            print("No persisted tool calls for this workspace.")
            return
        print("TOOL                         CALLS     OK  ERRORS   RATE  RETRIES  TRUNCATED  ERROR KINDS")
        for tool in report.tools:
            error_kinds = ",".join(f"{kind}:{count}" for kind, count in tool.error_kinds.items()) or "-"
            print(
                f"{tool.tool[:28]:<28} {tool.calls:>5} {tool.successes:>6} {tool.errors:>7} "
                f"{_format_rate(tool.success_rate):>6} {tool.retries_after_error:>8} "
                f"{tool.truncated_results:>10}  {error_kinds}"
            )

    return _emit_output(
        args,
        {"workspace": str(workspace), "effectiveness": payload},
        _print_report,
    )


def _handle_storage_prune_command(args: StorageArgs) -> int:
    workspace = args.workspace
    with _runtime_session(workspace) as runtime:
        try:
            counts = runtime.prune_runtime_storage(
                keep_sessions=args.keep_sessions,
                keep_background_tasks=args.keep_background_tasks,
                older_than=args.older_than,
            )
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_json({"workspace": str(workspace), "pruned": counts})
    return EXIT_SUCCESS


def _handle_storage_reset_command(args: StorageArgs) -> int:
    workspace = args.workspace
    # Reset is a pure file-deletion operation on the global store. It must NOT
    # boot a full runtime: runtime teardown reconnects to the session store
    # (background-task shutdown terminalizes queued tasks), which would
    # recreate the database files that were just deleted.
    store = SqliteSessionStore()
    result = store.reset_runtime_storage(workspace=workspace)
    print_json({"storage": result})
    return EXIT_SUCCESS


def _handle_server_command(args: ServerArgs) -> int:
    workspace = args.workspace
    server_approval_mode: PermissionDecision | None = cast(PermissionDecision | None, args.approval_mode)
    config = load_runtime_config(
        workspace,
        approval_mode=server_approval_mode,
    )
    server_entry = args.server_entry
    if server_entry is None:
        raise CliError(code=EXIT_RUNTIME_ERROR, message="server entry function is not configured")
    common_kwargs: dict[str, object] = dict(
        workspace=workspace,
        host=args.host,
        port=args.port,
        config=config,
    )
    if args.command == "web":
        server_entry(**common_kwargs, open_browser=args.open_browser)
    else:
        server_entry(**common_kwargs)

    return 0


def _handle_config_show_command(args: ConfigArgs) -> int:
    workspace = args.workspace
    if not workspace.exists() or not workspace.is_dir():
        raise CliError(code=EXIT_INVALID_RESOURCE, message=f"workspace does not exist: {workspace}")

    session_id = args.session_id
    with _runtime_session(workspace) as runtime:
        try:
            effective_config = runtime.effective_runtime_config(session_id=session_id)
            readiness = runtime.provider_readiness(session_id=session_id)
            agents = runtime.effective_agent_model_config(session_id=session_id)
            status = runtime.current_status()
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    print_json(
        {
            "workspace": str(workspace),
            "session_id": session_id,
            "approval_mode": effective_config.approval_mode,
            "execution_engine": effective_config.execution_engine,
            "model": effective_config.model,
            "fallback_models": (list(effective_config.provider_fallback.fallback_models) if effective_config.provider_fallback is not None else []),
            "max_steps": effective_config.max_steps,
            "reasoning_effort": getattr(effective_config, "reasoning_effort", None),
            "agent": serialize_runtime_agent_config(getattr(effective_config, "agent", None)),
            "agents": agents,
            "resolved_provider": resolved_provider_snapshot(getattr(effective_config, "resolved_provider", None)),
            "provider_readiness": _provider_readiness_payload(readiness),
            "context_budget": {
                "context_window": readiness.context_window,
                "max_output_tokens": readiness.max_output_tokens,
            },
            "mcp": _mcp_status_payload(status.mcp),
        }
    )
    return EXIT_SUCCESS


def _serialize_agent_summary(summary: AgentSummary) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": summary.id,
        "label": summary.label,
        "description": summary.description,
        "mode": summary.mode,
        "selectable": summary.selectable,
        "configured": summary.configured,
        "model": summary.model,
        "model_label": summary.model_label,
        "model_source": summary.model_source,
        "provider": summary.provider,
        "fallback_chain": list(summary.fallback_chain),
    }
    if summary.source_scope is not None:
        payload["source_scope"] = summary.source_scope
    if summary.source_path is not None:
        payload["source_path"] = summary.source_path
    return payload


def _handle_agents_list_command(args: AgentsArgs) -> int:
    workspace = args.workspace
    if not workspace.exists() or not workspace.is_dir():
        raise CliError(code=EXIT_INVALID_RESOURCE, message=f"workspace does not exist: {workspace}")

    with _runtime_session(workspace) as runtime:
        summaries = runtime.list_agent_summaries()

    payload = {
        "workspace": str(workspace),
        "agents": [_serialize_agent_summary(summary) for summary in summaries],
    }

    def _print_agents() -> None:
        for summary in summaries:
            fields: list[tuple[str, object]] = [
                ("id", summary.id),
                ("label", summary.label),
                ("mode", summary.mode),
                ("selectable", summary.selectable),
                ("configured", summary.configured),
                ("model", summary.model),
                ("provider", summary.provider),
            ]
            if summary.source_scope is not None:
                fields.append(("source_scope", summary.source_scope))
            if summary.source_path is not None:
                fields.append(("source_path", summary.source_path))
            print(_format_named_record("AGENT", fields))

    return _emit_output(args, payload, _print_agents)


def _mcp_status_payload(snapshot: CapabilityStatusSnapshot) -> dict[str, object]:
    state = snapshot.state
    error = snapshot.error
    details = snapshot.details
    return {
        "state": state,
        "error": error,
        "details": details,
    }


def _handle_mcp_list_command(args: McpArgs) -> int:
    workspace = args.workspace
    if not workspace.exists() or not workspace.is_dir():
        raise CliError(code=EXIT_INVALID_RESOURCE, message=f"workspace does not exist: {workspace}")

    with _runtime_session(workspace) as runtime:
        status = runtime.current_status()

    payload = {
        "workspace": str(workspace),
        "mcp": _mcp_status_payload(status.mcp),
    }

    def _print_mcp() -> None:
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

    return _emit_output(args, payload, _print_mcp)


def _handle_commands_list_command(args: CommandsArgs) -> int:
    workspace = args.workspace
    registry = _load_cli_command_registry(args, workspace=workspace)
    commands = registry.list(
        include_hidden=args.include_hidden,
        include_disabled=args.include_disabled,
    )

    def _print_commands() -> None:
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

    return _emit_output(
        args,
        {
            "workspace": str(workspace),
            "commands": [serialize_command_summary(command) for command in commands],
        },
        _print_commands,
    )


def _handle_commands_show_command(args: CommandsArgs) -> int:
    workspace = args.workspace
    registry = _load_cli_command_registry(args, workspace=workspace)
    command_name = args.name
    assert command_name is not None
    command = registry.get(command_name)
    if command is None:
        raise CliError(
            code=EXIT_INVALID_COMMAND,
            message=f"unknown command: /{command_name.removeprefix('/')}",
        )
    if command.hidden and not args.include_hidden:
        raise CliError(code=EXIT_INVALID_COMMAND, message=f"unknown command: /{command.name}")
    if not command.enabled and not args.include_disabled:
        raise CliError(code=EXIT_INVALID_COMMAND, message=f"command is disabled: /{command.name}")

    payload = serialize_command_definition(command)

    def _print_command() -> None:
        print(f"/{command.name}")
        print(f"Source: {command.source}")
        print(f"Enabled: {command.enabled}")
        print(f"Description: {command.description}")
        if command.path is not None:
            print(f"Path: {command.path}")
        print("Template:")
        print(command.template, end="" if command.template.endswith("\n") else "\n")

    return _emit_output(args, payload, _print_command)


def _load_cli_command_registry(args: CommandsArgs, *, workspace: Path) -> CommandRegistry:
    user_commands_dir = args.user_commands_dir
    try:
        return load_command_registry(workspace=workspace, user_commands_dir=user_commands_dir)
    except ValueError as exc:
        raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None


def _handle_config_schema_command(args: ConfigArgs) -> int:
    _ = args
    print(json.dumps(runtime_config_json_schema(), indent=2, sort_keys=True))
    return 0


def _handle_config_init_command(args: ConfigArgs) -> int:
    workspace = args.workspace
    if not workspace.exists() or not workspace.is_dir():
        raise CliError(code=EXIT_INVALID_RESOURCE, message=f"workspace does not exist: {workspace}")

    try:
        payload = generate_starter_runtime_config(
            approval_mode=args.approval_mode,
            model=args.model,
            max_steps=args.max_steps,
            include_examples=args.with_examples,
        )
    except ValueError as exc:
        raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    if args.print:
        print(format_starter_runtime_config_json(payload), end="")
        return 0

    config_path = workspace.resolve() / RUNTIME_CONFIG_FILE_NAME
    if config_path.exists() and not args.force:
        raise CliError(
            code=EXIT_CONFIG_ERROR,
            message=f"runtime config already exists: {config_path}; pass --force to overwrite",
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


def _handle_provider_models_command(args: ProviderArgs) -> int:
    workspace = args.workspace
    provider = args.provider
    assert provider is not None
    refresh = args.refresh
    with _runtime_session(workspace) as runtime:
        try:
            if refresh:
                _ = runtime.refresh_provider_models(provider)
            result = runtime.provider_models_result(provider)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    payload: dict[str, object] = {
        "workspace": str(workspace),
        "provider": provider,
        "refreshed": refresh,
        "models": list(result.models),
        "model_metadata": {model: _provider_model_metadata_payload(metadata) for model, metadata in result.model_metadata.items()},
        "source": result.source,
        "last_refresh_status": result.last_refresh_status,
        "last_error": result.last_error,
        "discovery_mode": result.discovery_mode,
    }
    if refresh and result.source == "fallback":
        print(
            f"WARN provider.models.refresh provider={provider} source=fallback reason={result.last_error}",
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
            "modalities_input": list(metadata.modalities_input) if metadata.modalities_input is not None else None,
            "modalities_output": list(metadata.modalities_output) if metadata.modalities_output is not None else None,
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


def _provider_inspect_payload(result: ProviderInspectResult, *, workspace: Path) -> dict[str, object]:
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
            "model_metadata": {model: _provider_model_metadata_payload(metadata) for model, metadata in result.models.model_metadata.items()},
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
        "readiness": (_provider_readiness_payload(result.readiness) if result.readiness is not None else None),
        "current_model": result.current_model,
        "current_model_metadata": (
            None if result.current_model_metadata is None else _provider_model_metadata_payload(result.current_model_metadata)
        ),
    }


def _handle_provider_inspect_command(args: ProviderArgs) -> int:
    workspace = args.workspace
    provider = args.provider
    assert provider is not None
    with _runtime_session(workspace) as runtime:
        try:
            result = runtime.inspect_provider(provider)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

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


def _handle_tui_command(args: TuiArgs) -> int:
    workspace = args.workspace
    approval_mode: PermissionDecision | None = cast(PermissionDecision | None, args.approval_mode)

    from ..tui import VoidCodeTUI

    app = cast(TuiAppProtocol, VoidCodeTUI(workspace=workspace, approval_mode=approval_mode))
    app.run()
    return 0


def _handle_doctor_command(args: DoctorArgs) -> int:
    """Run the capability doctor to check external tool readiness."""
    workspace = args.workspace
    verbose = args.verbose
    json_output = args.json

    # Load runtime config to get all capability settings
    config_error: str | None = None
    config: RuntimeConfig | None = None
    results: list[CapabilityCheckResult] = []
    if args.fix:
        if args.model is None or not args.model.strip():
            raise CliError(code=EXIT_USAGE_ERROR, message="doctor --fix requires --model provider/model")
        config_path = workspace.resolve() / RUNTIME_CONFIG_FILE_NAME
        if config_path.exists():
            raise CliError(code=EXIT_CONFIG_ERROR, message=f"runtime config already exists: {config_path}; edit it or run config init --force")
        payload = generate_starter_runtime_config(model=args.model)
        written_path = write_runtime_config_payload(workspace, payload)
        print(json.dumps({"config_path": str(written_path), "next_command": f"voidcode doctor --workspace {workspace}"}))
        return EXIT_SUCCESS
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
    function = _workspace_option("Workspace root used to discover project-local commands.")(function)
    return click.option(
        "--user-commands-dir",
        type=click.Path(path_type=Path),
        help="Optional user command directory to merge before project commands.",
    )(function)


def _run_click_command(command: click.Command, argv: Sequence[str] | None) -> int:
    try:
        result = command.main(
            args=argv,
            prog_name="voidcode",
            standalone_mode=False,
        )
        return EXIT_SUCCESS if result is None else cast(int, result)
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except CliError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.code
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR


@click.group(
    invoke_without_command=True,
    help="Voidcode command-line interface.\n\n" + _EXAMPLES,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "--version", prog_name="voidcode")
@click.option(
    "--db-path",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Override the runtime SQLite database path. Sets VOIDCODE_DB_PATH "
        "for this invocation; otherwise the path resolves under "
        "$XDG_STATE_HOME/voidcode/sessions.sqlite3."
    ),
)
@click.pass_context
def root_cli(ctx: click.Context, db_path: Path | None) -> None:
    if db_path is not None:
        os.environ["VOIDCODE_DB_PATH"] = str(Path(db_path).expanduser().resolve())
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
    return _handle_tui_command(
        TuiArgs(
            workspace=workspace,
            approval_mode=approval_mode,
        )
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
    help="Select a top-level or local custom agent preset for this run.",
)
@click.option(
    "--mode",
    "runtime_mode",
    type=click.Choice(["normal", "plan"]),
    help="Select runtime mode metadata; plan is a runtime-enforced read-only mode.",
)
@click.option(
    "--read-only",
    is_flag=True,
    help="Request runtime-enforced read-only tool policy without selecting a named mode.",
)
@click.option("--model", help="Override the provider/model for this run.")
@click.option("--skills", multiple=True, help="Optional skill names applied for this run.")
@click.option("--max-steps", type=int, help="Optional max graph steps override for this run.")
@click.option(
    "--reasoning-effort",
    help="Reasoning-effort level: off, minimal, low, medium, high, xhigh, max.",
)
@_show_thinking_option("Show persisted reasoning/thinking text; hidden by default.")
@_json_option("Output a structured JSON payload with session, events, and final output.")
@click.option(
    "--trace",
    is_flag=True,
    help="Stream model text, TODO updates, tool calls, and command output for manual QA.",
)
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
    model: str | None,
    skills: tuple[str, ...],
    max_steps: int | None,
    reasoning_effort: str | None,
    show_thinking: bool,
    json_output: bool,
    trace: bool,
    provider_stream: bool | None,
    runtime_mode: str | None,
    read_only: bool,
) -> int:
    return _handle_run_command(
        RunArgs(
            request=request,
            workspace=workspace,
            session_id=session_id,
            approval_mode=approval_mode,
            agent=agent,
            model=model,
            skills=skills,
            max_steps=max_steps,
            reasoning_effort=reasoning_effort,
            show_thinking=show_thinking,
            json=json_output,
            trace=trace,
            provider_stream=provider_stream,
            runtime_mode=runtime_mode,
            read_only=read_only,
        )
    )


@root_cli.command(help="Run the minimal external-facing ACP stdio JSON-RPC facade.")
@_workspace_option("Workspace root used by the ACP-backed runtime session database.")
@click.option(
    "--approval-mode",
    type=click.Choice(_APPROVAL_MODES),
    help="Override the runtime approval mode for this ACP process.",
)
def acp(workspace: Path, approval_mode: str | None) -> int:
    return _handle_acp_command(
        AcpArgs(
            workspace=workspace,
            approval_mode=approval_mode,
        )
    )


def _server_command(
    *,
    command: str,
    workspace: Path,
    host: str,
    port: int,
    approval_mode: str | None,
    server_entry: Callable[..., None],
) -> int:
    return _handle_server_command(
        ServerArgs(
            command=command,
            workspace=workspace,
            host=host,
            port=port,
            approval_mode=approval_mode,
            server_entry=server_entry,
        )
    )


def _web_server_command(
    *,
    workspace: Path,
    host: str,
    port: int | None,
    approval_mode: str | None,
    open_browser: bool,
) -> int:
    return _handle_server_command(
        ServerArgs(
            command="web",
            workspace=workspace,
            host=host,
            port=port,
            approval_mode=approval_mode,
            server_entry=web,
            open_browser=open_browser,
        )
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
@click.option(
    "--port",
    type=int,
    default=None,
    help="Port for the local launcher server. Defaults to an auto-assigned local port.",
)
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
    port: int | None,
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
    return _handle_sessions_list_command(
        SessionsArgs(
            workspace=workspace,
            json=json_output,
        )
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
        raise CliError(
            code=EXIT_USAGE_ERROR,
            message="--approval-request-id and --approval-decision must be provided together",
        )
    return _handle_sessions_resume_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
            dry_run=dry_run,
            show_thinking=show_thinking,
        )
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
    return _handle_sessions_answer_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
            question_request_id=question_request_id,
            response=response,
            response_json=response_json,
            json=json_output,
            show_thinking=show_thinking,
        )
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
    return _handle_sessions_export_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
            output=output,
            format=fmt,
            redact=redact,
            include_tool_output=include_tool_output,
            include_raw_provider_messages=include_raw_provider_messages,
            include_reasoning_text=include_reasoning_text,
            support=support,
        )
    )


@sessions.command(name="import", help="Import a portable session bundle for local inspection.")
@click.argument("bundle_path", type=click.Path(path_type=Path))
@_workspace_option("Workspace root used to resolve the local session database.")
@click.option("--dry-run", is_flag=True)
def sessions_import(bundle_path: Path, workspace: Path, dry_run: bool) -> int:
    return _handle_sessions_import_command(
        SessionsArgs(
            bundle_path=bundle_path,
            workspace=workspace,
            dry_run=dry_run,
        )
    )


@sessions.command(help="Show a minimal runtime-owned debug snapshot for one session.")
@click.argument("session_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@_show_thinking_option("Include reasoning/thinking text in debug event payloads; hidden by default.")
def debug(session_id: str, workspace: Path, show_thinking: bool) -> int:
    return _handle_sessions_debug_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
            json=True,
            show_thinking=show_thinking,
        )
    )


@sessions.command(help="Revert the latest user turn out of provider-facing context.")
@click.argument("session_id")
@_workspace_option("Workspace root used to resolve the local session database.")
def undo(session_id: str, workspace: Path) -> int:
    return _handle_sessions_undo_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
        )
    )


@sessions.command(help="Revert provider-facing context to an event sequence.")
@click.argument("session_id")
@click.option("--to", "sequence", type=int, required=True)
@_workspace_option("Workspace root used to resolve the local session database.")
def revert(session_id: str, sequence: int, workspace: Path) -> int:
    return _handle_sessions_revert_command(
        SessionsArgs(
            session_id=session_id,
            sequence=sequence,
            workspace=workspace,
        )
    )


@sessions.command(help="Clear an active conversation revert marker.")
@click.argument("session_id")
@_workspace_option("Workspace root used to resolve the local session database.")
def unrevert(session_id: str, workspace: Path) -> int:
    return _handle_sessions_unrevert_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
        )
    )


@root_cli.group(name="memory", help="Manage explicit workspace memory records in the MVP.")
def memory() -> None:
    pass


@memory.command(name="add", help="Add an explicit workspace memory record.")
@click.argument("content")
@_workspace_option("Workspace whose memory store should receive the record.")
@click.option("--kind", default="project", help="Memory kind.")
@click.option("--tag", multiple=True, help="Tag to attach to the memory. Repeatable.")
@_json_option("Output the created memory as JSON.")
def memory_add(
    content: str,
    workspace: Path,
    kind: str,
    tag: tuple[str, ...],
    json_output: bool,
) -> int:
    return _handle_memory_add_command(
        MemoryArgs(
            content=content,
            workspace=workspace,
            kind=kind,
            tag=tag,
            json=json_output,
        )
    )


@memory.command(name="list", help="List explicit workspace memory records.")
@_workspace_option("Workspace whose memory store should be listed.")
@click.option("--kind", help="Only include one memory kind.")
@click.option("--tag", multiple=True, help="Only include memories with this tag. Repeatable.")
@click.option("--limit", type=int, help="Maximum number of memories to return.")
@_json_option("Output memories as JSON.")
def memory_list(
    workspace: Path,
    kind: str | None,
    tag: tuple[str, ...],
    limit: int | None,
    json_output: bool,
) -> int:
    return _handle_memory_list_command(
        MemoryArgs(
            workspace=workspace,
            kind=kind,
            tag=tag,
            limit=limit,
            json=json_output,
        )
    )


@memory.command(name="search", help="Search explicit workspace memory records.")
@click.argument("query")
@_workspace_option("Workspace whose memory store should be searched.")
@click.option("--kind", help="Only include one memory kind.")
@click.option("--tag", multiple=True, help="Only include memories with this tag. Repeatable.")
@click.option("--limit", type=int, help="Maximum number of memories to return.")
@_json_option("Output search results as JSON.")
def memory_search(
    query: str,
    workspace: Path,
    kind: str | None,
    tag: tuple[str, ...],
    limit: int | None,
    json_output: bool,
) -> int:
    return _handle_memory_search_command(
        MemoryArgs(
            query=query,
            workspace=workspace,
            kind=kind,
            tag=tag,
            limit=limit,
            json=json_output,
        )
    )


@memory.command(name="show", help="Show one explicit workspace memory record.")
@click.argument("memory_id")
@_workspace_option("Workspace whose memory store should be queried.")
@_json_option("Output the memory as JSON.")
def memory_show(memory_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_memory_show_command(
        MemoryArgs(
            memory_id=memory_id,
            workspace=workspace,
            json=json_output,
        )
    )


@memory.command(name="delete", help="Tombstone one explicit workspace memory record.")
@click.argument("memory_id")
@_workspace_option("Workspace whose memory store should be updated.")
@_json_option("Output delete status as JSON.")
def memory_delete(memory_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_memory_delete_command(
        MemoryArgs(
            memory_id=memory_id,
            workspace=workspace,
            json=json_output,
        )
    )


@memory.command(name="status", help="Show workspace memory storage status.")
@_workspace_option("Workspace whose memory storage scope should be reported.")
@_json_option("Output memory status as JSON.")
def memory_status(workspace: Path, json_output: bool) -> int:
    return _handle_memory_status_command(
        MemoryArgs(
            workspace=workspace,
            json=json_output,
        )
    )


@root_cli.group(help="Inspect delegated background tasks.")
def tasks() -> None:
    pass


@tasks.command(help="Show delegated task lifecycle state.")
@click.argument("task_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output delegated task state as JSON.")
def status(task_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_tasks_status_command(
        TasksArgs(
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        )
    )


@tasks.command(help="Show delegated task output and correlation details.")
@click.argument("task_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output delegated task result and guidance as JSON.")
def output(task_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_tasks_output_command(
        TasksArgs(
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        )
    )


@tasks.command(help="Cancel delegated background work.")
@click.argument("task_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output cancelled delegated task state as JSON.")
def cancel(task_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_tasks_cancel_command(
        TasksArgs(
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        )
    )


@tasks.command(help="Retry failed, cancelled, or interrupted delegated background work.")
@click.argument("task_id")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output retried delegated task state as JSON.")
def retry(task_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_tasks_retry_command(
        TasksArgs(
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        )
    )


@tasks.command(help="Dispatch a new worker turn for an idle keep-alive delegated task.")
@click.argument("task_id")
@click.argument("prompt")
@_workspace_option("Workspace root used to resolve the local session database.")
@_json_option("Output steered delegated task state as JSON.")
def steer(task_id: str, prompt: str, workspace: Path, json_output: bool) -> int:
    return _handle_tasks_steer_command(
        TasksArgs(
            task_id=task_id,
            prompt=prompt,
            workspace=workspace,
            json=json_output,
        )
    )


@tasks.command(name="list", help="List delegated background tasks.")
@_workspace_option("Workspace root used to resolve the local session database.")
@click.option("--parent-session", "parent_session_id")
@_json_option("Output delegated task summaries as JSON.")
def tasks_list(workspace: Path, parent_session_id: str | None, json_output: bool) -> int:
    return _handle_tasks_list_command(
        TasksArgs(
            workspace=workspace,
            parent_session_id=parent_session_id,
            json=json_output,
        )
    )


@root_cli.group(help="Inspect and maintain the local runtime SQLite store.")
def storage() -> None:
    pass


@storage.command(help="Show SQLite runtime storage policy, checkpoint, size, and row counts.")
@_workspace_option("Workspace root used to resolve the local session database.")
def diagnostics(workspace: Path) -> int:
    return _handle_storage_diagnostics_command(
        StorageArgs(
            workspace=workspace,
            json=True,
        )
    )


@root_cli.group(help="Inspect local agent effectiveness metrics.")
def stats() -> None:
    pass


@stats.command(name="tools", help="Summarize persisted tool success, errors, retries, and output pressure.")
@_workspace_option("Workspace root used to select persisted runtime sessions.")
@_json_option("Output tool effectiveness metrics as JSON.")
def stats_tools(workspace: Path, json_output: bool) -> int:
    return _handle_stats_tools_command(
        StatsArgs(
            workspace=workspace,
            json=json_output,
        )
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
    return _handle_storage_prune_command(
        StorageArgs(
            workspace=workspace,
            keep_sessions=keep_sessions,
            keep_background_tasks=keep_background_tasks,
            older_than=older_than,
        )
    )


@storage.command(help="Delete the runtime SQLite database and WAL/SHM files.")
@_workspace_option("Workspace root accepted for CLI parity; the database itself is global.")
def reset(workspace: Path) -> int:
    return _handle_storage_reset_command(
        StorageArgs(
            workspace=workspace,
        )
    )


@root_cli.group(help="Inspect effective runtime configuration.")
def config() -> None:
    pass


@config.command(name="show", help="Show effective runtime config for a workspace or session.")
@_workspace_option("Workspace root used to resolve runtime config and sessions.")
@click.option("--session", "session_id")
def config_show(workspace: Path, session_id: str | None) -> int:
    return _handle_config_show_command(
        ConfigArgs(
            workspace=workspace,
            session_id=session_id,
            json=True,
        )
    )


@config.command(name="schema", help="Print the JSON Schema for .voidcode.json.")
def config_schema() -> int:
    return _handle_config_schema_command(ConfigArgs())


@config.command(name="init", help="Generate a starter workspace .voidcode.json.")
@_workspace_option("Workspace root where .voidcode.json should be generated.")
@click.option("--approval-mode", type=click.Choice(_APPROVAL_MODES), default="ask")
@click.option("--model")
@click.option("--max-steps", type=int)
@click.option("--with-examples", is_flag=True)
@click.option("--print", "print_config", is_flag=True)
@click.option("--force", is_flag=True)
def config_init(
    workspace: Path,
    approval_mode: str,
    model: str | None,
    max_steps: int | None,
    with_examples: bool,
    print_config: bool,
    force: bool,
) -> int:
    return _handle_config_init_command(
        ConfigArgs(
            workspace=workspace,
            approval_mode=approval_mode,
            model=model,
            max_steps=max_steps,
            with_examples=with_examples,
            print=print_config,
            force=force,
        )
    )


@root_cli.group(help="Inspect provider metadata.")
def provider() -> None:
    pass


@provider.command(help="Show or refresh available models for one provider.")
@click.argument("provider_name")
@_workspace_option("Workspace root used to resolve runtime config.")
@click.option("--refresh", is_flag=True)
def models(provider_name: str, workspace: Path, refresh: bool) -> int:
    return _handle_provider_models_command(
        ProviderArgs(
            provider=provider_name,
            workspace=workspace,
            refresh=refresh,
        )
    )


@provider.command(help="Show configured status, model limits, and model capabilities.")
@click.argument("provider_name")
@_workspace_option("Workspace root used to resolve runtime config.")
def inspect(provider_name: str, workspace: Path) -> int:
    return _handle_provider_inspect_command(
        ProviderArgs(
            provider=provider_name,
            workspace=workspace,
        )
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
    return _handle_commands_list_command(
        CommandsArgs(
            workspace=workspace,
            user_commands_dir=user_commands_dir,
            include_hidden=include_hidden,
            include_disabled=include_disabled,
            json=json_output,
        )
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
    return _handle_commands_show_command(
        CommandsArgs(
            name=name,
            workspace=workspace,
            user_commands_dir=user_commands_dir,
            include_hidden=include_hidden,
            include_disabled=include_disabled,
            json=json_output,
        )
    )


@root_cli.group(help="Discover built-in and local custom agents available to runtime requests.")
def agents() -> None:
    pass


@agents.command(
    name="list",
    help="List built-in and local custom agent manifests discovered for a workspace.",
)
@_workspace_option("Workspace root used to discover project-local agents.")
@_json_option("Output discovered agents as JSON.")
def agents_list(workspace: Path, json_output: bool) -> int:
    return _handle_agents_list_command(
        AgentsArgs(
            workspace=workspace,
            json=json_output,
        )
    )


@root_cli.group(help="Inspect runtime-managed MCP configuration and health.")
def mcp() -> None:
    pass


@mcp.command(name="list", help="List configured MCP servers and passive runtime status.")
@_workspace_option("Workspace root used to resolve runtime config and MCP state.")
@_json_option("Output MCP status as JSON.")
def mcp_list(workspace: Path, json_output: bool) -> int:
    return _handle_mcp_list_command(
        McpArgs(
            workspace=workspace,
            json=json_output,
        )
    )


@root_cli.command(help="Check runtime capability readiness (external tools, formatters, LSP, MCP).")
@_workspace_option("Workspace root used to resolve runtime config.")
@click.option("--verbose", "verbose", "-v", is_flag=True)
@click.option("--fix", is_flag=True, help="Create a starter runtime config when none exists.")
@click.option("--model", type=str, help="Provider/model used with --fix, for example openai/gpt-4o.")
@_json_option("Output report in JSON format.")
def doctor(workspace: Path, verbose: bool, fix: bool, model: str | None, json_output: bool) -> int:
    return _handle_doctor_command(
        DoctorArgs(
            workspace=workspace,
            verbose=verbose,
            json=json_output,
            fix=fix,
            model=model,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    return _run_click_command(root_cli, argv)
