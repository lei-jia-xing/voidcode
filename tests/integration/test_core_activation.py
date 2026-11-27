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
from voidcode.config import dumps_toml

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
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo", "read_only": False},
        "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {PLUGIN_ID: {"path": str(plugin_path), "enabled": enabled}},
    }
    (workspace / "voidcode.toml").write_text(dumps_toml(config), encoding="utf-8")


def _command(workspace: Path, state: Path, *arguments: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "voidcode",
        "--workspace",
        str(workspace),
        "--state-dir",
        str(state),
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


#: Cold start admits the whole bundled plugin set before the reload flow can reach its marker,
#: under parallel xdist workers; 30 s was tight and the failure path itself raised
#: TimeoutExpired by calling communicate() on a process that was still running.
_CRASH_STARTUP_DEADLINE_SECONDS = 60.0


def _await(predicate, process: subprocess.Popen[str], *, timeout: float = _CRASH_STARTUP_DEADLINE_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if process.poll() is not None or time.monotonic() >= deadline:
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=15)
            pytest.fail(f"condition never became true: {stdout} {stderr}")
        time.sleep(0.02)


def _revision_of(state: Path) -> str:
    connection = sqlite3.connect(f"{(state / 'voidcode.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT active_revision_id FROM workspace_state LIMIT 1").fetchone()
        assert row is not None, "workspace has no recorded revision"
        return str(row[0])
    finally:
        connection.close()


def _session_row(state: Path, session_id: str) -> tuple[str, str]:
    connection = sqlite3.connect(f"{(state / 'voidcode.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT active_revision_id, status FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        assert row is not None, "session is not durable"
        return (str(row[0]), str(row[1]))
    finally:
        connection.close()


def _spawn_reloading_holder(workspace: Path, state: Path, session_id: str, trigger: Path, done: Path) -> subprocess.Popen[str]:
    """A long-lived v1 core that reloads on trigger, so the old generation stays alive across the source edit.

    Under the current-source contract a fresh process would boot the edited source; only a
    core opened BEFORE the edit can run a live activation against it. The holder watches
    for the trigger file, invokes `core.reload` once, and records the outcome.
    """
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from voidcode.bootstrap import open_core\n"
        "workspace, state_dir = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "session, trigger, done, ready = sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5]), Path(sys.argv[6])\n"
        "core = open_core(workspace, state_dir=state_dir)\n"
        "revision = core.command_catalog().revision_id\n"
        "ready.write_text('opened', encoding='utf-8')\n"
        "deadline = time.monotonic() + 120.0\n"
        "while not trigger.exists():\n"
        "    if time.monotonic() > deadline:\n"
        "        raise SystemExit('holder timed out waiting for the trigger')\n"
        "    time.sleep(0.05)\n"
        "result = core.invoke_command(session, 'core.reload', {}, expected_revision=revision)\n"
        "done.write_text(result.status + ':' + (result.error or ''), encoding='utf-8')\n"
        "time.sleep(120)\n"
    )
    ready = trigger.parent / (trigger.stem + ".ready")
    if ready.exists():
        ready.unlink()
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(workspace), str(state), session_id, str(trigger), str(done), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Readiness is a positive signal AFTER the holder's core opened: the workspace
    # lease alone is not enough (bootstrap takes it transiently before importing).
    _await(ready.exists, process)
    return process


