from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.core.turns import FinalTurn, ModelTurnFact, ToolTurn, TurnRequest
from voidcode.runtime.background.process import BackgroundProcessManager
from voidcode.runtime.config.loading import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.execution.process.background_process import BackgroundProcessCommand
from voidcode.runtime.permissions.policy import PermissionPolicy
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.tools.contracts import TextOutput, ToolCall, ToolFailure, ToolSuccess


def _body(result: ToolSuccess | ToolFailure) -> Any:
    assert result.body is not None
    return result.body


class _Runtime:
    def __init__(self, manager: BackgroundProcessManager) -> None:
        self.background_process_manager = manager


def _command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}"


def _invoke(runtime: _Runtime, context: ToolContext, **arguments: object) -> ToolSuccess | ToolFailure:
    return BackgroundProcessCommand(runtime=runtime).invoke(
        ToolCall(tool_name="background_process", arguments=arguments),
        context=context,
    )


def _start(runtime: _Runtime, context: ToolContext, script: str, *, ready_pattern: str = "READY") -> ToolSuccess:
    result = _invoke(
        runtime,
        context,
        op="start",
        command=_command(script),
        ready_pattern=ready_pattern,
        ready_timeout=5,
    )
    assert isinstance(result, ToolSuccess)
    assert _body(result).ready is True
    return result


def test_native_background_start_preserves_exact_command_identity_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "npm"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "print('ARGS_HEX=' + b'\\0'.join(arg.encode() for arg in sys.argv[1:]).hex(), flush=True)\n"
        "print('ENV_HEX=' + (os.environ.get('CI', '') + '\\0' + os.environ.get('NPM_CONFIG_YES', '')).encode().hex(), flush=True)\n"
        "print('STDIN_HEX=' + sys.stdin.buffer.read().hex(), flush=True)\n"
        "print('READY', flush=True)\n"
        "time.sleep(5)\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("CI", "background-caller-value")
    monkeypatch.setenv("NPM_CONFIG_YES", "background-caller-no")
    manager = BackgroundProcessManager(workspace=tmp_path)
    runtime = _Runtime(manager)
    context = ToolContext(workspace=tmp_path, session_id="owner")
    command = "\nnpm 'arg with spaces' <<'EOF'\nfirst\nsecond\nEOF\n"
    try:
        started = _invoke(runtime, context, op="start", command=command, ready_pattern="READY", ready_timeout=5)
        assert isinstance(started, ToolSuccess)
        first_body = _body(started)
        assert first_body.command == command
        assert first_body.reused is False

        reused = _invoke(runtime, context, op="start", command=command, ready_pattern="READY", ready_timeout=5)
        assert isinstance(reused, ToolSuccess)
        reused_body = _body(reused)
        assert reused_body.process_id == first_body.process_id
        assert reused_body.reused is True

        distinct_command = command + "\n"
        distinct = _invoke(runtime, context, op="start", command=distinct_command, ready_pattern="READY", ready_timeout=5)
        assert isinstance(distinct, ToolSuccess)
        distinct_body = _body(distinct)
        assert distinct_body.command == distinct_command
        assert distinct_body.process_id != first_body.process_id
        assert distinct_body.reused is False

        logs = _invoke(runtime, context, op="logs", process_id=first_body.process_id, stream="stdout")
        assert isinstance(logs, ToolSuccess)
        log_text = logs.output.text
        assert "ARGS_HEX=" + b"arg with spaces".hex() in log_text
        assert "ENV_HEX=" + b"background-caller-value\0background-caller-no".hex() in log_text
        assert "STDIN_HEX=" + b"first\nsecond\n".hex() in log_text
        assert "READY" in log_text

        for process_id in (first_body.process_id, distinct_body.process_id):
            stopped = _invoke(runtime, context, op="stop", process_id=process_id)
            assert isinstance(stopped, ToolSuccess)
    finally:
        manager.stop_all()


