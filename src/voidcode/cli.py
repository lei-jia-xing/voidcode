from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Protocol, cast

from . import __version__
from .doctor import (
    CapabilityDoctor,
    create_doctor_for_config,
    create_report,
    format_report,
    format_report_json,
)
from .provider.snapshot import resolved_provider_snapshot
from .runtime.config import load_runtime_config, serialize_provider_fallback_config
from .runtime.contracts import RuntimeRequest, RuntimeStreamChunk
from .runtime.events import EventEnvelope
from .runtime.permission import PermissionDecision, PermissionResolution
from .runtime.service import VoidCodeRuntime
from .runtime.session import SessionState, StoredSessionSummary
from .server import serve

Handler = Callable[[argparse.Namespace], int]


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
    try:
        request = RuntimeRequest(prompt=request_text, session_id=cast(str | None, args.session_id))
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
        output = _run_with_inline_approval(
            runtime,
            request,
            interactive=interactive,
        )

        if not interactive:
            _print_runtime_output(output)
    finally:
        _ = runtime.shutdown_lsp()
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
            runtime.resume_stream(
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
    try:
        sessions = runtime.list_sessions()
    finally:
        _ = runtime.shutdown_lsp()

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
        try:
            result = runtime.resume(
                session_id,
                approval_request_id=cast(str | None, getattr(args, "approval_request_id", None)),
                approval_decision=approval_decision,
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _ = runtime.shutdown_lsp()

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


def _handle_config_show_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    if not workspace.exists() or not workspace.is_dir():
        raise SystemExit(f"error: workspace does not exist: {workspace}")

    session_id = cast(str | None, getattr(args, "session_id", None))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            effective_config = runtime.effective_runtime_config(session_id=session_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _ = runtime.shutdown_lsp()

    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "session_id": session_id,
                "approval_mode": effective_config.approval_mode,
                "model": effective_config.model,
                "execution_engine": effective_config.execution_engine,
                "max_steps": effective_config.max_steps,
                "provider_fallback": serialize_provider_fallback_config(
                    getattr(effective_config, "provider_fallback", None)
                ),
                "resolved_provider": resolved_provider_snapshot(
                    getattr(effective_config, "resolved_provider", None)
                ),
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
            models = (
                runtime.refresh_provider_models(provider)
                if refresh
                else runtime.provider_models(provider)
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _ = runtime.shutdown_lsp()

    catalog = runtime.provider_model_catalog(provider)
    payload: dict[str, object] = {
        "workspace": str(workspace),
        "provider": provider,
        "refreshed": refresh,
        "models": list(models),
    }
    if catalog is not None:
        payload["source"] = catalog.get("source")
        payload["last_refresh_status"] = catalog.get("last_refresh_status")
        payload["last_error"] = catalog.get("last_error")
        if refresh and catalog.get("source") == "fallback":
            refresh_error = catalog.get("last_error")
            print(
                "WARN provider.models.refresh "
                f"provider={provider} source=fallback reason={refresh_error}",
                file=sys.stderr,
                flush=True,
            )

    print(json.dumps(payload))
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

    app = VoidCodeTUI(workspace=workspace, approval_mode=approval_mode)
    app.run()
    return 0


def _handle_doctor_command(args: argparse.Namespace) -> int:
    """Run the capability doctor to check external tool readiness."""
    workspace = cast(Path, args.workspace)
    verbose = cast(bool, args.verbose)
    json_output = cast(bool, args.json)

    # Load runtime config to get all capability settings
    try:
        config = load_runtime_config(workspace)
    except Exception:
        # If config loading fails, run with minimal checks
        doctor = CapabilityDoctor(workspace=workspace)
        # Always check ast-grep
        doctor.add_executable_check("ast-grep", "ast-grep")
        results = doctor.results
    else:
        # Create doctor with full config
        doctor = create_doctor_for_config(workspace, config)
        results = doctor.run_all_checks()

    # Create and format report
    report = create_report(results, workspace=workspace)

    if json_output:
        print(format_report_json(report))
    else:
        print(format_report(report, verbose=verbose))

    # Return 0 if healthy, 1 if there are issues
    return 0 if report.is_healthy else 1


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

    config_parser = subparsers.add_parser("config", help="Inspect effective runtime configuration.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")

    provider_parser = subparsers.add_parser("provider", help="Inspect provider metadata.")
    provider_subparsers = provider_parser.add_subparsers(dest="provider_command")

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
    config_show_parser.set_defaults(handler=_handle_config_show_command)

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
        return 0
    return handler(args)
