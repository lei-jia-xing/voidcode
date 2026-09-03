from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools import ToolCall
from voidcode.tools.background_process_start import (
    _MAX_BACKGROUND_PROCESS_LOG_LINES,
    _terminate_background_process_group,
)
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def test_background_process_tools_are_registered() -> None:
    runtime = VoidCodeRuntime(workspace=Path(tempfile.mkdtemp()))
    registry = runtime._base_tool_registry
    assert registry.resolve("background_process_start").definition.name == "background_process_start"
    assert registry.resolve("background_process_logs").definition.name == "background_process_logs"
    assert registry.resolve("background_process_stop").definition.name == "background_process_stop"
    assert registry.resolve("background_process_send").definition.name == "background_process_send"
    runtime.__exit__(None, None, None)


def test_background_process_start_logs_and_stop(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    start_tool = runtime._base_tool_registry.resolve("background_process_start")
    logs_tool = runtime._base_tool_registry.resolve("background_process_logs")
    stop_tool = runtime._base_tool_registry.resolve("background_process_stop")
    command = f'"{sys.executable}" -c "import time; print(\'ready\', flush=True); time.sleep(5)"'
    start_result = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": command}),
        workspace=tmp_path,
    )
    process_id = str(start_result.data["process_id"])
    assert start_result.status == "ok"
    deadline = time.time() + 5
    logs_result = None
    while time.time() < deadline:
        logs_result = logs_tool.invoke(
            ToolCall(tool_name="background_process_logs", arguments={"process_id": process_id}),
            workspace=tmp_path,
        )
        if "ready" in (logs_result.content or ""):
            break
        time.sleep(0.05)
    assert logs_result is not None
    assert logs_result.status == "ok"
    assert isinstance(logs_result.content, str)
    assert "ready" in logs_result.content
    stop_result = stop_tool.invoke(
        ToolCall(tool_name="background_process_stop", arguments={"process_id": process_id}),
        workspace=tmp_path,
    )
    assert stop_result.status == "ok"
    assert stop_result.data["running"] is False
    runtime.__exit__(None, None, None)


def test_background_process_start_reuses_running_process_for_same_command(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    start_tool = runtime._base_tool_registry.resolve("background_process_start")
    stop_tool = runtime._base_tool_registry.resolve("background_process_stop")
    command = f'"{sys.executable}" -c "import time; print(\'ready\', flush=True); time.sleep(5)"'

    first = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": command}),
        workspace=tmp_path,
    )
    second = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": command}),
        workspace=tmp_path,
    )

    assert first.data["process_id"] == second.data["process_id"]
    assert second.data["reused"] is True
    assert "Reusing background process" in cast(str, second.content)
    assert "exact-match process is already running" in cast(str, second.content)
    assert "reuse process_id" in cast(str, second.data["guidance"])
    assert str(second.data["guidance"]).startswith("An exact-match process is already running; reuse process_id")

    stop_tool.invoke(
        ToolCall(
            tool_name="background_process_stop",
            arguments={"process_id": str(first.data["process_id"])},
        ),
        workspace=tmp_path,
    )
    runtime.__exit__(None, None, None)


def test_background_process_start_reuses_running_process_for_same_trimmed_command_and_workspace(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    start_tool = runtime._base_tool_registry.resolve("background_process_start")
    stop_tool = runtime._base_tool_registry.resolve("background_process_stop")
    trimmed_command = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    padded_command = f"  {trimmed_command}  "

    first = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": padded_command}),
        workspace=tmp_path,
    )
    second = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": trimmed_command}),
        workspace=tmp_path,
    )

    assert first.data["process_id"] == second.data["process_id"]
    assert second.data["reused"] is True
    assert "same trimmed command in this workspace" in cast(str, second.content)

    stop_tool.invoke(
        ToolCall(
            tool_name="background_process_stop",
            arguments={"process_id": str(first.data["process_id"])},
        ),
        workspace=tmp_path,
    )
    runtime.__exit__(None, None, None)


