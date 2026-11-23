"""Core-lifetime ProcessHost: shared across generations, bounded, closed only by the owner."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from voidcode.core.plugins import PluginRuntime
from voidcode.core.processes import ProcessHost


def _python(*lines: str) -> list[str]:
    return [sys.executable, "-c", "\n".join(lines)]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.02)
    return False


def _wait_entry_exit(host: ProcessHost, process_id: str, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = host.existing(process_id)
        assert entry is not None, process_id
        if entry.process.poll() is not None:
            return
        time.sleep(0.02)
    raise AssertionError(f"background process {process_id} never exited")


def _runtime(tmp_path: Path, host: ProcessHost, revision: str) -> PluginRuntime:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return PluginRuntime((), workspace=workspace, revision_id=revision, process_host=host)


def test_generation_close_keeps_background_jobs_on_the_shared_host(tmp_path: Path) -> None:
    host = ProcessHost(spool_dir=tmp_path / "process-spool")
    first = _runtime(tmp_path, host, "gen-1")
    second = _runtime(tmp_path, host, "gen-2")
    handle = first.processes.spawn(
        _python("import time", "time.sleep(120)"),
        owner="exec",
        session_id="session-host",
        cwd=tmp_path,
        env=None,
        stdin=False,
        stdout_path=tmp_path / "shared.out",
        stderr_path=tmp_path / "shared.err",
    )
    try:
        assert first.process_host is host
        assert second.process_host is host
        assert first.processes.list_background(owner="exec", session_id="session-host")[0].process_id == handle.process_id

        first.close()
        assert _pid_alive(handle.pid), "generation close must not stop a background job"
        assert second.processes.list_background(owner="exec", session_id="session-host")[0].process_id == handle.process_id

        second.close()
        assert _pid_alive(handle.pid), "a second generation close must not stop the job either"
        assert host.cancel(handle.process_id, owner="exec", session_id="session-host") is True
    finally:
        host.stop_background()
    assert _wait_gone(handle.pid)


def test_exited_background_entries_are_bounded_per_owner_session(tmp_path: Path) -> None:
    host = ProcessHost(spool_dir=tmp_path / "process-spool", max_exited_per_session=2)
    runtime = _runtime(tmp_path, host, "gen-retention")
    live = None
    try:
        for index in range(4):
            handle = runtime.processes.spawn(
                _python("pass"),
                owner="exec",
                session_id="session-retention",
                cwd=tmp_path,
                env=None,
                stdin=False,
                stdout_path=tmp_path / f"exited-{index}.out",
                stderr_path=tmp_path / f"exited-{index}.err",
                process_id=f"exited-{index}",
            )
            _wait_entry_exit(host, handle.process_id)

        listed = runtime.processes.list_background(owner="exec", session_id="session-retention")
        assert [handle.process_id for handle in listed] == ["exited-2", "exited-3"]

        live = runtime.processes.spawn(
            _python("import time", "time.sleep(120)"),
            owner="exec",
            session_id="session-retention",
            cwd=tmp_path,
            env=None,
            stdin=False,
            stdout_path=tmp_path / "live.out",
            stderr_path=tmp_path / "live.err",
            process_id="live",
        )
        listed = runtime.processes.list_background(owner="exec", session_id="session-retention")
        assert [handle.process_id for handle in listed] == ["exited-2", "exited-3", "live"]
        assert _pid_alive(live.pid)
    finally:
        runtime.close()
        host.stop_background()
    if live is not None:
        assert _wait_gone(live.pid)
