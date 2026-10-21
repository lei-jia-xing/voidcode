"""Runnable-example consumer behavior: the tally package over a live kernel.

Covers the tally example's real tool/command/state behavior over a live kernel
with the loopback provider.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.storage.store import SqliteStore

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tally-counter"


def _copy_example(root: Path) -> Path:
    target = root / "tally-counter"
    shutil.copytree(_EXAMPLE, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def _configure(workspace: Path, server: OpenAILoopbackServer, plugin: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    # Isolated author path: disable every bundled plugin the example does not
    # need (existing `enabled: false` contract, no default changes), so the
    # consumer test never depends on unrelated bundled identities. The
    # normal-setup smoke (full default bundle + example) runs separately once
    # the tree's bundled generation is stable.
    disabled = {
        name: {"enabled": False}
        for name in (
            "agents",
            "ast_grep",
            "btw",
            "compaction",
            "cron",
            "delegation",
            "effectiveness",
            "exec",
            "files",
            "formatter",
            "goal",
            "hooks",
            "lsp",
            "mcp",
            "plan",
            "review",
            "session_info",
            "skills",
            "todo",
            "user_shell",
            "web",
        )
    }
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": "ask"},
        "providers": {"endpoint": {"api_key": "loopback-test-key", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {**disabled, "tally-counter": {"path": str(plugin)}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


@contextmanager
def _provider(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_example_command_and_tool_share_durable_session_state(tmp_path: Path) -> None:
    plugin = _copy_example(tmp_path)
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "reasoning_content": "loopback reasoning",
                "tool_calls": [_call("tally-1", "tally_add", delta=5)],
            },
            finish="tool_calls",
        ),
        openai_response(
            message={"role": "assistant", "content": "tally updated", "reasoning_content": "loopback reasoning"},
            finish="stop",
        ),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, plugin)
        with closing(open_kernel(workspace, state_dir=state)) as kernel:
            kernel.create_session("tally-session")
            revision = kernel.command_catalog().revision_id

            # JSON Schema `integer` also accepts integral floats on the wire;
            # the committed state must still be a true integer the next
            # invocation can read back.
            first = kernel.invoke_command("tally-session", "tally", {"delta": 3.0}, expected_revision=revision)
            assert first.status == "ok", first.as_payload()
            assert first.data == {"total": 3, "updates": 1}
            assert type(first.data["total"]) is int

            run = kernel.run("add five to the tally", session_id="tally-session")
            assert run.status == "completed", run.as_payload()
            assert len(server.requests) == 2, server.requests

            with SqliteStore(state / "kernel-v1.sqlite3") as store:
                stored, _version = store.plugin_state("tally-counter", "session", "tally-session")
                assert stored == {"schema_version": 1, "total": 8, "updates": 2}, stored
                assert type(stored["total"]) is int
                completions = [event for event in store.events("tally-session") if event.event_type == "kernel.tool_completed"]
                assert completions and completions[0].payload["result"]["status"] == "ok"