def test_background_process_start_creates_new_process_for_different_command_or_workspace(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    start_tool = runtime._base_tool_registry.resolve("background_process_start")
    stop_tool = runtime._base_tool_registry.resolve("background_process_stop")
    first_command = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    second_command = f'"{sys.executable}" -c "import time; print(\'different\', flush=True); time.sleep(5)"'
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()

    first = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": first_command}),
        workspace=tmp_path,
    )
    second = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": second_command}),
        workspace=tmp_path,
    )
    third = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": first_command}),
        workspace=other_workspace,
    )

    assert first.data["process_id"] != second.data["process_id"]
    assert second.data["reused"] is False
    assert first.data["process_id"] != third.data["process_id"]
    assert third.data["reused"] is False

    stop_tool.invoke(
        ToolCall(
            tool_name="background_process_stop",
            arguments={"process_id": str(first.data["process_id"])},
        ),
        workspace=tmp_path,
    )
    stop_tool.invoke(
        ToolCall(
            tool_name="background_process_stop",
            arguments={"process_id": str(second.data["process_id"])},
        ),
        workspace=tmp_path,
    )
    stop_tool.invoke(
        ToolCall(
            tool_name="background_process_stop",
            arguments={"process_id": str(third.data["process_id"])},
        ),
        workspace=other_workspace,
    )
    runtime.__exit__(None, None, None)


def test_background_process_logs_retains_bounded_recent_lines(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    start_tool = runtime._base_tool_registry.resolve("background_process_start")
    logs_tool = runtime._base_tool_registry.resolve("background_process_logs")
    stop_tool = runtime._base_tool_registry.resolve("background_process_stop")
    line_count = _MAX_BACKGROUND_PROCESS_LOG_LINES + 25
    command = f'"{sys.executable}" -c "import sys; [sys.stdout.write(\'line-%d\\n\' % i) for i in range({line_count})]"'
    start_result = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": command}),
        workspace=tmp_path,
    )
    process_id = str(start_result.data["process_id"])
    deadline = time.time() + 5
    logs_result = None
    while time.time() < deadline:
        logs_result = logs_tool.invoke(
            ToolCall(tool_name="background_process_logs", arguments={"process_id": process_id}),
            workspace=tmp_path,
        )
        if logs_result.data["running"] is False:
            break
        time.sleep(0.05)
    assert logs_result is not None
    assert logs_result.status == "ok"
    stdout = str(logs_result.data["stdout"])
    assert "line-0" not in stdout
    assert f"line-{line_count - 1}" in stdout
    assert logs_result.data["stdout_retained_lines"] == _MAX_BACKGROUND_PROCESS_LOG_LINES
    assert logs_result.data["stdout_dropped_lines"] == 25
    assert logs_result.data["truncated"] is True
    references = logs_result.data["references"]
    assert isinstance(references, list)
    assert references
    assert str(references[0]).startswith("voidcode://artifact/")
    stdout_artifact = cast(dict[str, object], logs_result.data["stdout_artifact"])
    assert isinstance(stdout_artifact, dict)
    stdout_artifact_id = str(stdout_artifact["artifact_id"])
    assert stdout_artifact_id == str(references[0]).removeprefix("voidcode://artifact/")
    assert logs_result.reference == references[0]
    assert "Background process logs truncated" in (logs_result.content or "")
    assert "retained log tails" in (logs_result.content or "")
    assert "continuous watch loop" in (logs_result.content or "")
    assert "meaningful state change" in (logs_result.content or "")
    assert isinstance(logs_result.data["guidance"], str)
    assert "full logs" not in (logs_result.content or "")
    stop_tool.invoke(
        ToolCall(tool_name="background_process_stop", arguments={"process_id": process_id}),
        workspace=tmp_path,
    )
    runtime.__exit__(None, None, None)