def _spawn_model_reloading_holder(workspace: Path, state: Path, session_id: str, trigger: Path, ready: Path) -> subprocess.Popen[str]:
    """A long-lived v1 core that runs one model turn after the source edit.

    The model turn asks for `core_reload`, so the committed switch owes the same run a
    continuation. Only a core opened BEFORE the edit can do this: a fresh process boots the
    edited source and the state-schema guard refuses.
    """
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from voidcode.bootstrap import open_core\n"
        "workspace, state_dir = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "session, trigger, ready = sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5])\n"
        "core = open_core(workspace, state_dir=state_dir)\n"
        "ready.write_text('opened', encoding='utf-8')\n"
        "deadline = time.monotonic() + 120.0\n"
        "while not trigger.exists():\n"
        "    if time.monotonic() > deadline:\n"
        "        raise SystemExit('holder timed out waiting for the trigger')\n"
        "    time.sleep(0.05)\n"
        "core.run('upgrade now', session_id=session, max_steps=4)\n"
        "time.sleep(120)\n"
    )
    ready.unlink(missing_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(workspace), str(state), session_id, str(trigger), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _await(ready.exists, process)
    return process


def _add_activation_barrier(source: Path, marker: Path) -> None:
    """Make the written generation block once between its activation and the next dispatch.

    `Plugin.context` runs in `_messages`, after `core.activation_committed` committed the
    switch and before the continuation's `core.provider_intent`: killing at the marker leaves
    the owed resume intent durable. The marker also lets the restart's own context call pass.
    """
    path = source / "__init__.py"
    text = path.read_text(encoding="utf-8")
    barrier = (
        "\n\ndef _activation_barrier(context, services):\n"
        "    import os, time\n"
        "    from pathlib import Path\n"
        f"    marker = Path({str(marker)!r})\n"
        "    if marker.exists():\n"
        "        return None\n"
        "    marker.write_text(str(os.getpid()), encoding='utf-8')\n"
        "    time.sleep(60)\n"
        "    return None\n"
    )
    assert "\ndef create_plugin(" in text, "the fixture writer changed its factory shape"
    text = text.replace("\ndef create_plugin(", barrier + "\ndef create_plugin(", 1)
    text = text.replace("    return Plugin(\n", "    return Plugin(\n        context=_activation_barrier,\n", 1)
    path.write_text(text, encoding="utf-8")


def _spawn_core_holder(workspace: Path, state: Path) -> subprocess.Popen[str]:
    """Open a fresh core against the existing state and keep it alive so its driver can run.

    Callers judge real progress from the durable state (session status, events); spawning is not
    a readiness proof, so nothing here guesses at process health.
    """
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from voidcode.bootstrap import open_core\n"
        "core = open_core(Path(sys.argv[1]), state_dir=Path(sys.argv[2]))\n"
        "time.sleep(60)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code, str(workspace), str(state)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _await_durable(predicate, process: subprocess.Popen[str], *, timeout: float = _CRASH_STARTUP_DEADLINE_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=15)
            pytest.fail(f"durable condition never became true: {stdout} {stderr}")
        time.sleep(0.05)


def _session_status(state: Path, session_id: str) -> str:
    connection = sqlite3.connect(f"{(state / 'voidcode.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT status FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return "" if row is None else str(row[0])
    finally:
        connection.close()


def _tool_texts(state: Path, session_id: str) -> list[str]:
    connection = sqlite3.connect(f"{(state / 'voidcode.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT payload_json FROM session_events WHERE session_id = ? AND event_type = 'core.tool_completed'",
            (session_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


def _state_row(state: Path, session_id: str) -> tuple[int, str]:
    connection = sqlite3.connect(f"{(state / 'voidcode.db').resolve().as_uri()}?mode=ro", uri=True)
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
    connection = sqlite3.connect(f"{(state / 'voidcode.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM session_events WHERE session_id = ? AND event_type = 'core.activation_committed'",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        connection.close()


def _event_payloads(state: Path, session_id: str, event_type: str) -> list[dict]:
    connection = sqlite3.connect(f"{(state / 'voidcode.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT payload_json FROM session_events WHERE session_id = ? AND event_type = ? ORDER BY sequence",
            (session_id, event_type),
        ).fetchall()
        return [json.loads(str(row[0])) for row in rows]
    finally:
        connection.close()


def _event_sequences(state: Path, session_id: str, event_type: str) -> list[int]:
    connection = sqlite3.connect(f"{(state / 'voidcode.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT sequence FROM session_events WHERE session_id = ? AND event_type = ? ORDER BY sequence",
            (session_id, event_type),
        ).fetchall()
        return [int(row[0]) for row in rows]
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
        _turn(_call("reload-1", "core_reload", reason="upgrade now")),
        _turn(_call("probe-2", PROBE_TOOL)),
        _turn(output="post switch one"),
    ]
    with _server(replies) as server:
        _configure(workspace, server, source)
        first = _cli(workspace, state, "run", "write v1", "--session-id", "crash-session")
        assert first["status"] == "completed"
        before = first["revision_id"]

        # The v1 core stays alive across the source edit: only a generation opened
        # BEFORE the edit can run a live activation against it. The barrier blocks the
        # committed run's continuation in `_messages`, after the switch and before the
        # continuation's provider dispatch, so the owed resume intent is still durable.
        trigger = tmp_path / "reload.trigger"
        ready = tmp_path / "reload.ready"
        barrier = tmp_path / "continuation.started"
        holder = _spawn_model_reloading_holder(workspace, state, "crash-session", trigger, ready)
        try:
            write_reload_plugin(source, tool_output="v2")
            _add_activation_barrier(source, barrier)
            trigger.write_text("reload", encoding="utf-8")
            _await(lambda: _activation_commits(state, "crash-session") == 1 and barrier.exists(), holder)
        finally:
            if holder.poll() is None:
                os.kill(holder.pid, signal.SIGKILL)
                holder.wait(timeout=10)
        # The kill landed after the commit and before the continuation dispatch: the session is
        # still running and no provider turn was recorded after the activation.
        committed_revision, status = _session_row(state, "crash-session")
        assert status == "running"
        assert committed_revision != before
        assert _revision_of(state) == committed_revision
        assert (
            max(_event_sequences(state, "crash-session", "core.provider_intent"))
            < _event_sequences(state, "crash-session", "core.activation_committed")[0]
        )
        _version, payload = _state_row(state, "crash-session")
        assert json.loads(payload) == {"note": "v1"}

        # A fresh core over the same state must finish the owed continuation BY ITSELF:
        # no resume, no prompt, no drain -- it drives the session to completion on the committed
        # generation and executes the new tool.
        driver = _spawn_core_holder(workspace, state)
        try:
            _await_durable(lambda: _session_status(state, "crash-session") == "completed", driver)
            _await_durable(lambda: any("probe=v2" in payload for payload in _tool_texts(state, "crash-session")), driver)
        finally:
            driver.terminate()
            driver.wait(timeout=10)
        assert _activation_commits(state, "crash-session") == 1
        _version, payload = _state_row(state, "crash-session")
        assert json.loads(payload) == {"note": "v2"}
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
        # The model asks for the switch; the migration below hangs, so the request is never
        # answered and nothing commits.
        _turn(_call("reload-1", "core_reload", reason="crash before commit")),
        _turn(output="resumed on the adopted generation"),
    ]
    with _server(replies) as server:
        _configure(workspace, server, source)
        first = _cli(workspace, state, "run", "write v1", "--session-id", "crash-pre")
        assert first["status"] == "completed"

        trigger = tmp_path / "reload.trigger"
        ready = tmp_path / "reload.ready"
        holder = _spawn_model_reloading_holder(workspace, state, "crash-pre", trigger, ready)
        try:
            write_reload_plugin(source, tool_output="v2", state_schema_version=2, migration="hang", marker=marker)
            trigger.write_text("reload", encoding="utf-8")
            # Boot also imports the plugin (writing its pid); only the hang body's
            # 'migrating' proves the candidate entered migration before the kill.
            _await(lambda: marker.exists() and marker.read_text(encoding="utf-8") == "migrating", holder)
        finally:
            if holder.poll() is None:
                os.kill(holder.pid, signal.SIGKILL)
                holder.wait(timeout=10)
        # The candidate was admitted, imported, and was mid-migration when the core died:
        # nothing can have been committed, and the dead run is still 'running' durably.
        assert _revision_of(state) == first["revision_id"]
        assert _activation_commits(state, "crash-pre") == 0
        assert _session_status(state, "crash-pre") == "running"
        _version, payload = _state_row(state, "crash-pre")
        assert json.loads(payload) == {"note": "v1"}

        # A plain start against the edited source refuses to reinterpret the old row.
        refused = _cli(workspace, state, "catalog", code=1)
        assert "state schema mismatch" in refused["error"]

        # The explicit same-image adoption recovers the dead run: the held workspace lease
        # proves no core owns it, so the orphan settles as interrupted in the SAME transaction
        # that migrates the state. Its cursor is preserved for an explicit resume.
        write_reload_plugin(source, tool_output="v2", state_schema_version=2)
        still_refused = _cli(workspace, state, "catalog", code=1)
        assert "state schema mismatch" in still_refused["error"]
        from voidcode.bootstrap import adopt_generation

        with pytest.raises(ValueError, match="state schema mismatch"):
            adopt_generation(workspace=workspace, state_dir=state, session_id="crash-pre")
        # Now write matching schema v1: adoption succeeds and recovers the dead run
        write_reload_plugin(source, tool_output="v2")
        report = adopt_generation(workspace=workspace, state_dir=state, session_id="crash-pre")
        assert "migrated_owners" not in report
        assert _revision_of(state) == report["to_revision_id"]
        assert _revision_of(state) != first["revision_id"]
        _version, payload = _state_row(state, "crash-pre")
        assert _session_status(state, "crash-pre") == "interrupted"
        recovery = _event_payloads(state, "crash-pre", "core.run_interrupted")
        assert recovery, "the orphan recovery fact is durable"
        assert recovery[-1]["stop_cause"] == "shutdown"
        assert isinstance(recovery[-1]["eligible_owners"], list), "fact eligibility is frozen, never guessed"
        assert _activation_commits(state, "crash-pre") == 0
        catalog = _cli(workspace, state, "catalog")
        assert catalog["revision_id"] == report["to_revision_id"]

        # Safe continuation: the durable activation request is retried exactly once on the
        # adopted generation, and the session reaches a terminal result.
        resumed = _cli(workspace, state, "resume", "crash-pre", "--max-steps", "4")
        assert resumed["status"] == "completed"
        assert _activation_commits(state, "crash-pre") == 1
        assert _revision_of(state) == resumed["revision_id"]


def test_adoption_recovers_a_non_acting_orphaned_run(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    source = tmp_path / "plugin"
    marker = tmp_path / "candidate.loaded"
    write_reload_plugin(source, tool_output="v1")
    replies = [
        _turn(_call("probe-1", PROBE_TOOL)),
        _turn(output="v1 done"),
        _turn(_call("reload-1", "core_reload", reason="crash before commit")),
    ]
    with _server(replies) as server:
        _configure(workspace, server, source)
        first = _cli(workspace, state, "run", "write v1", "--session-id", "crash-pre")
        assert first["status"] == "completed"

        trigger = tmp_path / "reload.trigger"
        ready = tmp_path / "reload.ready"
        # The victim session is created by the holder's own run; the acting session is the
        # already-completed one, so this exercises the store's non-acting orphan path.
        holder = _spawn_model_reloading_holder(workspace, state, "victim", trigger, ready)
        try:
            write_reload_plugin(source, tool_output="v2", migration="hang", marker=marker)
            # simulate running
            trigger = tmp_path / "reload.trigger"
            trigger.write_text("reload", encoding="utf-8")
            _await(lambda: marker.exists() and marker.read_text(encoding="utf-8") == "migrating", holder)
        finally:
            if holder.poll() is None:
                os.kill(holder.pid, signal.SIGKILL)
                holder.wait(timeout=10)
        assert _session_status(state, "victim") == "running"
        assert _activation_commits(state, "victim") == 0

        write_reload_plugin(source, tool_output="v2")
        from voidcode.bootstrap import adopt_generation

        report = adopt_generation(workspace=workspace, state_dir=state, session_id="crash-pre")
        assert "migrated_owners" not in report
        assert _session_status(state, "victim") == "interrupted"
        assert _session_status(state, "crash-pre") == "completed"
        recovery = _event_payloads(state, "victim", "core.run_interrupted")
        assert recovery and recovery[-1]["stop_cause"] == "shutdown"
        assert isinstance(recovery[-1]["eligible_owners"], list)
        assert _event_payloads(state, "crash-pre", "core.run_interrupted") == []


def test_core_crash_inside_migration_commits_nothing_and_keeps_the_previous_generation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [
        _turn(_call("probe-1", PROBE_TOOL)),
        _turn(output="v1 done"),
    ]
    with _server(replies) as server:
        _configure(workspace, server, source)
        first = _cli(workspace, state, "run", "write v1", "--session-id", "crash-host")
        assert first["status"] == "completed"

        # In-process, a crashing migration takes the core down with it: the holder
        # exits 9 by itself and the activation transaction never ran.
        trigger = tmp_path / "reload.trigger"
        done = tmp_path / "reload.done"
        holder = _spawn_reloading_holder(workspace, state, "crash-host", trigger, done)
        write_reload_plugin(source, tool_output="v2", state_schema_version=2, migration="crash")
        trigger.write_text("reload", encoding="utf-8")
        holder.wait(timeout=120)
        assert holder.returncode == 9, holder.returncode
        assert _activation_commits(state, "crash-host") == 0
        assert _revision_of(state) == first["revision_id"]
        _version, payload = _state_row(state, "crash-host")
        assert json.loads(payload) == {"note": "v1"}

        # The crashed source still declares v2, so a plain start refuses; restoring a
        # valid v2 migration and adopting explicitly recovers the new generation.
        refused = _cli(workspace, state, "catalog", code=1)
        assert "state schema mismatch" in refused["error"]
        write_reload_plugin(source, tool_output="v2")
        from voidcode.bootstrap import adopt_generation

        report = adopt_generation(workspace=workspace, state_dir=state, session_id="crash-host")
        assert "migrated_owners" not in report
        catalog = _cli(workspace, state, "catalog")
        assert catalog["revision_id"] == report["to_revision_id"]
