"""Procfs inspection and process-group signaling used by every supervisor."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


def process_start_identity(pid: int) -> str:
    """The core's own start stamp for a pid, so a reused pid can never impersonate a process.

    Linux procfs exposes it (field 22 of `/proc/<pid>/stat`, the clock-tick start time). Where it
    is unavailable this returns an empty string and callers fall back to no identity rather than a
    guessed one.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = stat.rfind(")")
    if marker < 0:
        return ""
    fields = stat[marker + 2 :].split()
    return fields[19] if len(fields) > 19 else ""


def _kill_group(process: subprocess.Popen[Any], sig: int) -> None:
    """Signal the whole group the child opened for itself, then the child itself."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except ProcessLookupError, PermissionError:
            pass
    try:
        process.send_signal(sig)
    except ProcessLookupError, PermissionError, OSError:
        pass


def _proc_ppid(pid: int) -> int | None:
    """The parent pid of one process, read from procfs; None when it is unavailable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    marker = stat.rfind(")")
    if marker < 0:
        return None
    fields = stat[marker + 2 :].split()
    try:
        return int(fields[1]) if len(fields) > 1 else None
    except ValueError:
        return None


def _proc_pgrp(pid: int) -> int | None:
    """The process group of one pid, read from procfs; None when it is unavailable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    marker = stat.rfind(")")
    if marker < 0:
        return None
    fields = stat[marker + 2 :].split()
    try:
        return int(fields[2]) if len(fields) > 2 else None
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    """Whether one pid still names a live process this registry may signal (zombies are gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError, OSError:
        return True
    return True


def _group_pids(leader_pid: int) -> tuple[int, ...]:
    """Live members of one leader's process group, as far as procfs can tell.

    Used only for owned groups this registry itself created, never for the shared core group.
    Without procfs only the leader is known; group sweeps then degrade to leader signals.
    """
    if os.name != "posix" or not Path("/proc").is_dir():
        return (leader_pid,) if _pid_alive(leader_pid) else ()
    try:
        own_pgid = os.getpgrp()
    except OSError:
        own_pgid = -1
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        pgrp = _proc_pgrp(pid)
        if pgrp is None or pgrp != leader_pid:
            continue
        # Every group this helper serves was opened with the leader as its pgid, so that id still
        # names live members after the leader was reaped. Refuse the core's own group outright.
        if pgrp == own_pgid:
            return ()
        members.append(pid)
    return tuple(members)


def _descendant_pids(pid: int) -> tuple[int, ...]:
    """Every live descendant of one pid, deepest last, as far as procfs can tell.

    Without procfs nothing is known, and callers signal the pid only.
    """
    if os.name != "posix" or not Path("/proc").is_dir():
        return ()
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        child = int(entry.name)
        parent = _proc_ppid(child)
        if parent is not None:
            children.setdefault(parent, []).append(child)
    ordered: list[int] = []
    stack = [pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            ordered.append(child)
            stack.append(child)
    ordered.reverse()
    return tuple(ordered)


def _signal_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError, PermissionError, OSError:
        pass