@pytest.mark.skipif(os.name == "nt", reason="posix-only process group behavior")
def test_background_process_stop_escalates_when_descendant_ignores_sigterm(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    start_tool = runtime._base_tool_registry.resolve("background_process_start")
    stop_tool = runtime._base_tool_registry.resolve("background_process_stop")
    child_file = tmp_path / "child.pid"
    command = (
        f'"{sys.executable}" -c '
        f'"import os, signal, subprocess, sys, time; '
        f"child=subprocess.Popen([sys.executable,'-c',"
        f"'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
        f"open(r'{child_file}', 'w', encoding='utf-8').write(str(child.pid)); "
        f'time.sleep(30)"'
    )
    start_result = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": command}),
        workspace=tmp_path,
    )
    process_id = str(start_result.data["process_id"])
    deadline = time.time() + 5
    child_pid: int | None = None
    while time.time() < deadline:
        if child_file.exists():
            try:
                child_pid_text = child_file.read_text(encoding="utf-8").strip()
            except OSError:
                child_pid_text = ""
            if child_pid_text.isdecimal():
                child_pid = int(child_pid_text)
                break
        time.sleep(0.05)
    assert child_pid is not None
    stop_result = stop_tool.invoke(
        ToolCall(tool_name="background_process_stop", arguments={"process_id": process_id}),
        workspace=tmp_path,
    )

    assert stop_result.status == "ok"
    assert stop_result.data["running"] is False
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    runtime.__exit__(None, None, None)


@pytest.mark.skipif(os.name == "nt", reason="posix-only process group behavior")
def test_terminate_background_process_group_sends_sigkill_after_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []
    waits: list[float | None] = []
    group_exists = True

    class _FakeProcess:
        pid = 4321

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            return 0

    def fake_killpg(process_group_id: int, sig: int) -> None:
        nonlocal group_exists
        calls.append((process_group_id, sig))
        if sig == signal.SIGKILL:
            group_exists = False

    monkeypatch.setattr("voidcode.tools.background_process_start.os.killpg", fake_killpg)
    monkeypatch.setattr(
        "voidcode.tools.background_process_start._process_group_exists",
        lambda _process_group_id: group_exists,
    )

    _terminate_background_process_group(cast(subprocess.Popen[str], _FakeProcess()))

    assert calls == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]
    assert waits == [1]


@pytest.mark.skipif(os.name == "nt", reason="posix-only process group behavior")
def test_background_process_stop_terminates_spawned_children(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    start_tool = runtime._base_tool_registry.resolve("background_process_start")
    stop_tool = runtime._base_tool_registry.resolve("background_process_stop")
    child_file = tmp_path / "child.pid"
    command = (
        f'"{sys.executable}" -c '
        f'"import subprocess, sys, time; '
        f"child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"open(r'{child_file}', 'w', encoding='utf-8').write(str(child.pid)); "
        f'time.sleep(30)"'
    )
    start_result = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": command}),
        workspace=tmp_path,
    )
    process_id = str(start_result.data["process_id"])
    deadline = time.time() + 5
    while time.time() < deadline and not child_file.exists():
        time.sleep(0.05)
    assert child_file.exists()
    child_pid = int(child_file.read_text(encoding="utf-8"))
    stop_tool.invoke(
        ToolCall(tool_name="background_process_stop", arguments={"process_id": process_id}),
        workspace=tmp_path,
    )
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    runtime.__exit__(None, None, None)


def test_terminate_background_process_group_uses_taskkill_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    killed = False

    class _FakeProcess:
        pid = 4321

        def kill(self) -> None:
            nonlocal killed
            killed = True

        def wait(self, timeout: float | None = None) -> None:
            _ = timeout

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("voidcode.tools.background_process_start.os.name", "nt")
    monkeypatch.setattr("voidcode.tools.background_process_start.shutil.which", lambda _name: "taskkill")
    monkeypatch.setattr("voidcode.tools.background_process_start.subprocess.run", fake_run)

    _terminate_background_process_group(cast(subprocess.Popen[str], _FakeProcess()))

    assert calls == [["taskkill", "/PID", "4321", "/T", "/F"]]
    assert killed is False


