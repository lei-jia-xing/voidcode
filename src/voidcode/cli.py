from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Protocol, cast

from . import __version__
from .graph.contracts import GraphRunRequest
from .runtime.config import load_runtime_config
from .runtime.contracts import RuntimeRequest, RuntimeResponse, RuntimeStreamChunk
from .runtime.events import EventEnvelope
from .runtime.permission import PendingApproval, PermissionDecision, PermissionResolution
from .runtime.service import VoidCodeRuntime
from .runtime.session import SessionState, StoredSessionSummary
from .server import serve
from .tools.contracts import ToolDefinition, ToolResult

Handler = Callable[[argparse.Namespace], int]

_SESSION_STORE_ATTR = "_session_store"
_WORKSPACE_ATTR = "_workspace"
_TOOL_REGISTRY_ATTR = "_tool_registry"
_EXECUTE_GRAPH_LOOP_ATTR = "_execute_graph_loop"
_PERSIST_RESPONSE_ATTR = "_persist_response"
_REQUEST_ID_ATTR = "request_id"


def _format_event(event_type: str, source: str, data: dict[str, object]) -> str:
    suffix = " ".join(f"{key}={value}" for key, value in sorted(data.items()))
    if suffix:
        return f"EVENT {event_type} source={source} {suffix}"
    return f"EVENT {event_type} source={source}"


def _handle_run_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    request_text = cast(str, args.request)
    config = load_runtime_config(
        workspace,
        approval_mode=cast(PermissionDecision | None, getattr(args, "approval_mode", None)),
    )
    runtime = VoidCodeRuntime(workspace=workspace, config=config)
    request = RuntimeRequest(prompt=request_text, session_id=cast(str | None, args.session_id))
    interactive = sys.stdin.isatty() and sys.stderr.isatty()
    output = _run_with_inline_approval(
        runtime,
        request,
        interactive=interactive,
    )

    if not interactive:
        _print_runtime_output(output)
    return 0


def _run_with_inline_approval(
    runtime: VoidCodeRuntime,
    request: RuntimeRequest,
    *,
    interactive: bool,
) -> str | None:
    output, final_session, last_event = _consume_runtime_stream(runtime.run_stream(request))

    while interactive:
        approval_event = _pending_approval_event(final_session, last_event)
        if approval_event is None:
            break
        output, final_session, last_event = _consume_runtime_stream(
            _resume_stream_with_inline_approval(
                runtime,
                session_id=final_session.session.id,
                approval_request_id=_approval_request_id(approval_event),
                approval_decision=_prompt_for_approval(approval_event),
            )
        )

    if interactive:
        _print_runtime_output(output)

    return output


def _consume_runtime_stream(
    chunks: Iterator[RuntimeStreamChunk],
) -> tuple[str | None, SessionState, EventEnvelope | None]:
    output: str | None = None
    final_session: SessionState | None = None
    last_event: EventEnvelope | None = None

    for chunk in chunks:
        final_session = chunk.session
        if chunk.event is not None:
            print(
                _format_event(chunk.event.event_type, chunk.event.source, chunk.event.payload),
                flush=True,
            )
            last_event = chunk.event
        if chunk.kind == "output":
            output = chunk.output

    if final_session is None:
        raise ValueError("runtime stream emitted no chunks")

    return output, final_session, last_event


def _resume_stream_with_inline_approval(
    runtime: VoidCodeRuntime,
    *,
    session_id: str,
    approval_request_id: str,
    approval_decision: PermissionResolution,
) -> Iterator[RuntimeStreamChunk]:
    session_store = cast(_SessionStoreLike, getattr(runtime, _SESSION_STORE_ATTR))
    workspace = getattr(runtime, _WORKSPACE_ATTR)
    tool_registry = cast(_ToolRegistryLike, getattr(runtime, _TOOL_REGISTRY_ATTR))
    execute_graph_loop = cast(_ExecuteGraphLoopLike, getattr(runtime, _EXECUTE_GRAPH_LOOP_ATTR))
    persist_response = cast(_PersistResponseLike, getattr(runtime, _PERSIST_RESPONSE_ATTR))

    stored = session_store.load_session(workspace=workspace, session_id=session_id)
    pending = session_store.load_pending_approval(workspace=workspace, session_id=session_id)
    if pending is None:
        raise ValueError(f"no pending approval for session: {session_id}")
    if getattr(pending, _REQUEST_ID_ATTR) != approval_request_id:
        raise ValueError("approval request id does not match pending session approval")

    session = SessionState(
        session=stored.session.session,
        status="running",
        turn=stored.session.turn,
        metadata=stored.session.metadata,
    )

    sequence_before_turn = 1
    for event in reversed(stored.events):
        if event.event_type in ("runtime.tool_completed", "runtime.skills_loaded"):
            sequence_before_turn = event.sequence
            break

    max_stored_sequence = stored.events[-1].sequence if stored.events else 0

    tool_results: list[ToolResult] = []
    for event in stored.events:
        if event.event_type == "runtime.tool_completed":
            is_err = "error" in event.payload
            tool_results.append(
                ToolResult(
                    tool_name=str(event.payload.get("tool", "unknown")),
                    content=str(event.payload.get("content", "")) if not is_err else None,
                    status="error" if is_err else "ok",
                    data=event.payload,
                    error=str(event.payload["error"]) if is_err else None,
                )
            )

    graph_request = GraphRunRequest(
        session=session,
        prompt=_prompt_from_events(stored.events),
        available_tools=tool_registry.definitions(),
        metadata=session.metadata,
    )

    loop_events: list[EventEnvelope] = []
    output: str | None = None
    has_completed_tool = False

    try:
        for chunk in execute_graph_loop(
            session=session,
            sequence=sequence_before_turn,
            graph_request=graph_request,
            tool_results=tool_results,
            approval_resolution=(pending, approval_decision),
        ):
            if chunk.event is not None:
                if chunk.event.event_type == "runtime.tool_completed":
                    has_completed_tool = True
                if chunk.event.sequence > max_stored_sequence:
                    loop_events.append(chunk.event)
                    yield chunk
            if chunk.kind == "output":
                output = chunk.output
                yield chunk
            session = chunk.session
    except Exception:
        if session.status == "failed" and not has_completed_tool:
            response = RuntimeResponse(
                session=session,
                events=stored.events + tuple(loop_events),
                output=output,
            )
            request = RuntimeRequest(
                prompt=_prompt_from_events(stored.events), session_id=session_id
            )
            persist_response(request=request, response=response)
            return
        raise

    response = RuntimeResponse(
        session=session,
        events=stored.events + tuple(loop_events),
        output=output,
    )
    request = RuntimeRequest(prompt=_prompt_from_events(stored.events), session_id=session_id)
    persist_response(request=request, response=response)


