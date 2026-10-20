"""The VoidCode CLI: one kernel process, its plugins, and its clients.

``run/resume`` drive work and ``show`` reads durable session state without a kernel;
``create-session/sessions/catalog/invoke/confirm/views/drain/usage`` are client surfaces,
``tui`` attaches the terminal client, and ``serve`` serves the HTTP API and the built web
client.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .. import __version__
from ..sdk.json_values import json_wire_object, own_json_object
from .storage.store import SessionRecord

if TYPE_CHECKING:
    from .engine import Kernel
    from .images import Image


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
    {
        "run",
        "resume",
        "show",
        "preflight",
        "upgrade",
        "create-session",
        "sessions",
        "catalog",
        "invoke",
        "confirm",
        "views",
        "drain",
        "usage",
        "tui",
        "serve",
    }
)
_ROOT_VALUE_OPTIONS = frozenset({"--workspace", "--state-dir", "--trust-plugin"})

#: One-shot operational commands route into the elected image below (the supervisor owns its own
#: launch, and the control/rescue surfaces each use a different context, so they stay local).
_ROUTED_COMMANDS = frozenset({"run", "resume", "create-session", "sessions", "catalog", "invoke", "confirm", "views", "drain", "usage"})


def _normalized_argv(argv: Sequence[str]) -> list[str]:
    """Accept root options before *or* after the subcommand.

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
    #: Read-only candidate preflight for a controlled kernel upgrade: prints one JSON object and
    #: exits non-zero with a named reason instead of changing anything.
    commands.add_parser("preflight")
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
    serve.add_argument("--session-id", default=None, help="Acting session that owns a first-adoption transition, when the supervisor needs one")
    #: Supervised-child mode: the supervisor passes the listener and the launch identity, never a user.
    serve.add_argument("--listen-fd", type=int, default=None, help=argparse.SUPPRESS)
    serve.add_argument("--fenced", action="store_true", help=argparse.SUPPRESS)
    serve.add_argument("--control-token", default=None, help=argparse.SUPPRESS)
    serve.add_argument("--generation", type=int, default=1, help=argparse.SUPPRESS)
    serve.add_argument("--upgrade-id", default=None, help=argparse.SUPPRESS)
    serve.add_argument("--supervisor-pid", type=int, default=None, help=argparse.SUPPRESS)
    serve.add_argument("--owner-pid", type=int, default=None, help=argparse.SUPPRESS)
    upgrade = commands.add_parser("upgrade", help="Ask the running supervisor to upgrade this workspace's kernel")
    upgrade.add_argument("what", nargs="?", default="run", choices=("run", "status"))
    upgrade.add_argument("--candidate", default=None, help="Candidate installation directory (required unless status)")
    upgrade.add_argument("--session-id", default=None, help="Acting session that owns the transition, when the workspace has sessions")
    upgrade.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for the drain before refusing")
    normalized = _normalized_argv(argv if argv is not None else sys.argv[1:])
    args = parser.parse_args(normalized)
    try:
        workspace = args.workspace.resolve(strict=True)
        if args.command in {"show", "preflight"}:
            if args.state_dir is not None:
                directory = args.state_dir
            else:
                from .bootstrap import state_directory

                directory = state_directory(workspace)
            if args.command == "preflight":
                return _preflight(workspace, directory)
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
        _maybe_reexec_in_elected_image(args.command, workspace, directory, normalized)
        if args.command == "tui":
            from .tui import run_tui

            if args.server:
                return run_tui(args.server, token=args.token, session_id=args.session_id)
            return _embedded_tui(workspace, directory, trusted_plugins=frozenset(args.trust_plugin), session_id=args.session_id)
        if args.command == "upgrade":
            return _upgrade(directory, args)
        if args.command == "serve":
            from .transport.server import serve_kernel

            if args.listen_fd is not None:
                serve_kernel(
                    workspace=workspace,
                    state_dir=directory,
                    trusted_plugins=frozenset(args.trust_plugin),
                    host=args.host,
                    port=args.port,
                    bearer_token=args.bearer_token,
                    listen_fd=args.listen_fd,
                    fenced=args.fenced,
                    control_token=args.control_token,
                    generation=args.generation,
                    upgrade_id=args.upgrade_id,
                    supervisor_pid=args.supervisor_pid,
                    quiet=True,
                )
                return 0
            from .supervisor import run_supervisor

            return run_supervisor(
                workspace=workspace,
                state_dir=directory,
                host=args.host,
                port=args.port,
                bearer_token=args.bearer_token,
                trusted_plugins=frozenset(args.trust_plugin),
                session_id=args.session_id,
                open_browser=args.open_browser,
                owner_pid=args.owner_pid,
            )
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
    Never creates a database: a missing file is an explicit failure. Reads through
    the schema-tolerant reader, so a pre-transition database stays readable too.
    """
    from .storage.store import open_read_only

    database = directory / "kernel-v1.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("kernel database does not exist")
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


def _preflight(workspace: Path, directory: Path) -> int:
    """Candidate preflight for THIS installation against the workspace's live state.

    Runs before any kernel bootstrap: it reads the database, the pinned artifacts and the private
    snapshot without opening the configured plugins, never writes, and answers with one JSON object.
    """
    from ..sdk.manifest import SDK_PROTOCOL_VERSION
    from .activation import sdk_source_hash
    from .images import bundle_of, installed_identity, running_identity
    from .preflight import LAUNCH_PROTOCOL_VERSION, PreflightRefused, verify_preflight
    from .storage.store import SCHEMA_VERSION

    identity = running_identity() or installed_identity()
    try:
        report = verify_preflight(
            database=directory / "kernel-v1.sqlite3",
            workspace=str(workspace),
            state_dir=directory,
            candidate_package_root=identity.package_root,
            candidate_sdk_sha256=sdk_source_hash(identity.package_root / "sdk"),
            candidate_sdk_protocol=SDK_PROTOCOL_VERSION,
            candidate_launch_protocol=LAUNCH_PROTOCOL_VERSION,
            image_digest=identity.digest,
            web_bundle=bundle_of(identity.package_root),
            target_schema=SCHEMA_VERSION,
        )
    except PreflightRefused as exc:
        print(json.dumps({"ok": False, "reason": exc.reason, "detail": exc.detail}, ensure_ascii=False, allow_nan=False))
        return 1
    print(json.dumps(report.as_payload(), ensure_ascii=False, allow_nan=False))
    return 0


def _elect_image_for_command(command: str, workspace: Path, directory: Path) -> Image | None:
    """The image one one-shot command must execute in, or ``None`` to run in this process.

    Normal operational commands always run inside an image: a recorded election routes into the
    elected implementation, and a state that adopts for the first time snapshots this installation
    and runs that image, so the first writer always executes recoverable bytes. Returns the missing
    image as an error instead of running the wrong one.
    """
    if command not in _ROUTED_COMMANDS:
        return None
    from .images import IMAGE_DIGEST_ENV, IMAGES_DIR_NAME, ensure_running_image, image_for_digest, installed_identity

    if os.environ.get(IMAGE_DIGEST_ENV) is not None:
        return None  # already pinned by the launcher that started this process
    from .preflight import PreflightRefused, read_connection, read_workspace_revision

    database = directory / "kernel-v1.sqlite3"
    if not database.is_file():
        return ensure_running_image(directory / IMAGES_DIR_NAME)
    try:
        connection = read_connection(database)
        try:
            _, payload = read_workspace_revision(connection, str(workspace))
        finally:
            connection.close()
    except PreflightRefused as exc:
        if exc.reason == "no_adopted_revision":
            return ensure_running_image(directory / IMAGES_DIR_NAME)
        return None  # foreign state keeps today's local failure behavior
    image = payload.get("image")
    if not isinstance(image, Mapping) or not isinstance(image.get("digest"), str) or not image["digest"]:
        return None  # a pre-image generation names no election
    if installed_identity().digest == image["digest"]:
        return None
    return image_for_digest(directory / IMAGES_DIR_NAME, image["digest"])


def _maybe_reexec_in_elected_image(command: str, workspace: Path, directory: Path, normalized: Sequence[str]) -> None:
    """Replace this process with the elected image's copy of the same command, when one is elected.

    The working directory is inherited, the import root is replaced, and the probe that got us here
    is the same digest the elected image recomputes, so the re-executed process finds no second
    election. ``serve``/``tui`` own their own supervised launch instead of this route.
    """
    from .images import launch_argv, launch_environment

    elected = _elect_image_for_command(command, workspace, directory)
    if elected is None:
        return
    child = launch_argv(*normalized)
    os.execvpe(child[0], child, launch_environment(elected))  # noqa: S606 - the elected image, verified by digest


SUPERVISOR_SOCKET_NAME = "supervisor.sock"


def _supervisor_socket(directory: Path) -> Path:
    return directory / SUPERVISOR_SOCKET_NAME


def _supervisor_request(directory: Path, payload: Mapping[str, object], *, timeout_seconds: float = 300.0) -> dict[str, object]:
    """One newline-JSON round trip with the supervisor that owns this state directory."""
    import socket

    sock = directory / SUPERVISOR_SOCKET_NAME
    if not sock.exists():
        raise FileNotFoundError(f"no supervisor is running for this state directory ({directory})")
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout_seconds)
        client.connect(str(sock))
        client.sendall(body)
        received = b""
        while not received.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            received += chunk
            if len(received) > 1024 * 1024:
                raise ValueError("supervisor response exceeds 1 MiB")
    try:
        response = json.loads(received.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"supervisor answered malformed JSON: {exc}") from exc
    if not isinstance(response, dict):
        raise ValueError("supervisor answer is not an object")
    return response


def _upgrade(directory: Path, args: argparse.Namespace) -> int:
    """Ask the running supervisor about an upgrade: its status, or a candidate handoff."""
    if args.what == "status":
        response = _supervisor_request(directory, {"op": "status"})
        print(json.dumps(response, ensure_ascii=False, allow_nan=False))
        return 0
    if not args.candidate:
        print(json.dumps({"status": "failed", "error": "upgrade needs --candidate PATH (or use 'upgrade status')"}))
        return 1
    candidate = Path(args.candidate)
    if not candidate.is_dir():
        print(json.dumps({"status": "failed", "error": f"candidate is not a directory: {candidate}"}))
        return 1
    response = _supervisor_request(
        directory,
        {"op": "upgrade", "candidate": str(candidate.resolve(strict=True)), "session_id": args.session_id, "timeout_seconds": args.timeout},
        timeout_seconds=max(1.0, float(args.timeout) + 60.0),
    )
    print(json.dumps(response, ensure_ascii=False, allow_nan=False))
    return 0 if response.get("status") == "upgraded" else 1


def _embedded_tui(workspace: Path, directory: Path, *, trusted_plugins: frozenset[str], session_id: str | None) -> int:
    """Run the TUI against a supervisor child process, so a kernel replacement never kills the UI."""
    import queue
    import subprocess
    import urllib.request

    from .images import launch_environment_for_root, running_package_root
    from .tui import run_tui

    package_root = running_package_root()
    # Every value travels joined to its flag: a value that starts with a dash (a token, a
    # hostile path) can never be read as another flag.
    command = [
        sys.executable,
        "-P",
        "-m",
        "voidcode.kernel",
        "serve",
        f"--workspace={workspace}",
        f"--state-dir={directory}",
        "--host=127.0.0.1",
        "--port=0",
        f"--owner-pid={os.getpid()}",
    ]
    if session_id is not None:
        command.append(f"--session-id={session_id}")
    for plugin_id in sorted(trusted_plugins):
        command.append(f"--trust-plugin={plugin_id}")
    # The supervisor is spawned with the same pinned launch as a kernel child: safe path mode
    # plus an import root that names this installation, so a workspace or cwd package cannot
    # hijack it. The working directory is inherited untouched; it is never on the import path.
    child = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
        env=launch_environment_for_root(package_root.parent),
    )
    assert child.stdout is not None
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert child.stdout is not None
        try:
            for line in child.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=pump, name="EmbeddedSupervisorReader", daemon=True)
    reader.start()

    def next_line(deadline: float) -> str | None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return lines.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                continue

    def stop_child() -> None:
        try:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=15)
            reader.join(timeout=10)
        finally:
            if child.stdout is not None:
                child.stdout.close()

    try:
        endpoint: str | None = None
        token: str | None = None
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline and (endpoint is None or token is None):
            line = next_line(deadline)
            if line is None:
                break
            text = line.strip()
            if text.startswith("kernel endpoint: "):
                endpoint = text.partition(": ")[2].strip()
            elif text.startswith("kernel bearer token: "):
                token = text.partition(": ")[2].strip()
        if endpoint is None or token is None:
            raise RuntimeError(f"embedded supervisor did not report its endpoint (exit status {child.poll()})")
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"embedded supervisor exited early with status {child.returncode}")
            try:
                request = urllib.request.Request(f"{endpoint}/api/health", headers={"Authorization": f"Bearer {token}"}, method="GET")
                with urllib.request.urlopen(request, timeout=5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if isinstance(health, dict) and health.get("stage") == "serving":
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("embedded supervisor did not reach serving state in time")
        return run_tui(endpoint, token=token, session_id=session_id)
    finally:
        stop_child()


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