def test_native_process_logs_page_both_streams_and_send_utf8_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    manager = BackgroundProcessManager(workspace=tmp_path)
    runtime = _Runtime(manager)
    context = ToolContext(workspace=tmp_path, session_id="owner")
    script = (
        "import sys\n"
        "for index in range(700):\n"
        " print(f'OUT-{index:03d}-é', flush=True)\n"
        " print(f'ERR-{index:03d}-é', file=sys.stderr, flush=True)\n"
        "print('READY', flush=True)\n"
        "for value in sys.stdin:\n"
        " print(f'INPUT_BYTES:{len(value.encode(\"utf-8\"))}', flush=True)\n"
    )
    started = _start(runtime, context, script)
    process_id = _body(started).process_id
    try:
        first = _invoke(
            runtime,
            context,
            op="logs",
            process_id=process_id,
            stream="both",
            offset=0,
            limit=500,
        )
        assert isinstance(first, ToolSuccess)
        first_body = _body(first)
        assert first_body.stdout.startswith("OUT-000-é\n")
        assert first_body.stderr.startswith("ERR-000-é\n")
        assert first_body.stdout_retained_lines == first_body.stderr_retained_lines == 500
        assert first_body.next_offsets == {"stdout": 500, "stderr": 500}
        assert first_body.next_offset is None
        first_output = first.output
        assert isinstance(first_output, TextOutput)
        assert "[stdout]\n" in first_output.text and "[stderr]\n" in first_output.text
        assert "\\n" not in first_output.text
        assert first_body.stdout_artifact is not None
        assert first_body.stderr_artifact is not None

        try:
            _invoke(
                runtime,
                ToolContext(workspace=tmp_path, session_id="foreign"),
                op="logs",
                process_id=process_id,
                stream="stdout",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("foreign session read a background process log")

        sent = _invoke(runtime, context, op="send", process_id=process_id, input="café", newline=True)
        assert isinstance(sent, ToolSuccess)
        sent_body = _body(sent)
        assert sent_body.requested_bytes == len("café\n".encode())
        assert sent_body.written_bytes == sent_body.requested_bytes
        assert sent_body.error is None

        state = manager.load(process_id, workspace=tmp_path, owner_session_id="owner", enforce_owner=True)
        assert state is not None and state.stdout_path is not None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "INPUT_BYTES:6" not in state.stdout_path.read_text(encoding="utf-8"):
            time.sleep(0.02)
        assert "INPUT_BYTES:6" in state.stdout_path.read_text(encoding="utf-8")

        second = _invoke(
            runtime,
            context,
            op="logs",
            process_id=process_id,
            stream="stdout",
            offset=500,
            limit=500,
        )
        assert isinstance(second, ToolSuccess)
        second_body = _body(second)
        second_output = second.output
        assert isinstance(second_output, TextOutput)
        assert second_body.stdout.startswith("OUT-500-é\n")
        assert second_body.next_offset is None
        assert second_body.stdout_artifact is not None
        assert second_body.stdout_artifact["sha256"] != first_body.stdout_artifact["sha256"]
        assert "Full snapshot:" in second_output.text
    finally:
        manager.stop_all()


def test_native_process_logs_clip_long_lines_and_do_not_create_eof_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    manager = BackgroundProcessManager(workspace=tmp_path)
    runtime = _Runtime(manager)
    context = ToolContext(workspace=tmp_path, session_id="owner")
    started = _start(
        runtime,
        context,
        "import sys\nsys.stdout.write('x' * 50000 + '\\nend\\n')\nprint('READY', flush=True)\nsys.stdin.readline()\n",
    )
    process_id = _body(started).process_id
    try:
        first = _invoke(runtime, context, op="logs", process_id=process_id, stream="stdout", offset=0, limit=500)
        assert isinstance(first, ToolSuccess)
        first_body = _body(first)
        assert len(first_body.stdout) == 40 * 1024
        assert first_body.clipped_streams == ("stdout",)
        assert first_body.next_offset == 1
        assert "long line was clipped" in first_body.guidance

        second = _invoke(runtime, context, op="logs", process_id=process_id, stream="stdout", offset=1, limit=500)
        assert isinstance(second, ToolSuccess)
        second_body = _body(second)
        assert second_body.stdout == "end\nREADY\n"
        assert second_body.next_offset is None
    finally:
        manager.stop_all()


def test_native_process_send_is_nonblocking_and_reports_partial_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    manager = BackgroundProcessManager(workspace=tmp_path)
    runtime = _Runtime(manager)
    context = ToolContext(workspace=tmp_path, session_id="owner")
    started = _start(runtime, context, "import time\nprint('READY', flush=True)\ntime.sleep(10)\n")
    process_id = _body(started).process_id
    try:
        payload = "x" * (4 * 1024 * 1024)
        before = time.monotonic()
        sent = _invoke(runtime, context, op="send", process_id=process_id, input=payload, newline=True)
        elapsed = time.monotonic() - before
        assert isinstance(sent, ToolFailure)
        assert elapsed < 1
        sent_body = _body(sent)
        assert 0 <= sent_body.written_bytes < sent_body.requested_bytes
        assert sent_body.requested_bytes == len((payload + "\n").encode())
        assert sent_body.error
    finally:
        manager.stop_all()


def test_native_process_send_reports_closed_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    manager = BackgroundProcessManager(workspace=tmp_path)
    runtime = _Runtime(manager)
    context = ToolContext(workspace=tmp_path, session_id="owner")
    started = _start(runtime, context, "import os, time\nos.close(0)\nprint('READY', flush=True)\ntime.sleep(5)\n")
    process_id = _body(started).process_id
    try:
        sent = _invoke(runtime, context, op="send", process_id=process_id, input="café", newline=True)
        assert isinstance(sent, ToolFailure)
        sent_body = _body(sent)
        assert sent_body.requested_bytes == len("café\n".encode())
        assert sent_body.written_bytes == 0
        assert sent_body.error
    finally:
        manager.stop_all()


def test_stop_reports_failure_when_termination_is_unconfirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    manager = BackgroundProcessManager(workspace=tmp_path)
    runtime = _Runtime(manager)
    context = ToolContext(workspace=tmp_path, session_id="owner")
    started = _start(runtime, context, "import time\nprint('READY', flush=True)\ntime.sleep(60)\n")
    process_id = _body(started).process_id
    state = manager.load(process_id, workspace=tmp_path, owner_session_id="owner", enforce_owner=True)
    assert state is not None
    monkeypatch.setattr("voidcode.runtime.background.process._terminate_background_process_group", lambda _process: False)
    try:
        with pytest.raises(ValueError, match="termination is unconfirmed"):
            _invoke(runtime, context, op="stop", process_id=process_id)
    finally:
        state.process.kill()
        state.process.wait()
        manager.stop_all()


class _StartProcessProducer:
    def __init__(self, command: str) -> None:
        self.command = command

    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object) -> FinalTurn | ToolTurn:
        _ = session
        fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
        if tool_results:
            return FinalTurn(output="started", facts=(fact,))
        return ToolTurn(
            calls=(
                ToolCall(
                    tool_name="background_process",
                    arguments={
                        "op": "start",
                        "command": self.command,
                        "ready_pattern": "READY",
                        "ready_timeout": 5,
                    },
                ),
            ),
            facts=(fact,),
        )


