"""S5/7.6 acceptance: the lease-scoped process service owns flags, bounds, groups, and stance.

The runtime runs the child, so no output crosses a wire protocol; what the core contributes is
policy, and the tests below drive real processes through the real runtime: a positive run with
stdin, a bounded capture, a timeout, a session cancel that kills the whole group, the read-only and
hook refusals, and shutdown reaping.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import PROBE_TOOL, PROCESS_READ_TOOL, PROCESS_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.activation import SandboxPolicy
from voidcode.core.engine import Core
from voidcode.core.plugins import LeaseContext
from voidcode.core.processes import ProcessRegistry
from voidcode.core.sandbox import SandboxBackend
from voidcode.core.services import RuntimeServices
from voidcode.sdk.services import ProcessResult

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "process reasoning", "tool_calls": list(calls)},
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


_CREATED: list[Core] = []


@pytest.fixture(autouse=True)
def _close_cores() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _open(tmp_path: Path) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    core = open_core(workspace, state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo"},
                "providers": {
                    "endpoint": {
                        "api_key": "loopback-secret-not-for-storage",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
            }
        ),
        encoding="utf-8",
    )


def _tool_messages(server: OpenAILoopbackServer, index: int = 1) -> list[str]:
    messages = server.requests[index].get("messages")
    assert isinstance(messages, list)
    return [str(message.get("content", "")) for message in messages if isinstance(message, Mapping) and message.get("role") == "tool"]


def _tool_payloads(server: OpenAILoopbackServer, index: int = 1) -> list[dict[str, object]]:
    request = server.requests[index]
    messages = request.get("messages")
    assert isinstance(messages, list)
    payloads: list[dict[str, object]] = []
    for message in messages:
        if isinstance(message, Mapping) and message.get("role") == "tool":
            payloads.append(json.loads(str(message.get("content", ""))))
    return payloads


def _pid_alive(pid: int, *, marker: str | None = None) -> bool:
    """Whether the process started by this test is still alive.

    A bare `kill(pid, 0)` answers for whoever holds the number *now*: under the parallel
    suite a reaped test process's pid is routinely reused within seconds, which turns this
    probe into a false "still alive" forever. The marker (the test's own unique pid-file
    path, embedded in the sleeper's `python -c` command line) re-verifies the identity, so
    a recycled pid counts as exited.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if marker is not None:
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return False
        if marker not in command:
            return False
    return True


#: A full bundled cold start, a service spawn, and a group kill/reap all need more than
#: the old 10 s bound when the suite runs under parallel xdist workers.
_STARTUP_DEADLINE_SECONDS = 60.0


def _wait_for_file(path: Path, *, timeout: float = _STARTUP_DEADLINE_SECONDS) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        time.sleep(0.02)
    raise AssertionError(f"{path} was never written")


def _wait_for_pid_exit(pid: int, *, marker: str | None = None, timeout: float = _STARTUP_DEADLINE_SECONDS) -> bool:
    """A killed group leader can linger briefly as a zombie; poll until it is really gone."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid, marker=marker):
            return True
        if not Path(f"/proc/{pid}").exists():
            return True
        state = Path(f"/proc/{pid}/stat")
        if state.exists():
            fields = state.read_text(encoding="utf-8").split()
            if len(fields) > 2 and fields[2] == "Z":
                return True
        time.sleep(0.05)
    return False


def _python(*lines: str) -> list[str]:
    return [sys.executable, "-c", "\n".join(lines)]


def test_run_process_returns_stdout_stderr_exit_code_and_stdin(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", process_tool=True)
    script = _python(
        "import sys",
        "payload = sys.stdin.read()",
        "print('out:' + payload)",
        "print('err:' + payload, file=sys.stderr)",
        "sys.exit(7)",
    )
    replies = [
        _turn(_call("run-1", PROCESS_TOOL, argv=script, stdin="hello")),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-process")
        assert core.run("run it", session_id="sess-process").status == "completed"

        payload = _tool_payloads(server)[0]
        assert payload["exit_code"] == 7
        assert payload["stdout"] == "out:hello\n"
        assert payload["stderr"] == "err:hello\n"
        assert payload["truncated"] is False
        assert payload["timed_out"] is False
        assert payload["cancelled"] is False
        assert payload["has_duration"] is True


def test_run_process_bounds_each_stream_and_reports_truncation(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", process_tool=True)
    script = _python("import sys", "sys.stdout.write('x' * 5000)", "sys.stderr.write('y' * 5000)")
    replies = [
        _turn(_call("run-1", PROCESS_TOOL, argv=script, max_output_bytes=100)),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-process-bounds")
        assert core.run("run it", session_id="sess-process-bounds").status == "completed"

        payload = _tool_payloads(server)[0]
        assert payload["stdout"] == "x" * 100
        assert payload["stderr"] == "y" * 100
        assert payload["truncated"] is True
        assert payload["exit_code"] == 0


def test_run_process_timeout_kills_the_whole_group(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", process_tool=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    leader_pid_file = workspace / "leader.pid"
    grandchild_pid_file = workspace / "grandchild.pid"
    grandchild_script = f"import os, pathlib, time;pathlib.Path({str(grandchild_pid_file)!r}).write_text(str(os.getpid()));time.sleep(120)"
    script = _python(
        "import os, subprocess, sys, time",
        "from pathlib import Path",
        f"subprocess.Popen([sys.executable, '-c', {grandchild_script!r}])",
        f"Path({str(leader_pid_file)!r}).write_text(str(os.getpid()))",
        "sys.stdout.write('partial stdout before the timeout\\n')",
        "sys.stdout.flush()",
        "time.sleep(120)",
    )
    replies = [
        _turn(_call("run-1", PROCESS_TOOL, argv=script, timeout_seconds=1)),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(workspace, server, source)
        core = _open(tmp_path)
        core.create_session("sess-process-timeout")
        assert core.run("run it", session_id="sess-process-timeout").status == "completed"

        payload = _tool_payloads(server)[0]
        assert payload["timed_out"] is True
        assert payload["cancelled"] is False
        # A stopped process still reports what it had already written.
        assert payload["stdout"] == "partial stdout before the timeout\n"
        assert payload["exit_code"] is None

        leader_pid = int(_wait_for_file(leader_pid_file))
        grandchild_pid = int(_wait_for_file(grandchild_pid_file))
        # The whole group dies, not just the process the runtime spawned. Each probe carries the
        # sleeper's own unique command line, so a recycled pid cannot read as still alive.
        assert _wait_for_pid_exit(leader_pid, marker=str(leader_pid_file))
        assert _wait_for_pid_exit(grandchild_pid, marker=str(grandchild_pid_file))


def test_session_cancel_kills_the_process_group_and_reports_cancellation(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", process_tool=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    leader_pid_file = workspace / "leader.pid"
    script = _python(
        "import os, time",
        "from pathlib import Path",
        f"Path({str(leader_pid_file)!r}).write_text(str(os.getpid()))",
        "time.sleep(120)",
    )
    replies = [_turn(_call("run-1", PROCESS_TOOL, argv=script)), _turn(output="done")]
    with _server(replies) as server:
        _configure(workspace, server, source)
        core = _open(tmp_path)
        core.create_session("sess-process-cancel")
        worker = threading.Thread(target=lambda: core.run("run it", session_id="sess-process-cancel"), daemon=True)
        worker.start()
        leader_pid = int(_wait_for_file(leader_pid_file))

        assert core.cancel_session("sess-process-cancel", reason="test cancel") is True
        worker.join(timeout=_STARTUP_DEADLINE_SECONDS)
        assert not worker.is_alive()
        assert _wait_for_pid_exit(leader_pid, marker=str(leader_pid_file))


def test_a_cancelled_run_reports_what_the_child_had_already_written(tmp_path: Path) -> None:
    """Session cancel kills the group inside the core's window, and the bytes survive it."""
    registry = ProcessRegistry()
    cancel = threading.Event()
    result: list[ProcessResult] = []

    def drive() -> None:
        result.append(
            registry.run(
                _python("import time", "print('partial stdout before the cancel', flush=True)", "time.sleep(120)"),
                cwd=tmp_path,
                env=None,
                stdin=None,
                timeout_seconds=None,
                max_output_bytes=4096,
                cancel_event=cancel,
            )
        )

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    time.sleep(0.5)
    started = time.monotonic()
    cancel.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert time.monotonic() - started < 0.75 + 0.5
    assert result[0].cancelled is True
    assert result[0].exit_code is None
    assert result[0].stdout == "partial stdout before the cancel\n"
    assert registry.live_group_count() == 0


def test_read_only_lease_refuses_to_run_a_process(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", process_tool=True)
    replies = [
        _turn(_call("run-1", PROCESS_READ_TOOL, argv=_python("print('should not run')"))),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-process-readonly")
        assert core.run("try it", session_id="sess-process-readonly").status == "completed"

        payload = _tool_payloads(server)[0]
        assert payload["refused"].startswith("PermissionError:")
        assert "read-only" in payload["refused"]


def test_hook_lease_refuses_to_run_a_process(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(
        source,
        tool_output="v1",
        post_tool_hook=True,
        post_tool_process=True,
        post_tool_match=(PROBE_TOOL,),
    )
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="done")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-process-hook")
        assert core.run("write something", session_id="sess-process-hook").status == "completed"

        tool_text = "\n".join(_tool_messages(server))
        assert "process refused: PermissionError" in tool_text
        assert "process allowed" not in tool_text


def test_host_close_reaps_live_process_groups(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", process_tool=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    pid_file = workspace / "survivor.pid"
    script = _python(
        "import os, time",
        "from pathlib import Path",
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))",
        "time.sleep(120)",
    )
    replies = [_turn(_call("run-1", PROCESS_TOOL, argv=script)), _turn(output="done")]
    with _server(replies) as server:
        _configure(workspace, server, source)
        core = _open(tmp_path)
        core.create_session("sess-process-shutdown")

        def drive() -> None:
            try:
                core.run("run it", session_id="sess-process-shutdown")
            except RuntimeError:
                # Closing the core under a live run is the point of this test.
                pass

        worker = threading.Thread(target=drive, daemon=True)
        worker.start()
        pid = int(_wait_for_file(pid_file))

        core.close()
        worker.join(timeout=_STARTUP_DEADLINE_SECONDS)
        assert _wait_for_pid_exit(pid, marker=str(pid_file))


def test_read_only_lease_allows_run_query_process_and_hook_lease_rejects_both(tmp_path: Path) -> None:
    core = _open(tmp_path)
    runtime = core._snapshot.plugin_runtime

    ro_lease = LeaseContext(
        token="ro-token",
        owner="test-plugin",
        session_id="sess-ro",
        workspace=core.workspace,
        run_id="run-ro",
        revision_id=core._snapshot.admission.revision_id,
        invocation_id="inv-ro",
        readonly=True,
    )
    ro_services = RuntimeServices(runtime, ro_lease)

    with pytest.raises(PermissionError, match="read-only"):
        ro_services.run_process(_python("print('should not run')"))

    result = ro_services.run_query_process(_python("print('query ok')"))
    assert result.exit_code == 0
    assert result.stdout.strip() == "query ok"
    assert len(ro_lease.staged_observations) == 0

    for ro in (True, False):
        hook_lease = LeaseContext(
            token=f"hook-token-{ro}",
            owner="test-plugin",
            session_id=f"sess-hook-{ro}",
            workspace=core.workspace,
            run_id=f"run-hook-{ro}",
            revision_id=core._snapshot.admission.revision_id,
            invocation_id=f"inv-hook-{ro}",
            readonly=ro,
            hook=True,
        )
        hook_services = RuntimeServices(runtime, hook_lease)
        with pytest.raises(PermissionError):
            hook_services.run_process(_python("print('should not run')"))
        with pytest.raises(PermissionError, match="hook"):
            hook_services.run_query_process(_python("print('should not run')"))


def test_run_query_process_enforces_read_only_sandbox_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    core = _open(tmp_path)
    runtime = core._snapshot.plugin_runtime
    ro_lease = LeaseContext(
        token="ro-token-sb",
        owner="test-plugin",
        session_id="sess-ro-sb",
        workspace=core.workspace,
        run_id="run-ro-sb",
        revision_id=core._snapshot.admission.revision_id,
        invocation_id="inv-ro-sb",
        readonly=True,
    )
    fake_backend = SandboxBackend(
        policy=SandboxPolicy(mode="workspace-write", writable_roots=("/tmp",)),
        helper_path="/bin/true",
        protected_paths=(),
    )
    runtime.sandbox_backend = fake_backend

    recorded_sandbox: list[SandboxBackend | None] = []

    def _intercept_run(*args: object, **kwargs: object) -> ProcessResult:
        recorded_sandbox.append(kwargs.get("sandbox"))  # type: ignore[arg-type]
        return ProcessResult(exit_code=0, stdout="intercepted", stderr="", duration_ms=1)

    monkeypatch.setattr(runtime.processes, "run", _intercept_run)
    services = RuntimeServices(runtime, ro_lease)
    res = services.run_query_process(["echo", "hi"])
    assert res.stdout == "intercepted"
    assert len(recorded_sandbox) == 1
    passed_sb = recorded_sandbox[0]
    assert passed_sb is not None
    assert passed_sb.policy.mode == "read-only"
    assert passed_sb.policy.writable_roots == ()
