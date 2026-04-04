from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, cast

from . import __version__
from .runtime.contracts import RuntimeRequest
from .runtime.service import VoidCodeRuntime
from .runtime.session import StoredSessionSummary

Handler = Callable[[argparse.Namespace], int]


def _format_event(event_type: str, source: str, data: dict[str, object]) -> str:
    suffix = " ".join(f"{key}={value}" for key, value in sorted(data.items()))
    if suffix:
        return f"EVENT {event_type} source={source} {suffix}"
    return f"EVENT {event_type} source={source}"


def _handle_run_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    request_text = cast(str, args.request)
    runtime = VoidCodeRuntime(workspace=workspace)
    result = runtime.run(
        RuntimeRequest(prompt=request_text, session_id=cast(str | None, args.session_id))
    )

    _print_runtime_response(result)
    return 0


def _print_runtime_response(result: object) -> None:
    typed_result = cast("RuntimeResponseLike", result)

    for event in typed_result.events:
        print(_format_event(event.event_type, event.source, event.payload))

    print("RESULT")
    print(typed_result.output or "", end="")
    if typed_result.output and not typed_result.output.endswith("\n"):
        print()


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
    runtime = VoidCodeRuntime(workspace=workspace)
    result = runtime.resume(session_id)

    _print_runtime_response(result)
    return 0


class EventLikeProtocol(Protocol):
    event_type: str
    source: str
    payload: dict[str, object]


class RuntimeResponseLike(Protocol):
    events: tuple[EventLikeProtocol, ...]
    output: str | None

    session: object


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
    run_parser.set_defaults(handler=_handle_run_command)

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
    resume_parser.set_defaults(handler=_handle_sessions_resume_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast(Handler | None, getattr(args, "handler", None))
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
