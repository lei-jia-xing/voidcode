"""Host-lifetime process semantics: shutdown sweeps owned groups, swaps keep user jobs.

Bug 1: `stop_background` (owner shutdown) must kill TERM-ignoring group members even after
their leader exits — waiting on the leader alone leaves them behind. Bug 2: `reap_all`
(host swap cleanup) must never touch supervised background jobs, which keep running with
their persisted identities across a host generation.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from voidcode.plugin_host.processes import ProcessRegistry


def _python(*lines: str) -> list[str]:
    return [sys.executable, "-c", "\n".join(lines)]


def _wait_file(path: Path, *, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        time.sleep(0.02)
    raise AssertionError(f"{path} was never written")


def _wait_gone(pid: int, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = Path(f"/proc/{pid}/stat")
        if not state.exists():
            return True
        try:
            fields = state.read_text(encoding="utf-8").split()
            if len(fields) > 2 and fields[2] == "Z":
                return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_owner_shutdown_kills_term_ignoring_group_members(tmp_path: Path) -> None:
    registry = ProcessRegistry()
    kid_file = tmp_path / "kid.pid"
    kid = (
        "import os,pathlib,signal,time;"
        f"pathlib.Path({str(kid_file)!r}).write_text(str(os.getpid()));"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"
    )
    handle = registry.spawn(
        _python(
            "import subprocess,sys,time",
            f"subprocess.Popen([sys.executable,'-c',{kid!r}])",
            "time.sleep(120)",
        ),
        session_id="sess",
        cwd=tmp_path,
        env=None,
        stdin=False,
        stdout_path=tmp_path / "job.log",
        stderr_path=tmp_path / "job.err",
    )
    kid_pid = int(_wait_file(kid_file))
    assert os.getpgid(kid_pid) == os.getpgid(handle.pid)

    registry.stop_background()

    assert _wait_gone(handle.pid)
    assert _wait_gone(kid_pid)


def test_host_swap_cleanup_keeps_supervised_jobs_alive(tmp_path: Path) -> None:
    registry = ProcessRegistry()
    job = registry.spawn(
        _python("import time", "time.sleep(120)"),
        session_id="sess",
        cwd=tmp_path,
        env=None,
        stdin=False,
        stdout_path=tmp_path / "job.log",
        stderr_path=tmp_path / "job.err",
    )

    # A host swap runs exactly the generation cleanup: one-shot reaping plus service stops.
    registry.reap_all()
    registry.stop_services()

    assert _pid_alive(job.pid)
    assert registry.cancel(job.process_id, session_id="sess") is True
    assert _wait_gone(job.pid)


def test_owner_shutdown_sweeps_group_after_leader_was_reaped(tmp_path: Path) -> None:
    """A reaped leader must not hide its group: the pid is the pgid, so members stay sweepable."""
    registry = ProcessRegistry()
    kid_file = tmp_path / "kid.pid"
    kid = (
        "import os,pathlib,signal,time;"
        f"pathlib.Path({str(kid_file)!r}).write_text(str(os.getpid()));"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"
    )
    handle = registry.spawn(
        _python(
            "import subprocess,sys,time",
            f"subprocess.Popen([sys.executable,'-c',{kid!r}])",
            "time.sleep(0.2)",
        ),
        session_id="sess",
        cwd=tmp_path,
        env=None,
        stdin=False,
        stdout_path=tmp_path / "job.log",
        stderr_path=tmp_path / "job.err",
    )
    kid_pid = int(_wait_file(kid_file))
    assert _wait_gone(handle.pid)
    # A listing observes the dead leader, reaping the zombie and discarding its live pid entry.
    registry.list_background(session_id="sess")
    assert registry.background(handle.process_id, session_id="sess").process.returncode is not None

    registry.stop_background()

    assert _wait_gone(kid_pid)
