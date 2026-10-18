"""Development entry point; the production entry cuts over after plugin migration."""

from __future__ import annotations

import argparse
import json
import signal
from collections.abc import Sequence
from pathlib import Path

from ..sdk.json_values import json_wire_object
from .bootstrap import open_kernel, state_directory
from .storage.store import SqliteStore


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m voidcode.kernel")
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
    args = parser.parse_args(argv)
    try:
        workspace = args.workspace.resolve(strict=True)
        directory = args.state_dir if args.state_dir is not None else state_directory(workspace)
        if args.command == "show":
            database = directory / "kernel-v1.sqlite3"
            if not database.is_file():
                raise FileNotFoundError("kernel database does not exist")
            with SqliteStore(database) as store:
                session = store.session(args.session_id)
                if session.workspace != str(workspace):
                    raise ValueError("session belongs to another workspace")
                checkpoint = store.checkpoint(args.session_id)
                payload = {
                    "session_id": session.session_id,
                    "status": session.status,
                    "run_id": session.run_id,
                    "revision_id": session.active_revision_id,
                    "turn": session.turn,
                    "last_event_sequence": session.last_event_sequence,
                    "checkpoint": None if checkpoint is None else {"kind": checkpoint.kind, "payload": json_wire_object(checkpoint.payload)},
                }
                if args.events:
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
                        for event in store.events(args.session_id)
                    ]
            print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
            return 0
        with open_kernel(workspace, state_dir=directory, trusted_plugins=frozenset(args.trust_plugin)) as kernel:
            previous = {number: signal.signal(number, lambda *_: kernel.cancel("user signal")) for number in (signal.SIGINT, signal.SIGTERM)}
            try:
                if args.command == "run":
                    result = kernel.run(args.prompt, session_id=args.session_id, max_steps=args.max_steps)
                else:
                    result = kernel.resume(
                        args.session_id,
                        approval_request_id=args.approval_request_id,
                        decision=args.decision,
                        max_steps=args.max_steps,
                    )
                print(json.dumps(result.as_payload(), ensure_ascii=False, allow_nan=False))
                return 130 if result.status == "interrupted" else 1 if result.status == "failed" else 0
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, allow_nan=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
