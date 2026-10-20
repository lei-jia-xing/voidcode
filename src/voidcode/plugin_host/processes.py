"""Lease-scoped child processes owned by the plugin host.

The host owns the process, its group, and its bounded output: nothing here crosses the stdio
protocol, so a process result never depends on the kernel staying responsive, and the kernel's own
governance already decided whether the calling tool may execute at all. The lease adds stance only:
a read-only or hook lease is refused by the caller, and every live group is reaped when the host
shuts down so no child outlives the generation that started it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from voidcode.sdk.services import ProcessHandle, ProcessResult

#: Hard ceiling for one stream's capture. The bound exists so a runaway command cannot exhaust the
#: host's memory; a caller that wants more must spill the output itself.
MAX_CAPTURE_BYTES: Final[int] = 256 * 1024 * 1024
#: One bounded page of a background process's log, matching the artifact page bound.
MAX_PAGE_BYTES: Final[int] = 256 * 1024
_DEFAULT_CAPTURE_BYTES: Final[int] = 256 * 1024
_READ_CHUNK_BYTES: Final[int] = 8192
_POLL_SECONDS: Final[float] = 0.05
#: SIGTERM window before a group is escalated to SIGKILL.
_STOP_GRACE_SECONDS: Final[float] = 0.25
#: A stopped child's pipes are drained inside this budget, so its partial output is not lost.
_READER_GRACE_SECONDS: Final[float] = 2.0
#: A cancelled invocation answers inside the kernel's cooperative-cancel window, partial output and all.
_CANCEL_RESPONSE_SECONDS: Final[float] = 0.75
#: Service foster parent: becomes a child subreaper (Linux), spawns the real server with the
#: host's log fds, reports the server pid, then lingers as the foster parent so grandchildren
#: that outlive the server reparent here (observable) instead of to init (invisible). It exits
#: once the server and all fostered children are gone; the registry also signals it on cancel.
_SERVICE_INTERMEDIATE: Final[str] = (
    "import ctypes,os,subprocess,sys,time\n"
    "control=int(sys.argv[1])\n"
    "sin=int(sys.argv[2])\n"
    "try:\n"
    "    ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0)\n"
    "except Exception:\n"
    "    pass\n"
    "server=subprocess.Popen(sys.argv[3:],stdin=sin,stdout=sys.stdout.fileno(),"
    "stderr=sys.stderr.fileno(),start_new_session=False,close_fds=True,pass_fds=(sin,))\n"
    "os.write(control,repr(server.pid).encode())\n"
    "os.close(control)\n"
    "os.close(sin)\n"
    "while server.poll() is None:\n"
    "    time.sleep(0.05)\n"
    "deadline=time.monotonic()+300\n"
    "while time.monotonic()<deadline:\n"
    "    time.sleep(0.5)\n"
    "sys.exit(server.returncode or 0)\n"
)


@dataclass(slots=True)
class _StreamCapture:
    """Bounded in-memory capture; excess bytes are dropped and reported."""

    limit: int
    chunks: list[bytes] = field(default_factory=list)
    byte_count: int = 0
    truncated: bool = False

    def append(self, chunk: bytes) -> None:
        room = self.limit - self.byte_count
        if room <= 0:
            self.truncated = True
            return
        if len(chunk) > room:
            self.chunks.append(chunk[:room])
            self.byte_count = self.limit
            self.truncated = True
            return
        self.chunks.append(chunk)
        self.byte_count += len(chunk)

    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", errors="replace").replace("\r\n", "\n")


def _decode_argv(argv: Sequence[str], *, caller: str = "run_process") -> list[str]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError(f"{caller} requires a non-empty argv sequence of strings")
    decoded: list[str] = []
    for item in argv:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{caller} argv entries must be non-empty strings")
        decoded.append(item)
    return decoded


def process_start_identity(pid: int) -> str:
    """The kernel's own start stamp for a pid, so a reused pid can never impersonate a process.

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
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _group_pids(leader_pid: int) -> tuple[int, ...]:
    """Live members of one leader's process group, as far as procfs can tell.

    Used only for owned groups the host itself created, never for the shared host group.
    Without procfs only the leader is known; group sweeps then degrade to leader signals.
    """
    if os.name != "posix" or not Path("/proc").is_dir():
        return (leader_pid,) if _pid_alive(leader_pid) else ()
    try:
        pgid = os.getpgid(leader_pid)
    except ProcessLookupError, PermissionError, OSError:
        # Every group this helper serves was opened with the leader as its pgid, so that id still
        # names live members after the leader was reaped. A live /proc entry means pid reuse, and
        # the reused process is not this group: refuse rather than signal an unrelated group.
        if _proc_pgrp(leader_pid) is not None:
            return ()
        pgid = leader_pid
    members: list[int] = []
    try:
        candidates = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return ()
    for name in candidates:
        member = int(name)
        if _proc_pgrp(member) == pgid and _pid_alive(member):
            members.append(member)
    return tuple(members)


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
    if len(fields) < 3:
        return None
    try:
        return int(fields[2])
    except ValueError:
        return None


