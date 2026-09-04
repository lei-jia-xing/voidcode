from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.background.process import (
    _MAX_BACKGROUND_PROCESS_LOG_LINES,
    BackgroundProcessManager,
    _DetachedProcess,
    _terminate_background_process_group,
)
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools import ToolCall
from voidcode.tools.process.background_process import _MAX_BACKGROUND_PROCESS_ROWS
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def test_background_process_operations_have_read_execute_classes(tmp_path: Path) -> None:
    from voidcode.runtime.permission_context import operation_class_for_tool

    runtime = VoidCodeRuntime(workspace=tmp_path)
    tool = runtime._base_tool_registry.resolve("background_process")
    assert operation_class_for_tool("background_process", False, tool_instance=tool, arguments={"op": "ps"}) == "read"
    assert operation_class_for_tool("background_process", False, tool_instance=tool, arguments={"op": "logs", "process_id": "proc"}) == "read"
    for operation in ("start", "send", "stop"):
        assert operation_class_for_tool("background_process", False, tool_instance=tool, arguments={"op": operation}) == "execute"
    runtime.__exit__(None, None, None)


def _call(tool, op: str, workspace: Path, **arguments: object):
    return tool.invoke(ToolCall(tool_name="background_process", arguments={"op": op, **arguments}), workspace=workspace)


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