def _service_runtime(workspace: Path, command: str) -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=workspace,
        turn_producer=_StartProcessProducer(command),
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False)),
        permission_policy=PermissionPolicy(mode="yolo"),
    )


def _proc_state(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    marker = stat.rfind(")")
    return stat[marker + 2 : marker + 3] if marker >= 0 else None


@pytest.mark.skipif(sys.platform == "win32", reason="uses Linux /proc process state")
def test_explicit_stop_reaps_owned_process_group_and_restart_does_not_reattach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("VOIDCODE_DB_PATH", raising=False)
    command = _command(
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
        "print(f'PARENT_PID={os.getpid()}', flush=True)\n"
        "print(f'CHILD_PID={child.pid}', flush=True)\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    session_id = "native-process-owner"
    runtime = _service_runtime(tmp_path, command)
    closed = False
    try:
        tuple(runtime.run_stream(RuntimeRequest(prompt="start process", session_id=session_id)))
        states = runtime.background_process_manager.list_processes(
            workspace=tmp_path,
            owner_session_id=session_id,
            enforce_owner=True,
        )
        assert len(states) == 1 and states[0].stdout_path is not None
        output = states[0].stdout_path.read_text(encoding="utf-8")
        parent_pid = int(next(line.removeprefix("PARENT_PID=") for line in output.splitlines() if line.startswith("PARENT_PID=")))
        child_pid = int(next(line.removeprefix("CHILD_PID=") for line in output.splitlines() if line.startswith("CHILD_PID=")))
        process_id = states[0].process_id
        stopped = BackgroundProcessCommand(runtime=runtime).invoke(
            ToolCall(tool_name="background_process", arguments={"op": "stop", "process_id": process_id}),
            context=ToolContext(workspace=tmp_path, session_id=session_id),
        )
        assert isinstance(stopped, ToolSuccess)
        assert _body(stopped).running is False
        runtime.__exit__(None, None, None)
        closed = True
        for pid in (parent_pid, child_pid):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and _proc_state(pid) not in {None, "Z", "X"}:
                time.sleep(0.02)
            assert _proc_state(pid) in {None, "Z", "X"}
    finally:
        if not closed:
            runtime.__exit__(None, None, None)
    restarted = _service_runtime(tmp_path, command)
    try:
        stale = restarted.background_process_manager.load(
            process_id,
            workspace=tmp_path,
            owner_session_id=session_id,
            enforce_owner=True,
        )
        assert stale is not None and stale.prior_runtime
        assert stale.process.stdin is None
        result = BackgroundProcessCommand(runtime=restarted).invoke(
            ToolCall(
                tool_name="background_process",
                arguments={"op": "send", "process_id": process_id, "input": "must not reattach"},
            ),
            context=ToolContext(workspace=tmp_path, session_id=session_id),
        )
        assert isinstance(result, ToolFailure)
        assert _body(result).prior_runtime is True
    finally:
        restarted.__exit__(None, None, None)