def test_runtime_exit_stops_managed_background_processes(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    runtime = VoidCodeRuntime(workspace=tmp_path, session_store=SqliteSessionStore(database_path=database_path))
    start_tool = runtime._base_tool_registry.resolve("background_process_start")
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    started = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": command}),
        workspace=tmp_path,
    )
    process_id = str(started.data["process_id"])
    state = runtime.background_process_manager.load(process_id)
    assert state is not None
    assert state.process.poll() is None
    runtime.__exit__(None, None, None)
    assert state.process.poll() is not None
    persisted = SqliteSessionStore(database_path=database_path).load_background_process(workspace=tmp_path, process_id=process_id)
    assert persisted is not None
    assert persisted["status"] == "exited"


def test_background_process_send_writes_stdin(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    start_tool = runtime._base_tool_registry.resolve("background_process_start")
    send_tool = runtime._base_tool_registry.resolve("background_process_send")
    stop_tool = runtime._base_tool_registry.resolve("background_process_stop")
    command = (
        f'"{sys.executable}" -c '
        "\"import sys; print('ready', flush=True); "
        "line = sys.stdin.readline(); "
        "print(f'echo:{line.strip()}', flush=True); "
        'time.sleep(30)"'
    )
    started = start_tool.invoke(
        ToolCall(tool_name="background_process_start", arguments={"command": command}),
        workspace=tmp_path,
    )
    process_id = str(started.data["process_id"])
    deadline = time.time() + 5
    sent = False
    while time.time() < deadline:
        try:
            result = send_tool.invoke(
                ToolCall(
                    tool_name="background_process_send",
                    arguments={"process_id": process_id, "input": "hello"},
                ),
                workspace=tmp_path,
            )
        except ValueError:
            time.sleep(0.05)
            continue
        sent = True
        break
    assert sent is True
    assert result is not None
    assert result.status == "ok"
    assert f"Sent input to background process {process_id}" in cast(str, result.content)
    assert result.data["input"] == "hello"
    assert result.data["newline"] is True
    stop_tool.invoke(
        ToolCall(tool_name="background_process_stop", arguments={"process_id": process_id}),
        workspace=tmp_path,
    )
    runtime.__exit__(None, None, None)


def test_background_process_send_fails_for_unknown_process(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    send_tool = runtime._base_tool_registry.resolve("background_process_send")
    with pytest.raises(ValueError, match="unknown background process"):
        send_tool.invoke(
            ToolCall(
                tool_name="background_process_send",
                arguments={"process_id": "proc-missing", "input": "hello"},
            ),
            workspace=tmp_path,
        )
    runtime.__exit__(None, None, None)


def test_background_process_reconciles_after_runtime_restart_and_rejects_other_owner(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    runtime_one = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=SqliteSessionStore(database_path=database_path),
    )
    start_tool = runtime_one._base_tool_registry.resolve("background_process_start")
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    owner_context = RuntimeToolInvocationContext(session_id="owner-a")
    with bind_runtime_tool_context(owner_context):
        started = start_tool.invoke(
            ToolCall(tool_name="background_process_start", arguments={"command": command}),
            workspace=tmp_path,
        )
    process_id = str(started.data["process_id"])
    runtime_two = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=SqliteSessionStore(database_path=database_path),
    )
    logs_tool = runtime_two._base_tool_registry.resolve("background_process_logs")
    stop_tool = runtime_two._base_tool_registry.resolve("background_process_stop")
    send_tool = runtime_two._base_tool_registry.resolve("background_process_send")
    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="owner-b")):
        with pytest.raises(ValueError, match="another session"):
            stop_tool.invoke(
                ToolCall(tool_name="background_process_stop", arguments={"process_id": process_id}),
                workspace=tmp_path,
            )
    with bind_runtime_tool_context(owner_context):
        stale_logs = logs_tool.invoke(
            ToolCall(tool_name="background_process_logs", arguments={"process_id": process_id}),
            workspace=tmp_path,
        )
        stale_send = send_tool.invoke(
            ToolCall(tool_name="background_process_send", arguments={"process_id": process_id, "input": "hello"}),
            workspace=tmp_path,
        )
        stale_stop = stop_tool.invoke(
            ToolCall(tool_name="background_process_stop", arguments={"process_id": process_id}),
            workspace=tmp_path,
        )
    assert stale_logs.status == "error"
    assert stale_send.status == "error"
    assert stale_stop.status == "error"
    assert "externally managed" in str(stale_logs.error)
    assert "externally managed" in str(stale_send.error)
    assert "externally managed" in str(stale_stop.error)
    assert stale_logs.data["status"] == "stale"
    assert stale_logs.data["controllable"] is False
    assert stale_logs.data["prior_runtime"] is True
    assert stale_logs.data["running"] is None
    start_two = runtime_two._base_tool_registry.resolve("background_process_start")
    with bind_runtime_tool_context(owner_context):
        replacement = start_two.invoke(
            ToolCall(tool_name="background_process_start", arguments={"command": command}),
            workspace=tmp_path,
        )
    assert replacement.data["reused"] is False
    assert replacement.data["stale_process_id"] == process_id
    assert "Do not use that stale id" in str(replacement.data["guidance"])
    restored = runtime_two.background_process_manager.load(process_id, workspace=tmp_path)
    assert restored is not None
    assert restored.prior_runtime is True
    runtime_two.__exit__(None, None, None)
    assert _pid_is_running(int(started.data["pid"]))
    assert not _pid_is_running(int(replacement.data["pid"]))
    runtime_one.__exit__(None, None, None)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_restart_stale_identity_and_dead_pid_are_non_controllable(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    external = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        start_new_session=True,
    )
    try:
        store.register_background_process(
            workspace=tmp_path,
            process_id="proc-mismatch",
            owner_session_id="owner-a",
            command="external-mismatch",
            cwd=str(tmp_path.resolve()),
            pid=external.pid,
            process_group_id=external.pid,
            process_identity="not-the-current-identity",
            stdout_path="",
            stderr_path="",
        )
        dead = subprocess.Popen([sys.executable, "-c", "pass"], cwd=tmp_path)
        dead_pid = dead.pid
        assert dead.wait(timeout=5) == 0
        store.register_background_process(
            workspace=tmp_path,
            process_id="proc-dead",
            owner_session_id="owner-a",
            command="external-dead",
            cwd=str(tmp_path.resolve()),
            pid=dead_pid,
            process_group_id=None,
            process_identity="dead-identity",
            stdout_path="",
            stderr_path="",
        )
        runtime = VoidCodeRuntime(workspace=tmp_path, session_store=SqliteSessionStore(database_path=database_path))
        logs_tool = runtime._base_tool_registry.resolve("background_process_logs")
        with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="owner-a")):
            mismatch = logs_tool.invoke(
                ToolCall(tool_name="background_process_logs", arguments={"process_id": "proc-mismatch"}),
                workspace=tmp_path,
            )
            dead_result = logs_tool.invoke(
                ToolCall(tool_name="background_process_logs", arguments={"process_id": "proc-dead"}),
                workspace=tmp_path,
            )
        assert mismatch.status == "error"
        assert mismatch.data["identity_match"] is False
        assert mismatch.data["running"] is None
        assert dead_result.status == "error"
        assert dead_result.data["identity_match"] is False
        assert dead_result.data["observed_running"] is False
        runtime.__exit__(None, None, None)
        assert external.poll() is None
    finally:
        external.terminate()
        external.wait(timeout=5)


def test_restart_does_not_kill_terminal_prior_process_group(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    external = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        start_new_session=True,
    )
    try:
        store.register_background_process(
            workspace=tmp_path,
            process_id="proc-terminal",
            owner_session_id="owner-a",
            command="external-terminal",
            cwd=str(tmp_path.resolve()),
            pid=external.pid,
            process_group_id=external.pid,
            process_identity="terminal-identity",
            stdout_path="",
            stderr_path="",
        )
        store.mark_background_process_exit(
            workspace=tmp_path,
            process_id="proc-terminal",
            status="exited",
            exit_code=0,
        )
        runtime = VoidCodeRuntime(workspace=tmp_path, session_store=SqliteSessionStore(database_path=database_path))
        runtime.__exit__(None, None, None)
        assert external.poll() is None
    finally:
        external.terminate()
        external.wait(timeout=5)