def test_background_process_is_only_registered_model_surface(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    registry = runtime._base_tool_registry
    assert registry.resolve("background_process").definition.name == "background_process"
    for old_name in ("background_process_start", "background_process_logs", "background_process_send", "background_process_stop"):
        with pytest.raises(ValueError, match="unknown tool"):
            registry.resolve(old_name)
    runtime.__exit__(None, None, None)


def test_background_process_strict_op_schema(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    tool = runtime._base_tool_registry.resolve("background_process")
    for arguments in ({"op": "invalid"}, {"op": "ps", "extra": True}, {"op": "start"}, {"op": "logs", "process_id": " "}):
        with pytest.raises(ValueError, match="background_process"):
            tool.invoke(ToolCall(tool_name="background_process", arguments=arguments), workspace=tmp_path)
    runtime.__exit__(None, None, None)


def test_background_process_start_logs_and_stop(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    tool = runtime._base_tool_registry.resolve("background_process")
    command = f'"{sys.executable}" -c "import time; print(\'ready\', flush=True); time.sleep(5)"'
    started = _call(tool, "start", tmp_path, command=command)
    process_id = str(started.data["process_id"])
    assert started.status == "ok"
    logs = None
    _wait_for(lambda: "ready" in (setattr_and_return(tool, "logs", tmp_path, process_id) or ""))
    logs = _call(tool, "logs", tmp_path, process_id=process_id)
    assert logs.status == "ok"
    assert "ready" in (logs.content or "")
    stopped = _call(tool, "stop", tmp_path, process_id=process_id)
    assert stopped.status == "ok"
    assert stopped.data["running"] is False
    runtime.__exit__(None, None, None)


def setattr_and_return(tool, op: str, workspace: Path, process_id: str) -> str:
    result = _call(tool, op, workspace, process_id=process_id)
    return result.content or ""


def test_background_process_start_reuses_exact_command(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    tool = runtime._base_tool_registry.resolve("background_process")
    command = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    first = _call(tool, "start", tmp_path, command=command)
    second = _call(tool, "start", tmp_path, command=f"  {command}  ")
    assert first.data["process_id"] == second.data["process_id"]
    assert second.data["reused"] is True
    _call(tool, "stop", tmp_path, process_id=str(first.data["process_id"]))
    runtime.__exit__(None, None, None)


def test_background_process_logs_are_bounded_and_artifact_backed(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    tool = runtime._base_tool_registry.resolve("background_process")
    line_count = _MAX_BACKGROUND_PROCESS_LOG_LINES + 25
    command = f'"{sys.executable}" -c "import sys; [sys.stdout.write(\'line-%d\\n\' % i) for i in range({line_count})]"'
    started = _call(tool, "start", tmp_path, command=command)
    process_id = str(started.data["process_id"])
    _wait_for(lambda: _call(tool, "logs", tmp_path, process_id=process_id).data.get("stdout_retained_lines") == _MAX_BACKGROUND_PROCESS_LOG_LINES)
    result = _call(tool, "logs", tmp_path, process_id=process_id)
    assert result.data["stdout_retained_lines"] == _MAX_BACKGROUND_PROCESS_LOG_LINES
    assert result.data["stdout_dropped_lines"] >= 25
    assert result.data["truncated"] is True
    assert "line-0" not in str(result.data["stdout"])
    assert f"line-{line_count - 1}" in str(result.data["stdout"])
    assert result.reference and result.reference.startswith("voidcode://artifact/")
    runtime.__exit__(None, None, None)


def test_background_process_send_writes_stdin(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    tool = runtime._base_tool_registry.resolve("background_process")
    command = (
        f'"{sys.executable}" -c "import sys,time; print(\'ready\', flush=True); '
        "print('echo:'+sys.stdin.readline().strip(), flush=True); time.sleep(30)\""
    )
    started = _call(tool, "start", tmp_path, command=command)
    process_id = str(started.data["process_id"])
    result = _call(tool, "send", tmp_path, process_id=process_id, input="hello")
    assert result.status == "ok"
    assert result.data["input"] == "hello"
    assert result.data["newline"] is True
    _call(tool, "stop", tmp_path, process_id=process_id)
    runtime.__exit__(None, None, None)


def test_background_process_ps_is_owner_workspace_scoped_and_bounded(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path)
    tool = runtime._base_tool_registry.resolve("background_process")
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    owner = RuntimeToolInvocationContext(session_id="owner-a")
    other = RuntimeToolInvocationContext(session_id="owner-b")
    with bind_runtime_tool_context(owner):
        started = _call(tool, "start", tmp_path, command=command)
        process_id = str(started.data["process_id"])
        listing = _call(tool, "ps", tmp_path)
    assert listing.data["count"] == 1
    assert listing.data["processes"][0]["process_id"] == process_id
    with bind_runtime_tool_context(other):
        assert _call(tool, "ps", tmp_path).data["count"] == 0
    for index in range(_MAX_BACKGROUND_PROCESS_ROWS + 5):
        _call(tool, "start", tmp_path, command=f"{command} # {index}")
    assert len(_call(tool, "ps", tmp_path).data["processes"]) <= _MAX_BACKGROUND_PROCESS_ROWS
    with bind_runtime_tool_context(owner):
        _call(tool, "stop", tmp_path, process_id=process_id)
    runtime.__exit__(None, None, None)


def test_background_process_stale_rows_are_visible_but_not_controllable(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    external = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, start_new_session=True)
    try:
        store.register_background_process(
            workspace=tmp_path,
            process_id="proc-stale",
            owner_session_id="owner-a",
            command="external",
            cwd=str(tmp_path.resolve()),
            pid=external.pid,
            process_group_id=external.pid,
            process_identity="not-current",
            stdout_path="",
            stderr_path="",
        )
        runtime = VoidCodeRuntime(workspace=tmp_path, session_store=SqliteSessionStore(database_path=database_path))
        tool = runtime._base_tool_registry.resolve("background_process")
        with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="owner-a")):
            listing = _call(tool, "ps", tmp_path)
            logs = _call(tool, "logs", tmp_path, process_id="proc-stale")
        row = listing.data["processes"][0]
        assert row["status"] == "stale"
        assert row["controllable"] is False
        assert row["running"] is None
        assert logs.status == "error"
        assert logs.data["controllable"] is False
        runtime.__exit__(None, None, None)
    finally:
        external.terminate()
        external.wait(timeout=5)


def test_background_process_reconcile_rejects_external_log_paths(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    external_log = tmp_path.parent / "outside-background-process.log"
    external_log.write_text("do-not-leak\n", encoding="utf-8")
    store = SqliteSessionStore(database_path=database_path)
    store.register_background_process(
        workspace=tmp_path,
        process_id="proc-malicious-path",
        owner_session_id="owner-a",
        command="external",
        cwd=str(tmp_path.resolve()),
        pid=99999999,
        process_group_id=99999999,
        process_identity="not-current",
        stdout_path=str(external_log),
        stderr_path=str(external_log),
    )

    manager = BackgroundProcessManager(
        persistence=SqliteSessionStore(database_path=database_path),
        workspace=tmp_path,
    )
    state = manager.load("proc-malicious-path", workspace=tmp_path)
    assert state is not None
    assert state.stdout_path is None
    assert state.stderr_path is None
    assert state.stdout_chunks == []
    assert state.stderr_chunks == []

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=SqliteSessionStore(database_path=database_path),
    )
    try:
        tool = runtime._base_tool_registry.resolve("background_process")
        with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="owner-a")):
            result = _call(tool, "logs", tmp_path, process_id="proc-malicious-path")
        assert result.status == "error"
        assert "do-not-leak" not in (result.content or "")
        assert "do-not-leak" not in str(result.data)
    finally:
        runtime.__exit__(None, None, None)


def test_runtime_exit_stops_managed_background_processes(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    runtime = VoidCodeRuntime(workspace=tmp_path, session_store=SqliteSessionStore(database_path=database_path))
    tool = runtime._base_tool_registry.resolve("background_process")
    started = _call(tool, "start", tmp_path, command=f'"{sys.executable}" -c "import time; time.sleep(30)"')
    process_id = str(started.data["process_id"])
    runtime.__exit__(None, None, None)
    persisted = SqliteSessionStore(database_path=database_path).load_background_process(workspace=tmp_path, process_id=process_id)
    assert persisted is not None
    assert persisted["status"] == "exited"


@pytest.mark.skipif(os.name == "nt", reason="posix-only process group behavior")
def test_terminate_background_process_group_sends_sigkill_after_leader_exits(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr("voidcode.runtime.background.process.os.killpg", fake_killpg)
    monkeypatch.setattr("voidcode.runtime.background.process._process_group_exists", lambda _: group_exists)
    _terminate_background_process_group(cast(subprocess.Popen[str], _FakeProcess()))
    assert calls == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]
    assert waits == [1]


def test_detached_process_wait_rejects_unknown_exit_status() -> None:
    process = _DetachedProcess(pid=12345, exit_code=None)
    assert process.poll() is None
    with pytest.raises(RuntimeError, match="cannot be waited on"):
        process.wait(timeout=0.1)