def _descendant_pids(pid: int) -> tuple[int, ...]:
    """Every live descendant of one pid, deepest last, as far as procfs can tell.

    A service process shares this host's process group, which is exactly why per-process stops
    cannot use `killpg`: it would signal the host itself. Walking the parent chain gives the
    descendants that a group signal would otherwise have covered. Without procfs nothing is
    known, and callers signal the pid only.
    """
    if os.name != "posix" or not Path("/proc").is_dir():
        return ()
    children: dict[int, list[int]] = {}
    try:
        candidates = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return ()
    for name in candidates:
        child_pid = int(name)
        parent = _proc_ppid(child_pid)
        if parent is None:
            continue
        children.setdefault(parent, []).append(child_pid)
    descendants: list[int] = []
    frontier = [pid]
    seen = {pid}
    while frontier:
        current = frontier.pop()
        for child_pid in children.get(current, ()):
            if child_pid in seen:
                continue
            seen.add(child_pid)
            descendants.append(child_pid)
            frontier.append(child_pid)
    return tuple(descendants)


def _pid_alive(pid: int) -> bool:
    """Whether one pid still names a live process this host may signal (zombies are gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError, OSError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return True
    return stat.rsplit(") ", 1)[-1].split()[0] != "Z"


def _signal_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError, PermissionError, OSError:
        pass


def _signal_tree(pids: tuple[int, ...], leader: int, sig: int) -> None:
    """Signal recorded descendants and their leader, never the group they share with us."""
    for child_pid in pids:
        _signal_pid(child_pid, sig)
    _signal_pid(leader, sig)


@dataclass(slots=True)
class _Background:
    """One supervised process: identity, group, stdin pipe, and its log targets.

    ``own_group`` says who the group signal may reach: a background (user job) child opened its
    own session, so `killpg` is exactly its group; a service child shares this host's group, so it
    is stopped by signalling its recorded descendant tree, and the kernel's tracked-group sweep
    covers a hard host death separately.
    """

    process_id: str
    session_id: str
    pid: int
    start_time: str
    stdout_path: Path
    stderr_path: Path
    process: subprocess.Popen[Any]
    own_group: bool = True
    tree_pids: tuple[int, ...] = ()

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
            exit_code=self.process.poll(),
        )


class ProcessRegistry:
    """Every child process the host started for a lease, plus its bounded capture."""

    def __init__(self) -> None:
        # One-shot `run()` children only: supervised background jobs are owned by their bucket
        # (they survive a host swap), and service children share the host group (never `killpg`).
        self._groups: dict[int, subprocess.Popen[Any]] = {}
        self._background: dict[str, _Background] = {}
        self._services: dict[str, _Background] = {}
        self._lock = threading.Lock()

    def live_group_count(self) -> int:
        with self._lock:
            return len(self._groups)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        stdin: bytes | str | None,
        timeout_seconds: float | None,
        max_output_bytes: int,
        cancel_event: threading.Event | None = None,
        on_output: Callable[[str, bytes], None] | None = None,
    ) -> ProcessResult:
        """Run one command to completion, a timeout, or a lease cancellation."""
        command = _decode_argv(argv)
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("run_process timeout_seconds must be positive")
        if max_output_bytes <= 0:
            raise ValueError("run_process max_output_bytes must be positive")
        limit = min(int(max_output_bytes), MAX_CAPTURE_BYTES)
        child_env = None if env is None else {**os.environ, **{str(key): str(value) for key, value in env.items()}}

        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=None if cwd is None else str(cwd),
                env=child_env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            raise ValueError(f"failed to start process: {exc}") from exc

        with self._lock:
            self._groups[process.pid] = process

        stdout_capture = _StreamCapture(limit)
        stderr_capture = _StreamCapture(limit)
        reader_errors: list[BaseException] = []
        stdin_payload = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
        stdin_thread = threading.Thread(
            target=_write_stdin,
            args=(process, stdin_payload),
            name="plugin-host-stdin",
            daemon=True,
        )
        readers = [
            threading.Thread(
                target=_read_stream,
                args=(pipe, capture, stream_name, reader_errors, on_output),
                name=f"plugin-host-{stream_name}",
                daemon=True,
            )
            for pipe, capture, stream_name in (
                (process.stdout, stdout_capture, "stdout"),
                (process.stderr, stderr_capture, "stderr"),
            )
        ]
        stdin_thread.start()
        for reader in readers:
            reader.start()

        cancelled = False
        timed_out = False
        drain_until = time.monotonic() + _READER_GRACE_SECONDS
        deadline = None if timeout_seconds is None else started + timeout_seconds
        while True:
            if reader_errors:
                drain_until = time.monotonic() + _READER_GRACE_SECONDS
                self._stop(process, drain_until=drain_until)
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                # A cancelled invocation must answer inside the kernel's cooperative window while
                # still draining what the child already wrote, so its partial output survives.
                drain_until = time.monotonic() + _CANCEL_RESPONSE_SECONDS
                self._stop(process, drain_until=drain_until)
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                timed_out = True
                drain_until = time.monotonic() + _READER_GRACE_SECONDS
                self._stop(process, drain_until=drain_until)
                break
            if process.poll() is not None:
                break
            # Block on the lease's cancellation instead of polling it, so a cancel stops the
            # group immediately rather than up to one poll interval later.
            slot = _POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining)
            if cancel_event is not None:
                cancel_event.wait(slot)
            else:
                time.sleep(slot)
        # Every reader is joined inside the drain budget: the pipes close when the group dies, so
        # whatever the child wrote before it was stopped is captured rather than discarded.
        for reader in readers:
            reader.join(timeout=max(0.0, drain_until - time.monotonic()))
        stdin_thread.join(timeout=max(0.0, drain_until - time.monotonic()))
        duration_ms = int((time.monotonic() - started) * 1000)

        with self._lock:
            self._groups.pop(process.pid, None)

        if reader_errors:
            first = reader_errors[0]
            raise ValueError(f"failed while reading process output: {type(first).__name__}: {first}")
        return ProcessResult(
            # A process the runtime stopped has no exit status of its own: None here is honest,
            # and `timed_out`/`cancelled` say which stop it was.
            exit_code=None if (cancelled or timed_out) else process.returncode,
            stdout=stdout_capture.text(),
            stderr=stderr_capture.text(),
            duration_ms=duration_ms,
            truncated=stdout_capture.truncated or stderr_capture.truncated,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def _stop(self, process: subprocess.Popen[Any], *, drain_until: float) -> None:
        """Signal the group, escalating to SIGKILL, without ever blocking past the drain budget."""
        _kill_group(process, signal.SIGTERM)
        if self._wait_exit(process, drain_until):
            return
        _kill_group(process, signal.SIGKILL)
        self._wait_exit(process, drain_until)

    def _wait_exit(self, process: subprocess.Popen[Any], drain_until: float) -> bool:
        while process.poll() is None:
            if time.monotonic() >= drain_until:
                return False
            time.sleep(_POLL_SECONDS)
        return True

    def reap_all(self) -> None:
        """Kill and reap every one-shot `run()` group; supervised buckets are owned separately.

        Background jobs survive a host swap (stopped only by an explicit owner shutdown), and
        service children are stopped by `stop_services` — never by a group sweep that would
        signal the shared host group. The host still leaves no *one-shot* child behind.
        """
        with self._lock:
            processes = list(self._groups.values())
            self._groups.clear()
        drain_until = time.monotonic() + _READER_GRACE_SECONDS
        for process in processes:
            if process.poll() is None:
                self._stop(process, drain_until=drain_until)

    # ---- background processes -----------------------------------------------------------------
    # A background process outlives the invocation that started it, so it is supervised instead of
    # awaited: the host owns the live process and its group, the caller owns the identity it
    # persists (`ProcessHandle`) and the log paths it asked for.

    def spawn(
        self,
        argv: Sequence[str],
        *,
        session_id: str,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        stdin: bool,
        stdout_path: Path,
        stderr_path: Path,
        process_id: str | None = None,
        service: bool = False,
    ) -> ProcessHandle:
        """Start one supervised process, streaming its output into the caller's paths.

        ``service`` selects the lifetime: a service process belongs to this host generation (it is
        stopped at host retirement and joins the host's process group, so a hard host kill reaches
        it), while a background process is a user job that survives a host swap.
        """
        command = _decode_argv(argv, caller="spawn_service_process" if service else "spawn_process")
        identifier = process_id or f"{'svc' if service else 'proc'}-{uuid4().hex[:16]}"
        with self._lock:
            existing = self._background.get(identifier) or self._services.get(identifier)
            if existing is not None and existing.running():
                raise ValueError(f"background process {identifier} is already running")
        for path in (stdout_path, stderr_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        child_env = None if env is None else {**os.environ, **{str(key): str(value) for key, value in env.items()}}
        if service:
            return self._spawn_service(
                command,
                identifier=identifier,
                session_id=session_id,
                cwd=cwd,
                child_env=child_env,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        try:
            # Logs are appended, never truncated: restarting the same process id keeps one history,
            # which is what makes a readable log id useful across attempts.
            with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
                process = subprocess.Popen(
                    command,
                    cwd=None if cwd is None else str(cwd),
                    env=child_env,
                    stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
        except OSError as exc:
            raise ValueError(f"failed to start background process: {exc}") from exc
        entry = _Background(
            process_id=identifier,
            session_id=session_id,
            pid=process.pid,
            start_time=process_start_identity(process.pid),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            process=process,
            own_group=True,
        )
        with self._lock:
            replaced = self._background.get(identifier)
            if replaced is not None and replaced.pid in self._groups:
                # Defensive only: current spawns never register background pids here.
                self._groups.pop(replaced.pid, None)
            self._background[identifier] = entry
        return entry.handle()

    def _spawn_service(
        self,
        command: list[str],
        *,
        identifier: str,
        session_id: str,
        cwd: Path | None,
        child_env: dict[str, str] | None,
        stdout_path: Path,
        stderr_path: Path,
    ) -> ProcessHandle:
        """Start one service child behind a reaping intermediate in the host's group.

        The intermediate double-forks the real server, reports its pid over a pipe, then waits as
        the foster parent: grandchildren that outlive the server reparent to the intermediate
        (never to init), so the recorded tree stays observable until the whole service stops. The
        entry's ``process`` handle is the intermediate — shared group, reaped by us — while the
        server's stdin handle and log paths serve IO. The reported ``ProcessHandle`` names the
        server pid the caller observes.
        """
        control_reader, control_writer = os.pipe()
        stdin_reader, stdin_writer = os.pipe()
        try:
            with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
                intermediate = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _SERVICE_INTERMEDIATE,
                        str(control_writer),
                        str(stdin_reader),
                        *command,
                    ],
                    cwd=None if cwd is None else str(cwd),
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=False,
                    pass_fds=(control_writer, stdin_reader),
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
        except OSError as exc:
            os.close(control_reader)
            os.close(control_writer)
            os.close(stdin_reader)
            os.close(stdin_writer)
            raise ValueError(f"failed to start service process: {exc}") from exc
        os.close(control_writer)
        os.close(stdin_reader)
        try:
            with os.fdopen(control_reader, "rb") as control:
                reported = control.read().decode("ascii", errors="strict").strip()
            server_pid = int(reported)
        except (OSError, ValueError) as exc:
            os.close(stdin_writer)
            intermediate.kill()
            intermediate.wait(timeout=5)
            raise ValueError(f"service intermediate reported no server pid: {exc}") from exc
        stdin_pipe = os.fdopen(stdin_writer, "wb", buffering=0)
        # The server inherits our copies of the log fds through the intermediate's redirection,
        # so both the intermediate and the server append to the same logs.
        entry = _Background(
            process_id=identifier,
            session_id=session_id,
            pid=server_pid,
            start_time=process_start_identity(server_pid),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            process=intermediate,
            own_group=False,
        )
        entry.process.stdin = stdin_pipe  # type: ignore[assignment]
        with self._lock:
            replaced = self._services.get(identifier)
            if replaced is not None and replaced.pid in self._groups:
                # Defensive only: current spawns never register service pids here.
                self._groups.pop(replaced.pid, None)
            self._services[identifier] = entry
        self._refresh_tree(entry)
        return entry.handle()

    def _supervised(self, process_id: str, *, session_id: str) -> _Background:
        """One supervised entry (background or service), visible only to its own session."""
        with self._lock:
            entry = self._background.get(process_id) or self._services.get(process_id)
        if entry is None:
            # Either it was never started here, or it belongs to another session, or it survived a
            # host restart: all three are "not supervised by this host for this session", and the
            # caller's persisted identity is what distinguishes them for the user.
            raise ValueError(f"unknown background process for this session: {process_id}")
        if entry.session_id != session_id:
            raise ValueError(f"unknown background process for this session: {process_id}")
        return entry

    def background(self, process_id: str, *, session_id: str) -> _Background:
        """One supervised process, visible only to the session that started it."""
        return self._supervised(process_id, session_id=session_id)

    def service_entry(self, process_id: str, *, session_id: str) -> _Background | None:
        """One host-generation service entry, or None when this id is not a live service."""
        with self._lock:
            entry = self._services.get(process_id)
        if entry is None or entry.session_id != session_id:
            return None
        return entry

    def list_background(self, *, session_id: str) -> tuple[ProcessHandle, ...]:
        with self._lock:
            entries = [entry for entry in (*self._background.values(), *self._services.values()) if entry.session_id == session_id]
        return tuple(entry.handle() for entry in entries)

    def write(self, process_id: str, data: bytes, *, session_id: str) -> int:
        entry = self.background(process_id, session_id=session_id)
        self._refresh_tree(entry)
        pipe = entry.process.stdin
        if pipe is None:
            raise ValueError(f"background process {process_id} was started without a stdin pipe")
        if entry.process.poll() is not None:
            raise ValueError(f"background process {process_id} has exited")
        try:
            written = pipe.write(data)
            pipe.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ValueError(f"background process {process_id} is not reading its stdin: {exc}") from exc
        return 0 if written is None else written

    def read_output(self, process_id: str, *, stream: str, offset: int, limit: int, session_id: str) -> tuple[bytes, int, bool, bool]:
        entry = self.background(process_id, session_id=session_id)
        self._refresh_tree(entry)
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

    def cancel(self, process_id: str, *, session_id: str) -> bool:
        """Stop one supervised process and its owned descendants, reporting whether all are gone."""
        entry = self.background(process_id, session_id=session_id)
        descendants = self._refresh_tree(entry)
        leaders_alive = entry.process.poll() is None or (not entry.own_group and _pid_alive(entry.pid))
        if leaders_alive:
            self._signal_entry(entry, descendants, signal.SIGTERM)
            drain_until = time.monotonic() + _STOP_GRACE_SECONDS
            if not self._wait_owned_exit(entry, descendants, drain_until):
                self._signal_entry(entry, descendants, signal.SIGKILL)
                return self._wait_owned_exit(entry, descendants, time.monotonic() + _STOP_GRACE_SECONDS)
        elif descendants:
            # The leaders already exited: TERM-ignoring descendants are still owned, so sweep the
            # recorded tree instead of declaring victory on dead pids.
            self._signal_entry(entry, descendants, signal.SIGKILL)
            return self._wait_owned_exit(entry, descendants, time.monotonic() + _STOP_GRACE_SECONDS)
        return True

    def _refresh_tree(self, entry: _Background) -> tuple[int, ...]:
        """Union the live descendant tree into this entry's recorded tree and return it.

        Service children run behind a foster-parent intermediate, so both the server pid and the
        intermediate pid root the walk: grandchildren that outlived the server reparent to the
        intermediate and stay observable. Stale numbers are harmless: signals and liveness
        probes skip dead pids.
        """
        if entry.own_group:
            return ()
        seen = set(entry.tree_pids)
        seen.update(_descendant_pids(entry.pid))
        seen.update(_descendant_pids(entry.process.pid))
        seen.discard(entry.pid)
        seen.discard(entry.process.pid)
        entry.tree_pids = tuple(sorted(seen))
        return entry.tree_pids

    def _wait_owned_exit(self, entry: _Background, descendants: tuple[int, ...], drain_until: float) -> bool:
        """Whether the leaders and every recorded descendant exited before the deadline."""
        while True:
            server_alive = _pid_alive(entry.pid)
            intermediate_alive = entry.process.poll() is None
            survivors = [pid for pid in descendants if _pid_alive(pid)]
            if not server_alive and not intermediate_alive and not survivors:
                return True
            if time.monotonic() >= drain_until:
                return False
            time.sleep(_POLL_SECONDS)

    @staticmethod
    def _signal_entry(entry: _Background, descendants: tuple[int, ...], sig: int) -> None:
        """Signal exactly what this entry owns: its own group, or its tree in the shared group."""
        if entry.own_group:
            _kill_group(entry.process, sig)
        else:
            for child_pid in descendants:
                _signal_pid(child_pid, sig)
            _signal_pid(entry.pid, sig)
            _signal_pid(entry.process.pid, sig)

    def stop_background(self) -> None:
        """Stop every supervised background (user job) at the owner's shutdown; a host swap keeps them running.

        Each job owns its process group, so the stop sweeps the whole group — including
        TERM-ignoring members after a dead leader — and only then reports. A swap never calls
        this; the jobs keep running with their persisted identities.
        """
        entries = self._drain_bucket(self._background)
        for entry in entries:
            # One grace per job: sharing a single deadline lets an earlier TERM-ignoring job
            # consume the whole window and turns a later job's TERM into an immediate SIGKILL.
            self._stop_owned_group(entry, time.monotonic() + _READER_GRACE_SECONDS)

    def _stop_owned_group(self, entry: _Background, drain_until: float) -> None:
        """TERM a job's whole group, then KILL every surviving member by pid scan.

        Waiting on the leader alone would declare victory while a TERM-ignoring child still
        runs in the same group, so the escalation enumerates live group members instead.
        """
        _kill_group(entry.process, signal.SIGTERM)
        if self._wait_group_gone(entry.pid, drain_until):
            entry.process.poll()
            return
        for member in _group_pids(entry.pid):
            _signal_pid(member, signal.SIGKILL)
        self._wait_group_gone(entry.pid, drain_until)
        entry.process.poll()

    def _wait_group_gone(self, leader_pid: int, drain_until: float) -> bool:
        """Whether the leader's process group has no live members left before the deadline."""
        while True:
            if not _group_pids(leader_pid):
                return True
            if time.monotonic() >= drain_until:
                return False
            time.sleep(_POLL_SECONDS)

    def stop_services(self) -> None:
        """Stop every service process this generation started, plus its recorded descendants.

        Called by the host's own cleanup on retire/reload/owner-shutdown. A hard host kill is
        covered separately: the kernel sweeps this host's tracked group even when the leader is
        already dead, because service children share the group by design.
        """
        entries = self._drain_bucket(self._services)
        drain_until = time.monotonic() + _READER_GRACE_SECONDS
        for entry in entries:
            descendants = self._refresh_tree(entry)
            if entry.process.poll() is None:
                self._signal_entry(entry, descendants, signal.SIGTERM)
                if not self._wait_owned_exit(entry, descendants, drain_until):
                    self._signal_entry(entry, descendants, signal.SIGKILL)
                    self._wait_owned_exit(entry, descendants, drain_until)
            elif descendants:
                self._signal_entry(entry, descendants, signal.SIGKILL)
                self._wait_owned_exit(entry, descendants, drain_until)

    def _drain_bucket(self, bucket: dict[str, _Background]) -> list[_Background]:
        with self._lock:
            entries = list(bucket.values())
            bucket.clear()
        return entries


def _write_stdin(process: subprocess.Popen[Any], payload: bytes | None) -> None:
    if payload is None or process.stdin is None:
        return
    try:
        process.stdin.write(payload)
        process.stdin.flush()
    except BrokenPipeError, OSError, ValueError:
        pass
    finally:
        try:
            process.stdin.close()
        except BrokenPipeError, OSError, ValueError:
            pass


def _read_stream(
    pipe: Any,
    capture: _StreamCapture,
    stream_name: str,
    errors: list[BaseException],
    on_output: Callable[[str, bytes], None] | None,
) -> None:
    try:
        while True:
            reader = getattr(pipe, "read1", None)
            chunk = reader(_READ_CHUNK_BYTES) if callable(reader) else pipe.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            capture.append(chunk)
            if on_output is not None:
                try:
                    on_output(stream_name, chunk)
                except Exception:
                    # Streaming is observational: a broken sink never alters the command result.
                    pass
    except (OSError, ValueError) as exc:
        errors.append(exc)
    finally:
        try:
            pipe.close()
        except OSError, ValueError:
            pass
