"""The VoidCode CLI: one kernel process, its plugins, and its clients.

``run/resume/show`` drive work, ``sessions/catalog/invoke/confirm/views/drain/usage`` are
client surfaces, ``tui`` attaches the terminal client, and ``serve`` serves the HTTP API and
the built web client.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .. import __version__
from ..sdk.json_values import json_wire_object, own_json_object
from .storage.store import SessionRecord, SqliteStore

if TYPE_CHECKING:
    from .engine import Kernel


def _parse_arguments(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"arguments must be a JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("arguments must be a JSON object")
    # No blacklist on business keys: the admitted command schema validates
    # arguments, and identity always comes from the kernel-owned context.
    return json_wire_object(own_json_object(payload))


def _parse_capabilities(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    items = [item.strip() for item in raw.split(",") if item.strip()]
    for item in items:
        if "\0" in item or len(item) > 128:
            raise ValueError("client capabilities must be non-empty strings within 128 characters")
    return frozenset(items)


_COMMANDS = frozenset(
    {"run", "resume", "show", "create-session", "sessions", "catalog", "invoke", "confirm", "views", "drain", "usage", "tui", "serve"}
)
_ROOT_VALUE_OPTIONS = frozenset({"--workspace", "--state-dir", "--trust-plugin"})


def _normalized_argv(argv: Sequence[str]) -> list[str]:
    """Accept root options before *or* after the subcommand, as the legacy CLI did.

    ``argparse`` only knows the root options before the subcommand, but
    ``voidcode serve --workspace .`` is what a user types; the root pairs found
    after the command are hoisted in front of it.
    """
    tokens = list(argv)
    command_at = next((index for index, token in enumerate(tokens) if token in _COMMANDS), None)
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
    parser = argparse.ArgumentParser(prog="python -m voidcode.kernel")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--trust-plugin", action="append", default=[], metavar="PLUGIN_ID")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("prompt")
    run.add_argument("--session-id")
    run.add_argument("--max-steps", type=int, default=32)
    resume = commands.add_parser("resume")
    resume.add_argument("session_id")
    resume.add_argument("--approval-request-id")
    resume.add_argument("--decision", choices=("allow", "deny"))
    resume.add_argument("--max-steps", type=int, default=32)
    show = commands.add_parser("show")
    show.add_argument("session_id")
    show.add_argument("--events", action="store_true")
    create = commands.add_parser("create-session")
    create.add_argument("--session-id")
    sessions = commands.add_parser("sessions")
    sessions.add_argument("--limit", type=int, default=100)
    commands.add_parser("catalog")
    invoke = commands.add_parser("invoke")
    invoke.add_argument("session_id")
    invoke.add_argument("name")
    invoke.add_argument("--arguments", default="{}")
    invoke.add_argument("--expected-revision", required=True, help="Capability revision the displayed catalog was captured under")
    invoke.add_argument("--client-capabilities", default="")
    confirm = commands.add_parser("confirm")
    confirm.add_argument("session_id")
    confirm.add_argument("request_id")
    confirm.add_argument("--decision", choices=("allow", "deny"), required=True)
    confirm.add_argument("--expected-revision", required=True, help="Capability revision the displayed catalog was captured under")
    confirm.add_argument("--client-capabilities", default="")
    views = commands.add_parser("views")
    views.add_argument("session_id")
    views.add_argument("--client-capabilities", default="")
    drain = commands.add_parser("drain")
    drain.add_argument("session_id")
    drain.add_argument("--limit", type=int, default=10)
    usage = commands.add_parser("usage")
    usage.add_argument("session_id")
    usage.add_argument("--since-sequence", type=int, default=0)
    usage.add_argument("--until-sequence", type=int, default=None)
    tui = commands.add_parser("tui")
    tui.add_argument("--server", help="Remote or local kernel HTTP server base URL, e.g. http://127.0.0.1:8000")
    tui.add_argument("--token", help="Bearer token for HTTP authentication")
    tui.add_argument("--session-id", help="Session ID to attach or create")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--bearer-token")
    #: The replaced `voidcode web` opened the browser; serving the bundle is now the same command.
    serve.add_argument("--open", dest="open_browser", action="store_true", help="Open the served web client in a browser")
    args = parser.parse_args(_normalized_argv(argv if argv is not None else sys.argv[1:]))
    try:
        workspace = args.workspace.resolve(strict=True)
        if args.command == "show":
            if args.state_dir is not None:
                directory = args.state_dir
            else:
                from .bootstrap import state_directory

                directory = state_directory(workspace)
            return _show(workspace, directory, args.session_id, events=args.events)
        # Pure ingress validation before opening the kernel: malformed client
        # input fails fast without touching config, host, or storage.
        if args.command == "invoke":
            args.parsed_arguments = _parse_arguments(args.arguments)
            args.parsed_capabilities = _parse_capabilities(args.client_capabilities)
        elif args.command in {"confirm", "views"}:
            args.parsed_capabilities = _parse_capabilities(args.client_capabilities)
        from .bootstrap import open_kernel, state_directory

        directory = args.state_dir if args.state_dir is not None else state_directory(workspace)
        if args.command == "tui":
            from .tui import run_tui

            if args.server:
                return run_tui(args.server, token=args.token, session_id=args.session_id)
            import secrets
            import socket
            import threading
            import time

            from .transport.server import serve_kernel

            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            token = secrets.token_urlsafe(16)
            server_thread = threading.Thread(
                target=serve_kernel,
                kwargs={
                    "workspace": workspace,
                    "state_dir": directory,
                    "trusted_plugins": frozenset(args.trust_plugin),
                    "host": "127.0.0.1",
                    "port": port,
                    "bearer_token": token,
                },
                daemon=True,
                name="EmbeddedKernelServer",
            )
            server_thread.start()
            time.sleep(0.3)
            return run_tui(f"http://127.0.0.1:{port}", token=token, session_id=args.session_id)
        if args.command == "serve":
            from .transport.server import serve_kernel

            if args.open_browser:
                import webbrowser

                webbrowser.open(f"http://{args.host}:{args.port}/")
            serve_kernel(
                workspace=workspace,
                state_dir=directory,
                trusted_plugins=frozenset(args.trust_plugin),
                host=args.host,
                port=args.port,
                bearer_token=args.bearer_token,
            )
            return 0
        with open_kernel(workspace, state_dir=directory, trusted_plugins=frozenset(args.trust_plugin)) as kernel:
            previous = {number: signal.signal(number, lambda *_: kernel.cancel("user signal")) for number in (signal.SIGINT, signal.SIGTERM)}
            try:
                return _dispatch(kernel, args)
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, allow_nan=False))
        return 1


def _show(workspace: Path, directory: Path, session_id: str, *, events: bool) -> int:
    """Rescue path: read durable history without config, host, or revision checks.

    Runs before any kernel bootstrap so broken configuration, untrusted or
    broken plugins, and revision drift cannot hide already-persisted history.
    Never creates a database: a missing file is an explicit failure.
    """
    database = directory / "kernel-v1.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("kernel database does not exist")
    with SqliteStore(database) as store:
        session = store.session(session_id)
        if session.workspace != str(workspace):
            raise ValueError("session belongs to another workspace")
        checkpoint = store.checkpoint(session_id)
        payload: dict[str, object] = {
            "session_id": session.session_id,
            "status": session.status,
            "run_id": session.run_id,
            "revision_id": session.active_revision_id,
            "turn": session.turn,
            "last_event_sequence": session.last_event_sequence,
            "checkpoint": None if checkpoint is None else {"kind": checkpoint.kind, "payload": json_wire_object(checkpoint.payload)},
        }
        if events:
            payload["events"] = [
                {
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "run_id": event.run_id,
                    "revision_id": event.revision_id,
                    "source": event.source,
                    "actor": event.actor,
                    "payload": json_wire_object(event.payload),
                }
                for event in store.events(session_id)
            ]
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0


def _dispatch(kernel: Kernel, args: argparse.Namespace) -> int:
    if args.command == "run":
        result = kernel.run(args.prompt, session_id=args.session_id, max_steps=args.max_steps)
        print(json.dumps(result.as_payload(), ensure_ascii=False, allow_nan=False))
        return 130 if result.status == "interrupted" else 1 if result.status == "failed" else 0
    if args.command == "resume":
        result = kernel.resume(
            args.session_id,
            approval_request_id=args.approval_request_id,
            decision=args.decision,
            max_steps=args.max_steps,
        )
        print(json.dumps(result.as_payload(), ensure_ascii=False, allow_nan=False))
        return 130 if result.status == "interrupted" else 1 if result.status == "failed" else 0
    if args.command == "create-session":
        record = kernel.create_session(args.session_id)
        print(json.dumps(_session_payload(record), ensure_ascii=False, allow_nan=False))
        return 0
    if args.command == "sessions":
        records = kernel.sessions(limit=args.limit)
        print(json.dumps({"sessions": [_session_payload(record) for record in records]}, ensure_ascii=False, allow_nan=False))
        return 0
    if args.command == "catalog":
        catalog = kernel.command_catalog()
        print(json.dumps(catalog.as_payload(), ensure_ascii=False, allow_nan=False))
        return 0
    if args.command == "invoke":
        result = kernel.invoke_command(
            args.session_id,
            args.name,
            args.parsed_arguments,
            expected_revision=args.expected_revision,
            client_capabilities=args.parsed_capabilities,
        )
        print(json.dumps(result.as_payload(), ensure_ascii=False, allow_nan=False))
        return 0 if result.status == "ok" else 2 if result.status == "pending_confirmation" else 1
    if args.command == "confirm":
        result = kernel.confirm_action(
            args.session_id,
            args.request_id,
            args.decision,
            expected_revision=args.expected_revision,
            client_capabilities=args.parsed_capabilities,
        )
        print(json.dumps(result.as_payload(), ensure_ascii=False, allow_nan=False))
        return 0 if result.status == "ok" else 2 if result.status == "pending_confirmation" else 1
    if args.command == "views":
        snapshot = kernel.plugin_views(args.session_id, client_capabilities=args.parsed_capabilities)
        print(json.dumps(snapshot.as_payload(), ensure_ascii=False, allow_nan=False))
        return 0
    if args.command == "drain":
        results = kernel.drain_continuations(args.session_id, limit=args.limit)
        print(json.dumps({"continuations": [item.as_payload() for item in results]}, ensure_ascii=False, allow_nan=False))
        return 0
    if args.command == "usage":
        report = kernel.usage(args.session_id, since_sequence=args.since_sequence, until_sequence=args.until_sequence)
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
