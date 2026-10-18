"""P3 crash boundaries: a capability switch is either not committed at all or committed once."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import PROBE_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "crash loopback reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _configure(workspace: Path, server: OpenAILoopbackServer, plugin_path: Path, *, enabled: bool = True) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "loopback/exact-model"},
        "policy": {"approval_mode": "yolo", "read_only": False},
        "providers": {"loopback": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {PLUGIN_ID: {"path": str(plugin_path), "enabled": enabled}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


def _command(workspace: Path, state: Path, *arguments: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "voidcode.kernel",
        "--workspace",
        str(workspace),
        "--state-dir",
        str(state),
        "--trust-plugin",
        PLUGIN_ID,
        *arguments,
    ]


def _cli(workspace: Path, state: Path, *arguments: str, code: int = 0) -> dict:
    result = subprocess.run(_command(workspace, state, *arguments), capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == code, result.stdout + result.stderr
    return json.loads(result.stdout)


def _spawn(workspace: Path, state: Path, *arguments: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _command(workspace, state, *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _await(predicate, process: subprocess.Popen[str], *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if process.poll() is not None or time.monotonic() >= deadline:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"condition never became true: {stdout} {stderr}")
        time.sleep(0.02)


def _revision_of(state: Path) -> str:
    connection = sqlite3.connect(f"{(state / 'kernel-v1.sqlite3').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT active_revision_id FROM workspace_state LIMIT 1").fetchone()
        assert row is not None, "workspace has no recorded revision"
        return str(row[0])
    finally:
        connection.close()


def _session_row(state: Path, session_id: str) -> tuple[str, str]:
    connection = sqlite3.connect(f"{(state / 'kernel-v1.sqlite3').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT active_revision_id, status FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        assert row is not None, "session is not durable"
        return (str(row[0]), str(row[1]))
    finally:
        connection.close()


def _spawn_kernel_holder(workspace: Path, state: Path) -> subprocess.Popen[str]:
    """Open a fresh kernel against the existing state and keep it alive so its driver can run."""
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from voidcode.kernel.bootstrap import open_kernel\n"
        f"kernel = open_kernel(Path(sys.argv[1]), state_dir=Path(sys.argv[2]), trusted_plugins=frozenset({{{PLUGIN_ID!r}}}))\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(workspace), str(state)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # The kernel must actually open before we judge the driver's progress.
    _await_durable(lambda: "another kernel owns this workspace" not in _peek(process), process)
    time.sleep(0.5)
    return process


def _peek(process: subprocess.Popen[str]) -> str:
    return "" if process.poll() is None else "another kernel owns this workspace"


def _await_durable(predicate, process: subprocess.Popen[str], *, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"durable condition never became true: {stdout} {stderr}")
        time.sleep(0.05)


def _session_status(state: Path, session_id: str) -> str:
    connection = sqlite3.connect(f"{(state / 'kernel-v1.sqlite3').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT status FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return "" if row is None else str(row[0])
    finally:
        connection.close()


def _tool_texts(state: Path, session_id: str) -> list[str]:
    connection = sqlite3.connect(f"{(state / 'kernel-v1.sqlite3').resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT payload_json FROM session_events WHERE session_id = ? AND event_type = 'kernel.tool_completed'",
            (session_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


def _state_row(state: Path, session_id: str) -> tuple[int, str]:
    connection = sqlite3.connect(f"{(state / 'kernel-v1.sqlite3').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT version, payload_json FROM plugin_state WHERE scope = 'session' AND scope_key = ? AND owner = ?",
            (session_id, PLUGIN_ID),
        ).fetchone()
        assert row is not None, "session state is not durable"
        return (int(row[0]), str(row[1]))
    finally:
        connection.close()


def _activation_commits(state: Path, session_id: str) -> int:
    connection = sqlite3.connect(f"{(state / 'kernel-v1.sqlite3').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM session_events WHERE session_id = ? AND event_type = 'kernel.activation_committed'",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        connection.close()


def test_crash_after_the_commit_keeps_exactly_one_committed_target(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [
        _turn(_call("probe-1", PROBE_TOOL)),
        _turn(output="v1 done"),
        # The upgrade batch switches the generation; every later turn asks for the probe, so the
        # automatic continuation always executes it in the new generation.
        _turn(_call("reload-1", "kernel_reload", reason="upgrade now")),
        _turn(_call("probe-2", PROBE_TOOL)),
        _turn(output="post switch one"),
        _turn(_call("probe-3", PROBE_TOOL)),
        _turn(output="post switch two"),
        _turn(_call("probe-4", PROBE_TOOL)),
        _turn(output="post switch three"),
    ]
    with _server(replies) as server:
        _configure(workspace, server, source)
        first = _cli(workspace, state, "run", "write v1", "--session-id", "crash-session")
        assert first["status"] == "completed"
        before = first["revision_id"]
        write_reload_plugin(source, tool_output="v2", state_schema_version=2, migration="ok")

        process = _spawn(workspace, state, "run", "upgrade now", "--session-id", "crash-session")
        try:
            _await(lambda: _activation_commits(state, "crash-session") == 1, process)
        finally:
            if process.poll() is None:
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        committed_revision, _status = _session_row(state, "crash-session")
        assert committed_revision != before
        assert _revision_of(state) == committed_revision

        # A fresh kernel over the same state must finish the owed continuation BY ITSELF:
        # no resume, no prompt, no drain -- it drives the session to completion on the committed
        # generation and executes the new tool.
        driver = _spawn_kernel_holder(workspace, state)
        try:
            _await_durable(lambda: _session_status(state, "crash-session") == "completed", driver)
            _await_durable(lambda: any("probe=v2" in payload for payload in _tool_texts(state, "crash-session")), driver)
        finally:
            driver.terminate()
            driver.wait(timeout=10)
        assert _activation_commits(state, "crash-session") == 1
        _version, payload = _state_row(state, "crash-session")
        assert json.loads(payload).get("migrated_to") == 2
        shown = _cli(workspace, state, "show", "crash-session")
        assert shown["revision_id"] == committed_revision


def test_crash_during_candidate_validation_keeps_the_previous_generation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    source = tmp_path / "plugin"
    marker = tmp_path / "candidate.loaded"
    write_reload_plugin(source, tool_output="v1")
    replies = [
        _turn(_call("probe-1", PROBE_TOOL)),
        _turn(output="v1 done"),
        _turn(_call("reload-1", "kernel_reload", reason="crash before commit")),
        _turn(output="resumed on the old generation"),
        _turn(output="still the old generation"),
    ]
    with _server(replies) as server:
        _configure(workspace, server, source)
        first = _cli(workspace, state, "run", "write v1", "--session-id", "crash-pre")
        assert first["status"] == "completed"
        write_reload_plugin(source, tool_output="v2", state_schema_version=2, migration="hang", marker=marker)

        process = _spawn(workspace, state, "run", "upgrade and die", "--session-id", "crash-pre")
        try:
            _await(marker.exists, process)
        finally:
            if process.poll() is None:
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        # The candidate was admitted, imported, and was mid-migration when the kernel died:
        # nothing can have been committed.
        assert _revision_of(state) == first["revision_id"]
        assert _activation_commits(state, "crash-pre") == 0
        _version, payload = _state_row(state, "crash-pre")
        assert json.loads(payload) == {"note": "v1"}

        # The staged request is durable and unconsumed: once the source is valid again, the
        # resumed run retries it instead of losing the model's request.
        write_reload_plugin(source, tool_output="v2", state_schema_version=2, migration="ok")
        resumed = _cli(workspace, state, "resume", "crash-pre", "--max-steps", "4")
        assert resumed["revision_id"] != first["revision_id"]
        assert _revision_of(state) == resumed["revision_id"]
        assert _activation_commits(state, "crash-pre") == 1


def test_host_crash_inside_migration_refuses_only_the_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [
        _turn(_call("probe-1", PROBE_TOOL)),
        _turn(output="v1 done"),
        _turn(_call("reload-1", "kernel_reload", reason="host dies mid migration")),
        _turn(output="kept v1"),
    ]
    with _server(replies) as server:
        _configure(workspace, server, source)
        first = _cli(workspace, state, "run", "write v1", "--session-id", "crash-host")
        assert first["status"] == "completed"
        write_reload_plugin(source, tool_output="v2", state_schema_version=2, migration="crash")

        second = _cli(workspace, state, "run", "upgrade", "--session-id", "crash-host")
        assert second["status"] == "completed"
        assert second["revision_id"] == first["revision_id"]
        assert _activation_commits(state, "crash-host") == 0
        assert _revision_of(state) == first["revision_id"]
        _version, payload = _state_row(state, "crash-host")
        assert json.loads(payload) == {"note": "v1"}