def _prompt_from_events(events: tuple[EventEnvelope, ...]) -> str:
    if not events:
        return ""
    prompt = events[0].payload.get("prompt")
    if isinstance(prompt, str):
        return prompt
    return ""


def _pending_approval_event(
    session: SessionState,
    event: EventEnvelope | None,
) -> EventEnvelope | None:
    if session.status != "waiting":
        return None
    if event is None or event.event_type != "runtime.approval_requested":
        return None
    return event


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
        print(_format_event(event.event_type, event.source, event.payload), flush=True)

    if include_result:
        _print_runtime_output(typed_result.output)
    return len(typed_result.events)


def _print_runtime_output(output: str | None) -> None:
    print("RESULT", flush=True)
    print(output or "", end="", flush=True)
    if output and not output.endswith("\n"):
        print(flush=True)


def _handle_sessions_list_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    runtime = VoidCodeRuntime(workspace=workspace)
    sessions = runtime.list_sessions()

    for session in sessions:
        print(_format_session_summary(session))

    return 0


def _format_session_summary(session: StoredSessionSummary) -> str:
    return (
        f"SESSION id={session.session.id} status={session.status} "
        f"turn={session.turn} updated_at={session.updated_at} prompt={session.prompt!r}"
    )


def _handle_sessions_resume_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    approval_decision = cast(PermissionResolution | None, getattr(args, "approval_decision", None))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        result = runtime.resume(
            session_id,
            approval_request_id=cast(str | None, getattr(args, "approval_request_id", None)),
            approval_decision=approval_decision,
        )
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None

    _print_runtime_response(result)
    return 0


def _handle_serve_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    config = load_runtime_config(
        workspace,
        approval_mode=cast(PermissionDecision | None, getattr(args, "approval_mode", None)),
    )
    serve(
        workspace=workspace,
        host=cast(str, args.host),
        port=cast(int, args.port),
        config=config,
    )
    return 0


class EventLikeProtocol(Protocol):
    event_type: str
    source: str
    payload: dict[str, object]


class RuntimeResponseLike(Protocol):
    events: tuple[EventLikeProtocol, ...]
    output: str | None

    session: SessionState


class _SessionStoreLike(Protocol):
    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponse: ...

    def load_pending_approval(
        self, *, workspace: Path, session_id: str
    ) -> PendingApproval | None: ...


class _ToolRegistryLike(Protocol):
    def definitions(self) -> tuple[ToolDefinition, ...]: ...


class _ExecuteGraphLoopLike(Protocol):
    def __call__(
        self,
        *,
        session: SessionState,
        sequence: int,
        graph_request: GraphRunRequest,
        tool_results: list[ToolResult],
        approval_resolution: tuple[PendingApproval, PermissionResolution] | None = None,
    ) -> Iterator[RuntimeStreamChunk]: ...


class _PersistResponseLike(Protocol):
    def __call__(self, *, request: RuntimeRequest, response: RuntimeResponse) -> None: ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voidcode",
        description="Voidcode command-line interface.",
    )
    _ = parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run",
        help="Run the deterministic local read-only slice.",
    )
    _ = run_parser.add_argument(
        "request",
        help="Simple deterministic request such as 'read README.md'.",
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
    run_parser.set_defaults(handler=_handle_run_command)

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
    serve_parser.set_defaults(handler=_handle_serve_command)

    sessions_parser = subparsers.add_parser("sessions", help="Inspect persisted local sessions.")
    sessions_subparsers = sessions_parser.add_subparsers(dest="sessions_command")

    list_parser = sessions_subparsers.add_parser("list", help="List persisted sessions.")
    _ = list_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
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
        return 0
    return handler(args)
