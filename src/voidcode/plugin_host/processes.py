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


@dataclass(slots=True)
class _Background:
    """One supervised background process: identity, group, stdin pipe, and its log targets."""

    process_id: str
    session_id: str
    pid: int
    start_time: str
    stdout_path: Path
    stderr_path: Path
    process: subprocess.Popen[Any]

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
        self._groups: dict[int, subprocess.Popen[Any]] = {}
        self._background: dict[str, _Background] = {}
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
        """Kill and reap every tracked group; the host never leaves a child behind."""
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
    ) -> ProcessHandle:
        """Start one supervised process, streaming its output into the caller's paths."""
        command = _decode_argv(argv, caller="spawn_process")
        identifier = process_id or f"proc-{uuid4().hex[:16]}"
        with self._lock:
            existing = self._background.get(identifier)
            if existing is not None and existing.running():
                raise ValueError(f"background process {identifier} is already running")
        for path in (stdout_path, stderr_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        child_env = None if env is None else {**os.environ, **{str(key): str(value) for key, value in env.items()}}
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
        )
        with self._lock:
            replaced = self._background.get(identifier)
            if replaced is not None:
                self._groups.pop(replaced.pid, None)
            self._background[identifier] = entry
            self._groups[process.pid] = process
        return entry.handle()

    def background(self, process_id: str, *, session_id: str) -> _Background:
        """One supervised process, visible only to the session that started it."""
        with self._lock:
            entry = self._background.get(process_id)
        if entry is None:
            # Either it was never started here, or it belongs to another session, or it survived a
            # host restart: all three are "not supervised by this host for this session", and the
            # caller's persisted identity is what distinguishes them for the user.
            raise ValueError(f"unknown background process for this session: {process_id}")
        if entry.session_id != session_id:
            raise ValueError(f"unknown background process for this session: {process_id}")
        return entry

    def list_background(self, *, session_id: str) -> tuple[ProcessHandle, ...]:
        with self._lock:
            entries = [entry for entry in self._background.values() if entry.session_id == session_id]
        return tuple(entry.handle() for entry in entries)

    def write(self, process_id: str, data: bytes, *, session_id: str) -> int:
        entry = self.background(process_id, session_id=session_id)
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
        """Stop one supervised process: SIGTERM its group, then SIGKILL, and report whether it died."""
        entry = self.background(process_id, session_id=session_id)
        if entry.process.poll() is None:
            _kill_group(entry.process, signal.SIGTERM)
            drain_until = time.monotonic() + _STOP_GRACE_SECONDS
            if not self._wait_exit(entry.process, drain_until):
                _kill_group(entry.process, signal.SIGKILL)
                if not self._wait_exit(entry.process, time.monotonic() + _STOP_GRACE_SECONDS):
                    return False
        return True

    def stop_background(self) -> None:
        """Stop every supervised process at the owner's shutdown; a host swap keeps them running."""
        with self._lock:
            entries = list(self._background.values())
        drain_until = time.monotonic() + _READER_GRACE_SECONDS
        for entry in entries:
            if entry.process.poll() is None:
                self._stop(entry.process, drain_until=drain_until)
        with self._lock:
            for entry in entries:
                self._groups.pop(entry.pid, None)
            self._background.clear()


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
