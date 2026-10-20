"""Service-process acceptance: owned trees die, detached jobs live, sessions stay apart.

A service child joins the host's group; cancellation records the tree before TERM and KILLs
surviving owned descendants (including after the leader exits). Host retirement stops every
service. Ordinary background jobs keep their own groups and their survival contract.
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


def test_service_cancel_kills_term_ignoring_descendants(tmp_path: Path) -> None:
    registry = ProcessRegistry()
    leader_file = tmp_path / "leader.pid"
    grandchild_file = tmp_path / "grandchild.pid"
    grandchild = (
        "import os,pathlib,signal,time;"
        f"pathlib.Path({str(grandchild_file)!r}).write_text(str(os.getpid()));"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"
    )
    handle = registry.spawn(
        _python(
            "import os,pathlib,signal,subprocess,sys,time",
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)",
            f"subprocess.Popen([sys.executable,'-c',{grandchild!r}])",
            f"pathlib.Path({str(leader_file)!r}).write_text(str(os.getpid()));time.sleep(120)",
        ),
        session_id="sess",
        cwd=tmp_path,
        env=None,
        stdin=True,
        stdout_path=tmp_path / "svc.log",
        stderr_path=tmp_path / "svc.err",
        service=True,
    )
    assert os.getpgid(handle.pid) == os.getpgid(os.getpid())
    leader = int(_wait_file(leader_file))
    descendant = int(_wait_file(grandchild_file))

    assert registry.cancel(handle.process_id, session_id="sess") is True
    assert _wait_gone(leader)
    assert _wait_gone(descendant)


def test_service_cancel_sweeps_descendants_after_leader_exit(tmp_path: Path) -> None:
    registry = ProcessRegistry()
    grandchild_file = tmp_path / "orphan.pid"
    grandchild = (
        "import os,pathlib,signal,time;"
        f"pathlib.Path({str(grandchild_file)!r}).write_text(str(os.getpid()));"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"
    )
    handle = registry.spawn(
        _python(
            "import os,pathlib,subprocess,sys",
            f"child=subprocess.Popen([sys.executable,'-c',{grandchild!r}]);child.wait(timeout=1)",
        ),
        session_id="sess",
        cwd=tmp_path,
        env=None,
        stdin=True,
        stdout_path=tmp_path / "svc.log",
        stderr_path=tmp_path / "svc.err",
        service=True,
    )
    descendant = int(_wait_file(grandchild_file))
    assert _wait_gone(handle.pid)

    assert registry.cancel(handle.process_id, session_id="sess") is True
    assert _wait_gone(descendant)


def test_stop_services_kills_services_but_keeps_user_jobs(tmp_path: Path) -> None:
    registry = ProcessRegistry()
    svc = registry.spawn(
        _python("import time", "time.sleep(120)"),
        session_id="sess",
        cwd=tmp_path,
        env=None,
        stdin=True,
        stdout_path=tmp_path / "svc.log",
        stderr_path=tmp_path / "svc.err",
        service=True,
    )
    job = registry.spawn(
        _python("import time", "time.sleep(120)"),
        session_id="sess",
        cwd=tmp_path,
        env=None,
        stdin=False,
        stdout_path=tmp_path / "job.log",
        stderr_path=tmp_path / "job.err",
    )
    assert os.getpgid(job.pid) != os.getpgid(os.getpid())

    registry.stop_services()
    assert _wait_gone(svc.pid)
    assert _pid_alive(job.pid)
    assert registry.cancel(job.process_id, session_id="sess") is True
    assert _wait_gone(job.pid)


def test_service_ids_stay_session_scoped(tmp_path: Path) -> None:
    registry = ProcessRegistry()
    handle = registry.spawn(
        _python("import time", "time.sleep(120)"),
        session_id="owner",
        cwd=tmp_path,
        env=None,
        stdin=True,
        stdout_path=tmp_path / "svc.log",
        stderr_path=tmp_path / "svc.err",
        service=True,
    )
    try:
        assert registry.list_background(session_id="intruder") == ()
        for call in (
            lambda: registry.write(handle.process_id, b"x", session_id="intruder"),
            lambda: registry.read_output(handle.process_id, stream="stdout", offset=0, limit=8, session_id="intruder"),
            lambda: registry.cancel(handle.process_id, session_id="intruder"),
        ):
            try:
                call()
            except ValueError as exc:
                assert "unknown background process" in str(exc)
            else:
                raise AssertionError("foreign session reached a service process")
        assert _pid_alive(handle.pid)
    finally:
        assert registry.cancel(handle.process_id, session_id="owner") is True
