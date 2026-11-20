"""Core-owned child processes: lease-scoped runs plus supervised services and jobs.

The core owns the process, its group, and its bounded output. The core's own
governance already decided whether the calling tool may execute at all; the lease adds
stance only: a read-only or hook lease is refused by the caller.

Every child this registry starts lives in its own process group, so group
signals touch exactly the owned tree. Service processes (one per generation,
stopped at generation retire) run in their own session. Detached background jobs
(user jobs that survive a reload) own their session directly.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from voidcode.sdk.observed_effects import ObservedEffects
from voidcode.sdk.services import ProcessHandle, ProcessResult

from .artifacts import MAX_PAGE_BYTES
from .sandbox import SandboxBackend, build_plan, native_backend, read_effects_file, scratch_dir

#: Hard ceiling for one stream's capture. The bound exists so a runaway command cannot exhaust the
#: core's memory; a caller that wants more must spill the output itself.
MAX_CAPTURE_BYTES: Final[int] = 256 * 1024 * 1024
_DEFAULT_CAPTURE_BYTES: Final[int] = 256 * 1024
_READ_CHUNK_BYTES: Final[int] = 8192
_POLL_SECONDS: Final[float] = 0.05
#: SIGTERM window before a group is escalated to SIGKILL.
_STOP_GRACE_SECONDS: Final[float] = 0.25
#: A stopped child's pipes are drained inside this budget, so its partial output is not lost.
_READER_GRACE_SECONDS: Final[float] = 2.0
#: A cancelled invocation answers inside the core's cooperative-cancel window, partial output and all.
_CANCEL_RESPONSE_SECONDS: Final[float] = 0.75


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


def _pid_alive(pid: int) -> bool:
    """Whether one pid still names a live process this registry may signal (zombies are gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError, OSError:
        return True
    return True


def _signal_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError, PermissionError, OSError:
        pass


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


class ProcessRegistry:
    """Every child process the core started for a lease, plus its bounded capture."""

    def __init__(self) -> None:
        # One-shot `run()` children only: supervised background jobs are owned by their bucket
        # (they survive a generation swap), and service children live in their own sessions so a
        # generation retire stops exactly its tree without touching the shared core group.
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
        sandbox: SandboxBackend | None = None,
        workspace: Path | None = None,
    ) -> ProcessResult:
        """Run one command to completion, a timeout, or a lease cancellation."""
        command = _decode_argv(argv)
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("run_process timeout_seconds must be positive")
        if max_output_bytes <= 0:
            raise ValueError("run_process max_output_bytes must be positive")
        limit = min(int(max_output_bytes), MAX_CAPTURE_BYTES)
        # A backend means the helper's argv replaces the caller's argv; None means today's
        # host path with effects=None.
        if sandbox is not None:
            return self._run_confined(
                sandbox,
                command,
                cwd=cwd,
                env=env,
                workspace=workspace,
                stdin=stdin,
                timeout_seconds=timeout_seconds,
                max_output_bytes=limit,
                cancel_event=cancel_event,
                on_output=on_output,
            )
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
            name="core-stdin",
            daemon=True,
        )
        readers = [
            threading.Thread(
                target=_read_stream,
                args=(pipe, capture, stream_name, reader_errors, on_output),
                name=f"core-{stream_name}",
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
                # A cancelled invocation must answer inside the core's cooperative window while
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

    def _run_confined(
        self,
        sandbox: SandboxBackend,
        command: list[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        workspace: Path | None,
        stdin: bytes | str | None,
        timeout_seconds: float | None,
        max_output_bytes: int,
        cancel_event: threading.Event | None,
        on_output: Callable[[str, bytes], None] | None,
    ) -> ProcessResult:
        """Run one command under the helper: its argv IS the child; effects ride report.json.

        The helper's stdout carries ONLY the child's stdout bytes (byte-clean for capture);
        the exit code + observed effects ride the pid-keyed stage sidecar. A helper launch
        failure is a visible error with the prefix when one exists — never a host fallback.
        Kill/timeout still report the committed prefix the supervisor flushed.
        """
        anchor = str(workspace.resolve()) if workspace is not None else str((cwd or Path.cwd()).resolve())
        child_cwd = str(cwd.resolve()) if cwd is not None else anchor
        raw = None if stdin is None else (stdin.encode("utf-8") if isinstance(stdin, str) else bytes(stdin))
        helper_argv, helper_cwd, helper_env, frame = build_plan(
            sandbox,
            argv=tuple(command),
            cwd=child_cwd,
            env_overrides={str(key): str(value) for key, value in dict(env or {}).items()},
            workspace=anchor,
            scratch=scratch_dir(anchor),
            stdin=raw,
        )
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                list(helper_argv),
                cwd=helper_cwd,
                env=helper_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            raise ValueError(f"sandbox run failed to start the helper: {exc}") from exc
        with self._lock:
            self._groups[process.pid] = process
        helper_pid = process.pid
        backend_name = "unconfined" if sandbox.policy.mode == "danger-full-access" else native_backend()
        stdout_capture = _StreamCapture(max_output_bytes)
        stderr_capture = _StreamCapture(max_output_bytes)
        reader_errors: list[BaseException] = []
        stdin_thread = threading.Thread(target=_write_stdin, args=(process, frame), name="core-stdin", daemon=True)
        readers = [
            threading.Thread(
                target=_read_stream,
                args=(pipe, capture, stream_name, reader_errors, on_output),
                name=f"core-{stream_name}",
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
            slot = _POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining)
            if cancel_event is not None:
                cancel_event.wait(slot)
            else:
                time.sleep(slot)
        for reader in readers:
            reader.join(timeout=max(0.0, drain_until - time.monotonic()))
        stdin_thread.join(timeout=max(0.0, drain_until - time.monotonic()))
        duration_ms = int((time.monotonic() - started) * 1000)
        with self._lock:
            self._groups.pop(process.pid, None)
        if reader_errors:
            first = reader_errors[0]
            raise ValueError(f"sandbox run failed while reading helper output: {type(first).__name__}: {first}")
        effects, helper_exit = self._read_exec_report(helper_pid, helper_env, scratch_dir(anchor), backend_name)
        # The HELPER's exit is transport; the CHILD's exit rides the report. A helper crash
        # (nonzero with no report) is a visible failure — never a host fallback.
        exit_code: int | None
        if cancelled or timed_out:
            exit_code = None
        elif helper_exit is not None:
            exit_code = helper_exit
        elif process.returncode != 0:
            raise ValueError(f"sandbox helper failed (exit {process.returncode}): {stderr_capture.text()[:2000]}")
        else:
            exit_code = process.returncode
        return ProcessResult(
            exit_code=exit_code,
            stdout=stdout_capture.text(),
            stderr=stderr_capture.text(),
            duration_ms=duration_ms,
            truncated=stdout_capture.truncated or stderr_capture.truncated,
            timed_out=timed_out,
            cancelled=cancelled,
            effects=effects,
        )

    def _read_exec_report(self, helper_pid: int, helper_env: dict[str, str], scratch: str, backend: str) -> tuple[ObservedEffects | None, int | None]:
        """Read the pid-keyed stage sidecar; reap the stage dir. Missing report, honest empty.

        Danger (unconfined) mode writes no report.json: the supervisor drops its effects at
        scratch/effects.json instead, so fall back there. The exit code then comes from the
        helper process itself.
        """
        # The helper stages under ITS temp dir (its own TMPDIR from the plan env), not ours.
        temp_base = Path(helper_env.get("TMPDIR") or helper_env.get("TEMP") or tempfile.gettempdir())
        stage = temp_base / f"voidcode-sandbox-{helper_pid}"
        report_path = stage / "report.json"
        try:
            text = report_path.read_text(encoding="utf-8")
        except OSError:
            self._reap_stage(stage)
            return read_effects_file(Path(scratch) / "effects.json", backend=backend), None
        self._reap_stage(stage)
        try:
            payload = json.loads(text)
        except ValueError:
            return None, None
        exit_code = payload.get("exit_code")
        if exit_code is not None and type(exit_code) is not int:
            exit_code = None
        raw_effects = payload.get("effects", {})
        if isinstance(raw_effects, dict) and not raw_effects.get("backend"):
            raw_effects = {**raw_effects, "backend": backend}
        try:
            effects = ObservedEffects.from_payload(raw_effects)
        except ValueError:
            return None, exit_code
        return effects, exit_code

    @staticmethod
    def _reap_stage(stage: Path) -> None:
        try:
            for child in stage.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            stage.rmdir()
        except OSError:
            pass

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

        Background jobs survive a generation swap (stopped only by an explicit owner shutdown),
        and service children are stopped by `stop_services` per owned tree. This registry still
        leaves no *one-shot* child behind.
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
    # awaited: the core owns the live process and its group, the caller owns the identity it
    # persists (`ProcessHandle`) and the log paths it asked for.

    def spawn(
        self,
        argv: Sequence[str],
        *,
        owner: str,
        session_id: str,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        stdin: bool,
        stdout_path: Path,
        stderr_path: Path,
        process_id: str | None = None,
        service: bool = False,
        sandbox: SandboxBackend | None = None,
        workspace: Path | None = None,
    ) -> ProcessHandle:
        """Start one supervised process, streaming its output into the caller's paths.

        ``service`` selects the lifetime: a service process belongs to this core generation
        (it is stopped at generation retirement, in its own session so the shared core group
        is never signalled), while a background process is a user job that survives a swap.
        """
        command = _decode_argv(argv, caller="spawn_service_process" if service else "spawn_process")
        identifier = process_id or f"{'svc' if service else 'proc'}-{uuid4().hex[:16]}"
        with self._lock:
            existing = self._background.get(identifier) or self._services.get(identifier)
            if existing is not None and (existing.owner != owner or existing.session_id != session_id):
                raise ValueError(f"background process id {identifier!r} belongs to another owner or session")
            if existing is not None and existing.running():
                raise ValueError(f"background process {identifier} is already running")
        for path in (stdout_path, stderr_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        frame: bytes | None = None
        effects_path: Path | None = None
        if sandbox is not None:
            anchor = str(workspace.resolve()) if workspace is not None else str((cwd or Path.cwd()).resolve())
            child_cwd = str(cwd.resolve()) if cwd is not None else anchor
            # The supervisor's cumulative sidecar lives next to the logs (runtime-owned spool).
            effects_path = stdout_path.parent / f"{identifier}.effects.json"
            helper_argv, helper_cwd, helper_env, frame = build_plan(
                sandbox,
                argv=tuple(command),
                cwd=child_cwd,
                env_overrides={str(key): str(value) for key, value in dict(env or {}).items()},
                workspace=anchor,
                scratch=scratch_dir(anchor),
                effects_path=str(effects_path),
            )
            command = list(helper_argv)
            child_env: dict[str, str] | None = helper_env
            cwd = Path(helper_cwd)
        else:
            child_env = None if env is None else {**os.environ, **{str(key): str(value) for key, value in env.items()}}
        if service:
            return self._spawn_service(
                command,
                identifier=identifier,
                owner=owner,
                session_id=session_id,
                cwd=cwd,
                child_env=child_env,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                frame=frame,
                effects_path=effects_path,
            )
        try:
            # Logs are appended, never truncated: restarting the same process id keeps one history,
            # which is what makes a readable log id useful across attempts.
            with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
                process = subprocess.Popen(
                    command,
                    cwd=None if cwd is None else str(cwd),
                    env=child_env,
                    stdin=subprocess.PIPE if (stdin or frame is not None) else subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
        except OSError as exc:
            raise ValueError(f"failed to start background process: {exc}") from exc
        if frame is not None:
            self._feed_frame(process, frame)
            if not stdin:
                # The frame was the only stdin the helper reads; close so a service-style
                # helper never blocks waiting for more (a stdin=True caller keeps the pipe).
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except BrokenPipeError, OSError, ValueError:
                    pass
        entry = _Background(
            process_id=identifier,
            owner=owner,
            session_id=session_id,
            pid=process.pid,
            start_time=process_start_identity(process.pid),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            process=process,
            own_group=True,
            effects_path=effects_path,
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
        owner: str,
        session_id: str,
        cwd: Path | None,
        child_env: dict[str, str] | None,
        stdout_path: Path,
        stderr_path: Path,
        frame: bytes | None = None,
        effects_path: Path | None = None,
    ) -> ProcessHandle:
        """Start one service child in its own session."""
        try:
            with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
                server = subprocess.Popen(
                    command,
                    cwd=None if cwd is None else str(cwd),
                    env=child_env,
                    stdin=subprocess.PIPE,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
        except OSError as exc:
            raise ValueError(f"failed to start service process: {exc}") from exc
        if frame is not None:
            # Services keep the stdin pipe (frame first, then caller bytes): the helper
            # reads its one frame and the supervisor forwards the rest to the child.
            self._feed_frame(server, frame)
        entry = _Background(
            process_id=identifier,
            owner=owner,
            session_id=session_id,
            pid=server.pid,
            start_time=process_start_identity(server.pid),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            process=server,
            own_group=True,
            effects_path=effects_path,
        )
        with self._lock:
            replaced = self._services.get(identifier)
            if replaced is not None and replaced.pid in self._groups:
                # Defensive only: current spawns never register service pids here.
                self._groups.pop(replaced.pid, None)
            self._services[identifier] = entry
        self._refresh_tree(entry)
        return entry.handle()

    @staticmethod
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

    def process_effects(self, process_id: str, *, owner: str, session_id: str) -> ObservedEffects | None:
        """Cumulative supervisor sidecar for one owner- and session-scoped process; None when unconfined."""
        entry = self._supervised(process_id, owner=owner, session_id=session_id)
        if entry.effects_path is None:
            return None
        return read_effects_file(entry.effects_path, backend=native_backend())

    def _supervised(self, process_id: str, *, owner: str, session_id: str) -> _Background:
        """One supervised entry, visible only to the owner and session that started it."""
        with self._lock:
            entry = self._background.get(process_id) or self._services.get(process_id)
        if entry is None or entry.owner != owner or entry.session_id != session_id:
            raise ValueError(f"unknown background process for this owner and session: {process_id}")
        return entry

    def background(self, process_id: str, *, owner: str, session_id: str) -> _Background:
        """One supervised process, visible only to its owner and session."""
        return self._supervised(process_id, owner=owner, session_id=session_id)

    def service_entry(self, process_id: str, *, owner: str, session_id: str) -> _Background | None:
        """One generation service entry, or None when not owned by this owner and session."""
        with self._lock:
            entry = self._services.get(process_id)
        if entry is None or entry.owner != owner or entry.session_id != session_id:
            return None
        return entry

    def list_background(self, *, owner: str, session_id: str) -> tuple[ProcessHandle, ...]:
        with self._lock:
            entries = [
                entry for entry in (*self._background.values(), *self._services.values()) if entry.owner == owner and entry.session_id == session_id
            ]
        return tuple(entry.handle() for entry in entries)

    def write(self, process_id: str, data: bytes, *, owner: str, session_id: str) -> int:
        entry = self.background(process_id, owner=owner, session_id=session_id)
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

    def read_output(
        self,
        process_id: str,
        *,
        stream: str,
        offset: int,
        limit: int,
        owner: str,
        session_id: str,
    ) -> tuple[bytes, int, bool, bool]:
        entry = self.background(process_id, owner=owner, session_id=session_id)
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

    def cancel(self, process_id: str, *, owner: str, session_id: str) -> bool:
        """Stop one owner- and session-scoped process and its owned descendants."""
        entry = self.background(process_id, owner=owner, session_id=session_id)
        descendants = self._refresh_tree(entry)
        leader_alive = entry.process.poll() is None or _pid_alive(entry.pid)
        if leader_alive:
            self._signal_entry(entry, descendants, signal.SIGTERM)
            drain_until = time.monotonic() + _STOP_GRACE_SECONDS
            if not self._wait_owned_exit(entry, descendants, drain_until):
                self._signal_entry(entry, descendants, signal.SIGKILL)
                return self._wait_owned_exit(entry, descendants, time.monotonic() + _STOP_GRACE_SECONDS)
        elif descendants:
            # The leader already exited: TERM-ignoring descendants are still owned, so sweep the
            # recorded tree instead of declaring victory on dead pids.
            self._signal_entry(entry, descendants, signal.SIGKILL)
            return self._wait_owned_exit(entry, descendants, time.monotonic() + _STOP_GRACE_SECONDS)
        return True

    def _refresh_tree(self, entry: _Background) -> tuple[int, ...]:
        """Union the live descendant tree into this entry's recorded tree and return it.

        Stale numbers are harmless: signals and liveness probes skip dead pids.
        """
        seen = set(entry.tree_pids)
        seen.update(_descendant_pids(entry.pid))
        seen.discard(entry.pid)
        entry.tree_pids = tuple(sorted(seen))
        return entry.tree_pids

    def _wait_owned_exit(self, entry: _Background, descendants: tuple[int, ...], drain_until: float) -> bool:
        """Whether the leader and every recorded descendant exited before the deadline."""
        while True:
            alive = entry.process.poll() is None or _pid_alive(entry.pid)
            survivors = [pid for pid in descendants if _pid_alive(pid)]
            if not alive and not survivors:
                return True
            if time.monotonic() >= drain_until:
                return False
            time.sleep(_POLL_SECONDS)

    @staticmethod
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

    def stop_background(self) -> None:
        """Stop every supervised background (user job) at the owner's shutdown; a swap keeps them running.

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

        Called by the runtime's own cleanup on retire/reload/owner-shutdown. Each service owns
        its session, so the stop sweeps exactly its group plus the recorded tree — never the
        shared core group.
        """
        entries = self._drain_bucket(self._services)
        drain_until = time.monotonic() + _READER_GRACE_SECONDS
        for entry in entries:
            descendants = self._refresh_tree(entry)
            if entry.process.poll() is None or _pid_alive(entry.pid):
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
