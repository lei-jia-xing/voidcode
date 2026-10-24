"""The VoidCode CLI: one core process, its plugins, and its clients.

``run/resume`` drive work and ``show`` reads durable session state without a core;
``create-session/sessions/catalog/invoke/confirm/views/drain/usage`` are client surfaces,
bare ``voidcode`` attaches the terminal client, and ``serve`` serves the HTTP API and the built web
client.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .. import __version__
from ..sdk.json_values import json_wire_object, own_json_object
from .storage.store import SessionRecord

if TYPE_CHECKING:
    from .engine import Core


def _parse_arguments(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"arguments must be a JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("arguments must be a JSON object")
    # No blacklist on business keys: the admitted command schema validates
    # arguments, and identity always comes from the core-owned context.
    return json_wire_object(own_json_object(payload))


_COMMANDS = frozenset(
    {
        "run",
        "resume",
        "show",
        "upgrade",
        "create-session",
        "sessions",
        "catalog",
        "invoke",
        "confirm",
        "views",
        "drain",
        "usage",
        "serve",
    }
)
_ROOT_VALUE_OPTIONS = frozenset({"--workspace", "--state-dir"})
#: Every value option the command scan skips, so a session named e.g. ``run`` never reads as a subcommand.
_COMMAND_SCAN_VALUE_OPTIONS = _ROOT_VALUE_OPTIONS | frozenset({"--server", "--token", "--session-id"})


def _normalized_argv(argv: Sequence[str]) -> list[str]:
    """Accept root options before *or* after the subcommand.

    ``argparse`` only knows the root options before the subcommand, but
    ``voidcode serve --workspace .`` is what a user types; the root pairs found
    after the command are hoisted in front of it. The command scan skips the
    separate value of every root value option, so a session named e.g. ``run``
    never reads as a subcommand; only the durable root options are hoisted, never
    a command's own ``--session-id``.
    """
    tokens = list(argv)
    command_at: int | None = None
    scan = 0
    while scan < len(tokens):
        name, separator, _inline = tokens[scan].partition("=")
        if name in _COMMAND_SCAN_VALUE_OPTIONS and not separator:
            scan += 2  # the option and its separate value are never the command
            continue
        if tokens[scan] in _COMMANDS:
            command_at = scan
            break
        scan += 1
    if command_at is None:
        return tokens
    head, rest = tokens[:command_at], tokens[command_at:]
    tail: list[str] = []
    index = 1  # rest[0] is the command itself
    while index < len(rest):
        name, separator, _inline = rest[index].partition("=")
        if name in _ROOT_VALUE_OPTIONS:
            if separator or index + 1 >= len(rest):
                head.append(rest[index])
            else:
                head.extend((rest[index], rest[index + 1]))
                index += 1
        else:
            tail.append(rest[index])
        index += 1
    return [*head, rest[0], *tail]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="voidcode",
        usage="voidcode [OPTIONS] [COMMAND]",
        description="VoidCode: one CLI for every operation.\nRun without a command to open the terminal UI.",
        epilog="Examples:\n"
        "  voidcode\n"
        "  voidcode --server http://127.0.0.1:8000 \\\n"
        "      --token TOKEN --session-id ID\n"
        '  voidcode run "read README.md"\n'
        "  voidcode show SESSION --events\n"
        "  voidcode serve --port 8000",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        suggest_on_error=True,
    )
    client = parser.add_argument_group("terminal client (no command)")
    client.add_argument("--server", metavar="URL", default=None, help="Attach to a running core instead of starting one")
    client.add_argument("--token", metavar="TOKEN", default=None, help="Bearer token for --server")
    #: The root `--session-id` keeps its own destination so it can never overwrite a command's own `--session-id`.
    client.add_argument(
        "--session-id", dest="tui_session_id", metavar="ID", default=None, help="Attach to an existing session (default: latest, or a new one)"
    )
    roots = parser.add_argument_group("workspace options")
    roots.add_argument(
        "--workspace", type=Path, default=Path.cwd(), metavar="PATH", help="Workspace directory holding voidcode.json (default: current directory)"
    )
    roots.add_argument("--state-dir", type=Path, default=None, metavar="PATH", help="State directory (default: derived from the workspace)")
    roots.add_argument("--version", action="version", version=__version__, help="Print the version and exit")
    commands = parser.add_subparsers(prog="voidcode", dest="command", metavar="COMMAND", title="commands")
    agents = commands.add_parser(
        "run",
        help="Run one prompt and print one JSON result",
        description="Run one agent turn and print the result as JSON.",
        epilog='Example:\n  voidcode run "read README.md" --session-id ID',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    agents.add_argument("prompt", metavar="PROMPT", help="Prompt to run")
    agents.add_argument("--session-id", metavar="ID", default=None, help="Session to continue, or create when absent")
    agents.add_argument("--max-steps", type=int, default=32, metavar="N", help="Maximum agent steps (default: 32)")
    resume = commands.add_parser(
        "resume",
        help="Resume a session, optionally deciding a parked approval",
        description="Resume a session, optionally deciding one parked native tool approval.",
    )
    resume.add_argument("session_id", metavar="SESSION", help="Session to resume")
    resume.add_argument("--approval-request-id", metavar="ID", default=None, help="Parked approval to decide")
    resume.add_argument("--decision", choices=("allow", "deny"), default=None, help="Decision for the parked approval")
    resume.add_argument("--max-steps", type=int, default=32, metavar="N", help="Maximum agent steps (default: 32)")
    create = commands.add_parser(
        "create-session", help="Create a session and print it as JSON", description="Create an empty session and print it as JSON."
    )
    create.add_argument("--session-id", metavar="ID", default=None, help="Session id, generated when absent")
    sessions = commands.add_parser("sessions", help="List sessions, newest first", description="List this workspace's sessions, newest first.")
    sessions.add_argument("--limit", type=int, default=100, metavar="N", help="Maximum sessions to list (default: 100)")
    show = commands.add_parser(
        "show",
        help="Read durable session history without starting a core",
        description="Read durable session history straight from SQLite: no config, runtime, or revision checks.",
    )
    show.add_argument("session_id", metavar="SESSION", help="Session to read")
    show.add_argument("--events", action="store_true", help="Include the persisted event history")
    usage = commands.add_parser(
        "usage", help="Print the session usage report as JSON", description="Print the truthful session token usage report as JSON."
    )
    usage.add_argument("session_id", metavar="SESSION", help="Session to report on")
    usage.add_argument("--since-sequence", type=int, default=0, metavar="SEQ", help="First event sequence to include (default: 0)")
    usage.add_argument("--until-sequence", type=int, default=None, metavar="SEQ", help="Last event sequence to include")
    commands.add_parser(
        "catalog", help="Print the admitted command catalog as JSON", description="Print the admitted command catalog and its revision as JSON."
    )
    invoke = commands.add_parser(
        "invoke",
        help="Invoke an admitted command and print one JSON result",
        description="Invoke one admitted command and print the result\nas JSON.",
        epilog="Example:\n  voidcode invoke SESSION status \\\n      --expected-revision ID",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    invoke.add_argument("session_id", metavar="SESSION", help="Session that owns the invocation")
    invoke.add_argument("name", metavar="NAME", help="Command name from the catalog")
    invoke.add_argument("--arguments", default="{}", metavar="JSON", help="Command arguments as a JSON object")
    invoke.add_argument("--expected-revision", required=True, metavar="ID", help="Capability revision the displayed catalog was captured under")
    confirm = commands.add_parser(
        "confirm",
        help="Decide a pending confirmation and print one JSON result",
        description="Decide one stored plugin confirmation and print the result as JSON.",
    )
    confirm.add_argument("session_id", metavar="SESSION", help="Session that owns the request")
    confirm.add_argument("request_id", metavar="REQUEST", help="Pending request id")
    confirm.add_argument("--decision", choices=("allow", "deny"), required=True, help="Decision for the pending request")
    confirm.add_argument("--expected-revision", required=True, metavar="ID", help="Capability revision the displayed catalog was captured under")
    views = commands.add_parser(
        "views", help="Print this session's plugin views as JSON", description="Print this session's rendered plugin views as JSON."
    )
    views.add_argument("session_id", metavar="SESSION", help="Session to inspect")
    drain = commands.add_parser(
        "drain",
        help="Execute pending continuations and print results as JSON",
        description="Execute pending durable continuations and print their results as JSON.",
    )
    drain.add_argument("session_id", metavar="SESSION", help="Session to drain")
    drain.add_argument("--limit", type=int, default=10, metavar="N", help="Maximum continuations (default: 10)")
    serve = commands.add_parser(
        "serve",
        help="Run HTTP API plus the web client",
        description="Run the VoidCode HTTP API and serve the web client.",
        epilog="Example:\n  voidcode serve --port 8000",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    serve.add_argument("--host", default="127.0.0.1", metavar="HOST", help="Loopback bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, metavar="PORT", help="Port, 0 for an ephemeral one (default: 8000)")
    serve.add_argument("--bearer-token", default=None, metavar="TOKEN", help="API bearer token, generated when absent")
    serve.add_argument("--open", dest="open_browser", action="store_true", help="Open the served web client in a browser")
    upgrade = commands.add_parser(
        "upgrade",
        help="Upgrade guidance (VoidCode is single-process; restart to upgrade)",
        description="VoidCode is a local single-process runtime. When code or configuration updates, simply restart the process.",
    )
    upgrade.add_argument("what", nargs="*", help=argparse.SUPPRESS)
    upgrade.add_argument("--candidate", default=None, help=argparse.SUPPRESS)
    upgrade.add_argument("--session-id", default=None, help=argparse.SUPPRESS)
    upgrade.add_argument("--timeout", type=float, default=300.0, help=argparse.SUPPRESS)
    normalized = _normalized_argv(argv if argv is not None else sys.argv[1:])
    args = parser.parse_args(normalized)
    if args.command is not None and (args.server is not None or args.token is not None or args.tui_session_id is not None):
        parser.error("--server/--token/--session-id are bare-client options and cannot be combined with a subcommand")
    try:
        workspace = args.workspace.resolve(strict=True)
        if args.command is None:
            from .bootstrap import state_directory
            from .tui import run_tui

            bare_directory = args.state_dir if args.state_dir is not None else state_directory(workspace)
            if args.server is not None:
                return run_tui(args.server, token=args.token, session_id=args.tui_session_id)
            return _embedded_tui(workspace, bare_directory, session_id=args.tui_session_id)
        if args.command == "show":
            if args.state_dir is not None:
                directory = args.state_dir
            else:
                from .bootstrap import state_directory

                directory = state_directory(workspace)
            return _show(workspace, directory, args.session_id, events=args.events)
        # Pure ingress validation before opening the core: malformed client
        # input fails fast without touching config, runtime, or storage.
        if args.command == "invoke":
            args.parsed_arguments = _parse_arguments(args.arguments)
        from .bootstrap import open_core, state_directory

        directory = args.state_dir if args.state_dir is not None else state_directory(workspace)
        if args.command == "upgrade":
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "message": "VoidCode is a local single-process runtime. To upgrade, update your installation and restart the process.",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "serve":
            from .transport.server import serve_core

            serve_core(
                workspace=workspace,
                state_dir=directory,
                host=args.host,
                port=args.port,
                bearer_token=args.bearer_token,
                open_browser=args.open_browser,
            )
            return 0
        with open_core(workspace, state_dir=directory) as core:
            previous = {number: signal.signal(number, lambda *_: core.cancel("user signal")) for number in (signal.SIGINT, signal.SIGTERM)}
            try:
                return _dispatch(core, args)
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, allow_nan=False))
        return 1


def _show(workspace: Path, directory: Path, session_id: str, *, events: bool) -> int:
    """Rescue path: read durable history without config, runtime, or revision checks.

    Runs before any core bootstrap so broken configuration, untrusted or
    broken plugins, and revision drift cannot hide already-persisted history.
    Never creates a database: a missing file is an explicit failure. Reads through
    the schema-tolerant reader, so a pre-transition database stays readable too.
    """
    from .storage.store import open_read_only

    database = directory / "kernel-v1.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("core database does not exist")
    connection = open_read_only(database)
    try:
        row = connection.execute(
            """
            SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence
            FROM sessions
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown session: {session_id}")
        if str(row["workspace"]) != str(workspace):
            raise ValueError("session belongs to another workspace")
        checkpoint_row = connection.execute(
            "SELECT kind, payload_json FROM checkpoints WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if checkpoint_row is None:
            checkpoint = None
        else:
            checkpoint_payload = json.loads(str(checkpoint_row["payload_json"]))
            if not isinstance(checkpoint_payload, dict):
                raise ValueError("checkpoint payload is not an object")
            checkpoint = {"kind": str(checkpoint_row["kind"]), "payload": json_wire_object(checkpoint_payload)}
        payload: dict[str, object] = {
            "session_id": str(row["session_id"]),
            "status": str(row["status"]),
            "run_id": None if row["run_id"] is None else str(row["run_id"]),
            "revision_id": str(row["active_revision_id"]),
            "turn": int(row["turn"]),
            "last_event_sequence": int(row["last_event_sequence"]),
            "checkpoint": checkpoint,
        }
        if events:
            payload["events"] = [
                {
                    "sequence": int(event_row["sequence"]),
                    "type": str(event_row["event_type"]),
                    "run_id": str(event_row["run_id"]),
                    "revision_id": str(event_row["revision_id"]),
                    "source": str(event_row["source"]),
                    "actor": str(event_row["actor"]),
                    "payload": json_wire_object(json.loads(str(event_row["payload_json"]))),
                }
                for event_row in connection.execute(
                    """
                    SELECT sequence, event_type, run_id, revision_id, source, actor, payload_json
                    FROM session_events
                    WHERE session_id = ?
                    ORDER BY sequence ASC
                    """,
                    (session_id,),
                ).fetchall()
            ]
    finally:
        connection.close()
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0


def _embedded_tui(workspace: Path, directory: Path, *, session_id: str | None) -> int:
    """Run the TUI directly against an in-process local transport server."""
    import socket

    import uvicorn

    from .transport.http import create_core_app
    from .transport.server import generate_bearer_token
    from .tui import run_tui

    token = generate_bearer_token()
    app = create_core_app(
        workspace=workspace,
        state_dir=directory,
        bearer_token=token,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    config = uvicorn.Config(app, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.01)

    try:
        return run_tui(f"http://127.0.0.1:{port}", token=token, session_id=session_id)
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)


def _dispatch(core: Core, args: argparse.Namespace) -> int:
    if args.command == "run":
        result = core.run(args.prompt, session_id=args.session_id, max_steps=args.max_steps)
        print(json.dumps(result.as_payload(), ensure_ascii=False, allow_nan=False))
        return 130 if result.status == "interrupted" else 1 if result.status == "failed" else 0
    if args.command == "resume":
        result = core.resume(
            args.session_id,
            approval_request_id=args.approval_request_id,
            decision=args.decision,
            max_steps=args.max_steps,
        )
        print(json.dumps(result.as_payload(), ensure_ascii=False, allow_nan=False))
        return 130 if result.status == "interrupted" else 1 if result.status == "failed" else 0
    if args.command == "create-session":
        record = core.create_session(args.session_id)
        print(json.dumps(_session_payload(record), ensure_ascii=False, allow_nan=False))
        return 0
    if args.command == "sessions":
        records = core.sessions(limit=args.limit)
        print(json.dumps({"sessions": [_session_payload(record) for record in records]}, ensure_ascii=False, allow_nan=False))
        return 0
    if args.command == "catalog":
        catalog = core.command_catalog()
        print(json.dumps(catalog.as_payload(), ensure_ascii=False, allow_nan=False))
        return 0
    if args.command == "invoke":
        result = core.invoke_command(
            args.session_id,
            args.name,
            args.parsed_arguments,
            expected_revision=args.expected_revision,
        )
        print(json.dumps(result.as_payload(), ensure_ascii=False, allow_nan=False))
        return 0 if result.status == "ok" else 2 if result.status == "pending_confirmation" else 1
    if args.command == "confirm":
        result = core.confirm_action(
            args.session_id,
            args.request_id,
            args.decision,
            expected_revision=args.expected_revision,
        )
        print(json.dumps(result.as_payload(), ensure_ascii=False, allow_nan=False))
        return 0 if result.status == "ok" else 2 if result.status == "pending_confirmation" else 1
    if args.command == "views":
        snapshot = core.plugin_views(args.session_id)
        print(json.dumps(snapshot.as_payload(), ensure_ascii=False, allow_nan=False))
        return 0
    if args.command == "drain":
        results = core.drain_continuations(args.session_id, limit=args.limit)
        print(json.dumps({"continuations": [item.as_payload() for item in results]}, ensure_ascii=False, allow_nan=False))
        return 0
    if args.command == "usage":
        report = core.usage(args.session_id, since_sequence=args.since_sequence, until_sequence=args.until_sequence)
        print(json.dumps(report.as_payload(), ensure_ascii=False, allow_nan=False))
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def _session_payload(record: SessionRecord) -> dict[str, object]:
    return {
        "session_id": record.session_id,
        "workspace": record.workspace,
        "status": record.status,
        "active_revision_id": record.active_revision_id,
        "run_id": record.run_id,
        "turn": record.turn,
        "last_event_sequence": record.last_event_sequence,
    }


if __name__ == "__main__":
    raise SystemExit(main())
