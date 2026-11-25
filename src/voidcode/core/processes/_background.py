"""Supervised process entries: identity, log targets, stdin I/O, and owned-tree cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from voidcode.sdk.observed_effects import ObservedEffects
from voidcode.sdk.services import ProcessHandle

from ..artifacts import MAX_PAGE_BYTES
from ..sandbox import native_backend, read_effects_file
from ._procfs import _descendant_pids, _group_pids, _kill_group, _pid_alive, _signal_pid
from ._timing import _POLL_SECONDS, _STOP_GRACE_SECONDS, _WRITE_DEADLINE_SECONDS, _WRITE_POLL_SECONDS


@dataclass(slots=True)
class _Background:
    """One supervised process: identity, group, stdin pipe, and its log targets."""

    process_id: str
    owner: str
    session_id: str
    pid: int
    start_time: str
    stdout_path: Path
    stderr_path: Path
    process: subprocess.Popen[Any]
    own_group: bool = True
    tree_pids: tuple[int, ...] = ()
    #: Supervisor effects sidecar path for confined spawns; None when unconfined.
    effects_path: Path | None = None

    def running(self) -> bool:
        return self.process.poll() is None

    def handle(self) -> ProcessHandle:
        return ProcessHandle(
            process_id=self.process_id,
            pid=self.pid,
            start_time=self.start_time,
            stdout_path=str(self.stdout_path),
            stderr_path=str(self.stderr_path),
            running=self.running(),
            exit_code=self.process.returncode,
        )


#: Exited background entries retained per owner/session; live jobs are never evicted.
MAX_EXITED_BACKGROUND_PER_SESSION: Final[int] = 64


def _refresh_tree(entry: _Background) -> tuple[int, ...]:
    """Union the live descendant tree into this entry's recorded tree and return it.

    Stale numbers are harmless: signals and liveness probes skip dead pids.
    """
    seen = set(entry.tree_pids)
    seen.update(_descendant_pids(entry.pid))
    seen.discard(entry.pid)
    entry.tree_pids = tuple(sorted(seen))
    return entry.tree_pids


def _wait_owned_exit(entry: _Background, descendants: tuple[int, ...], drain_until: float) -> bool:
    """Whether the leader and every recorded descendant exited before the deadline."""
    while True:
        alive = entry.process.poll() is None or _pid_alive(entry.pid)
        survivors = [pid for pid in descendants if _pid_alive(pid)]
        if not alive and not survivors:
            return True
        if time.monotonic() >= drain_until:
            return False
        time.sleep(_POLL_SECONDS)


def _signal_entry(entry: _Background, descendants: tuple[int, ...], sig: int) -> None:
    """Signal exactly what this entry owns: its own session group plus its recorded tree."""
    if os.name == "posix":
        try:
            os.killpg(entry.pid, sig)
        except ProcessLookupError, PermissionError:
            pass
    try:
        entry.process.send_signal(sig)
    except ProcessLookupError, PermissionError, OSError:
        pass
    for child_pid in descendants:
        _signal_pid(child_pid, sig)
    _signal_pid(entry.pid, sig)


def _feed_frame(process: subprocess.Popen[Any], frame: bytes) -> None:
    """Write one framed plan payload to the helper's stdin."""
    pipe = process.stdin
    if pipe is None:
        raise ValueError("sandbox spawn lost its helper stdin pipe")
    try:
        pipe.write(frame)
        pipe.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        raise ValueError(f"sandbox spawn failed to deliver its plan: {exc}") from exc


def _entry_effects(entry: _Background) -> ObservedEffects | None:
    if entry.effects_path is None:
        return None
    return read_effects_file(entry.effects_path, backend=native_backend())


def _write_entry(
    process_id: str,
    entry: _Background,
    data: bytes,
    *,
    deadline: float | None,
    cancel_event: threading.Event | None,
) -> int:
    """Write one supervised entry's stdin without an unbounded blocking write."""
    _refresh_tree(entry)
    pipe = entry.process.stdin
    if pipe is None:
        raise ValueError(f"background process {process_id} was started without a stdin pipe")
    if entry.process.poll() is not None:
        raise ValueError(f"background process {process_id} has exited")
    try:
        fd = pipe.fileno()
        os.set_blocking(fd, False)
    except (OSError, ValueError) as exc:
        raise ValueError(f"background process {process_id} stdin is unavailable: {exc}") from exc
    if deadline is None:
        deadline = time.monotonic() + _WRITE_DEADLINE_SECONDS
    payload = memoryview(data)
    written = 0
    try:
        while written < len(payload):
            if cancel_event is not None and cancel_event.is_set():
                break
            if time.monotonic() >= deadline:
                break
            try:
                count = os.write(fd, payload[written:])
            except BlockingIOError:
                # A full pipe is retried inside the deadline; no writer thread is ever parked.
                time.sleep(_WRITE_POLL_SECONDS)
                continue
            except (BrokenPipeError, OSError, ValueError) as exc:
                if written:
                    # Bytes already accepted are a real side effect; report them honestly.
                    break
                raise ValueError(f"background process {process_id} is not reading its stdin: {exc}") from exc
            if count <= 0:
                break
            written += count
    finally:
        try:
            os.set_blocking(fd, True)
        except OSError, ValueError:
            pass
    return written


