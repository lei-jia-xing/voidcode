"""External identity generation + runnable-example consumer behavior (P6).

Covers the exact authoring seams, not hash snapshots: pinning an unpinned
external manifest, proving an edited source becomes a new admitted identity,
--check refusing stale state without writing, escaped-resource rejection, and
the runnable tally example's real admission/tool/command/view behavior over a
live kernel with the loopback provider.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.storage.store import SqliteStore
from voidcode.sdk.manifest import parse_plugin_manifest

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tally-counter"
_GENERATOR = [sys.executable, "scripts/generate_plugin_identities.py"]


def _generate(*arguments: str, code: int = 0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run([*_GENERATOR, *arguments], capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == code, completed.stdout + completed.stderr
    return completed


def _copy_example(root: Path) -> Path:
    target = root / "tally-counter"
    shutil.copytree(_EXAMPLE, target)
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


def test_external_unpinned_manifest_pins_then_admits_without_hand_edits(tmp_path: Path) -> None:
    plugin = _copy_example(tmp_path)
    manifest_path = plugin / "voidcode-plugin.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"] = []
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    _generate("--plugin-dir", str(plugin), "--check", code=1)
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["files"] == []
    _generate("--plugin-dir", str(plugin))
    _generate("--plugin-dir", str(plugin), "--check")

    parsed = parse_plugin_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert [item.path for item in parsed.files] == ["tally_plugin.py"]
    assert parsed.files[0].sha256 == hashlib.sha256((plugin / "tally_plugin.py").read_bytes()).hexdigest()


def test_source_edit_is_a_new_identity_never_an_in_place_mutation(tmp_path: Path) -> None:
    plugin = _copy_example(tmp_path)
    before = json.loads((plugin / "voidcode-plugin.json").read_text(encoding="utf-8"))["files"][0]["sha256"]
    with (plugin / "tally_plugin.py").open("a", encoding="utf-8") as stream:
        stream.write("\n# author edit\n")
    _generate("--plugin-dir", str(plugin), "--check", code=1)
    assert json.loads((plugin / "voidcode-plugin.json").read_text(encoding="utf-8"))["files"][0]["sha256"] == before
    _generate("--plugin-dir", str(plugin))
    after = json.loads((plugin / "voidcode-plugin.json").read_text(encoding="utf-8"))["files"][0]["sha256"]
    assert after != before


def test_escaped_resource_is_rejected_and_manifest_is_not_rewritten(tmp_path: Path) -> None:
    plugin = _copy_example(tmp_path)
    manifest_path = plugin / "voidcode-plugin.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"].append({"path": "../escape.txt", "sha256": "0" * 64})
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    before = manifest_path.read_bytes()
    _generate("--plugin-dir", str(plugin), code=1)
    assert manifest_path.read_bytes() == before


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
        with closing(open_kernel(workspace, state_dir=state, trusted_plugins=frozenset({"tally-counter"}))) as kernel:
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