def _read_entry_output(
    process_id: str,
    entry: _Background,
    *,
    stream: str,
    offset: int,
    limit: int,
) -> tuple[bytes, int, bool, bool]:
    _refresh_tree(entry)
    if stream not in ("stdout", "stderr"):
        raise ValueError(f"unknown output stream: {stream!r}")
    if offset < 0:
        raise ValueError("process output offset must not be negative")
    if limit <= 0 or limit > MAX_PAGE_BYTES:
        raise ValueError(f"process output limit must be between 1 and {MAX_PAGE_BYTES}")
    path = entry.stdout_path if stream == "stdout" else entry.stderr_path
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(limit)
            total = handle.seek(0, os.SEEK_END)
    except OSError as exc:
        raise ValueError(f"background process {process_id} has no readable {stream}: {exc}") from exc
    return data, total, offset + len(data) >= total, entry.running()


def _cancel_entry(entry: _Background) -> bool:
    """Stop one supervised entry and its owned descendants."""
    descendants = _refresh_tree(entry)
    leader_alive = entry.process.poll() is None or _pid_alive(entry.pid)
    if leader_alive:
        _signal_entry(entry, descendants, signal.SIGTERM)
        drain_until = time.monotonic() + _STOP_GRACE_SECONDS
        if not _wait_owned_exit(entry, descendants, drain_until):
            _signal_entry(entry, descendants, signal.SIGKILL)
            return _wait_owned_exit(entry, descendants, time.monotonic() + _STOP_GRACE_SECONDS)
    elif descendants:
        # The leader already exited: TERM-ignoring descendants are still owned, so sweep the
        # recorded tree instead of declaring victory on dead pids.
        _signal_entry(entry, descendants, signal.SIGKILL)
        return _wait_owned_exit(entry, descendants, time.monotonic() + _STOP_GRACE_SECONDS)
    return True


def _stop_owned_group(entry: _Background, drain_until: float) -> None:
    """TERM a job's whole group, then KILL every surviving member by pid scan.

    Waiting on the leader alone would declare victory while a TERM-ignoring child still
    runs in the same group, so the escalation enumerates live group members instead.
    """
    _kill_group(entry.process, signal.SIGTERM)
    if _wait_group_gone(entry.pid, drain_until):
        entry.process.poll()
        return
    for member in _group_pids(entry.pid):
        _signal_pid(member, signal.SIGKILL)
    _wait_group_gone(entry.pid, drain_until)
    entry.process.poll()


def _wait_group_gone(leader_pid: int, drain_until: float) -> bool:
    """Whether the leader's process group has no live members left before the deadline."""
    while True:
        if not _group_pids(leader_pid):
            return True
        if time.monotonic() >= drain_until:
            return False
        time.sleep(_POLL_SECONDS)


def _entry_settled(entry: _Background) -> bool:
    """Whether an entry's leader and all recorded group members are gone."""
    if entry.process.poll() is None:
        return False
    return not _group_pids(entry.pid)


def _drain_bucket(lock: threading.Lock, bucket: dict[str, _Background]) -> list[_Background]:
    with lock:
        entries = list(bucket.values())
        bucket.clear()
    return entries


def stop_service_entries(entries: list[_Background], drain_until: float) -> None:
    """TERM every generation-owned service tree, then KILL what survives the shared budget."""
    for entry in entries:
        descendants = _refresh_tree(entry)
        if entry.process.poll() is None or _pid_alive(entry.pid):
            _signal_entry(entry, descendants, signal.SIGTERM)
            if not _wait_owned_exit(entry, descendants, drain_until):
                _signal_entry(entry, descendants, signal.SIGKILL)
                _wait_owned_exit(entry, descendants, drain_until)
        elif descendants:
            _signal_entry(entry, descendants, signal.SIGKILL)
            _wait_owned_exit(entry, descendants, drain_until)
