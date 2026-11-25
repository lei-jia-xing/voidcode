"""One-shot execution lifecycle: unconfined runs, sandboxed runs, and report capture."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from voidcode.sdk.observed_effects import ObservedEffects
from voidcode.sdk.services import ProcessFailure, ProcessResult, ProcessStream

from ..sandbox import SandboxBackend, build_plan, native_backend, read_effects_file, scratch_dir
from ._capture import MAX_CAPTURE_BYTES, _read_stream, _StreamCapture, _write_stdin
from ._procfs import _kill_group
from ._timing import _CANCEL_RESPONSE_SECONDS, _POLL_SECONDS, _READER_GRACE_SECONDS


def _decode_argv(argv: Sequence[str], *, caller: str = "run_process") -> list[str]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError(f"{caller} requires a non-empty argv sequence of strings")
    decoded: list[str] = []
    for item in argv:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{caller} argv entries must be non-empty strings")
        decoded.append(item)
    return decoded


class _GroupTable:
    """Live one-shot child groups, added after spawn and dropped after capture."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._groups: dict[int, subprocess.Popen[Any]] = {}

    def add(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._groups[process.pid] = process

    def discard(self, pid: int) -> None:
        with self._lock:
            self._groups.pop(pid, None)

    def count(self) -> int:
        with self._lock:
            return len(self._groups)

    def take_all(self) -> list[subprocess.Popen[Any]]:
        with self._lock:
            processes = list(self._groups.values())
            self._groups.clear()
        return processes


def run_oneshot(
    groups: _GroupTable,
    argv: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    stdin: bytes | str | None,
    timeout_seconds: float | None,
    max_output_bytes: int,
    cancel_event: threading.Event | None = None,
    on_output: Callable[[ProcessStream, bytes], None] | None = None,
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
        return _run_confined(
            groups,
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
        raise ProcessFailure("start_failed", f"failed to start process: {exc}") from exc

    groups.add(process)

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
            stop_process(process, drain_until=drain_until)
            break
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            # A cancelled invocation must answer inside the core's cooperative window while
            # still draining what the child already wrote, so its partial output survives.
            drain_until = time.monotonic() + _CANCEL_RESPONSE_SECONDS
            stop_process(process, drain_until=drain_until)
            break
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            timed_out = True
            drain_until = time.monotonic() + _READER_GRACE_SECONDS
            stop_process(process, drain_until=drain_until)
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

    groups.discard(process.pid)

    if reader_errors:
        first = reader_errors[0]
        raise ProcessFailure("capture_failed", f"failed while reading process output: {type(first).__name__}: {first}")
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
    groups: _GroupTable,
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
    on_output: Callable[[ProcessStream, bytes], None] | None,
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
        raise ProcessFailure("start_failed", f"sandbox run failed to start the helper: {exc}") from exc
    groups.add(process)
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
            stop_process(process, drain_until=drain_until)
            break
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            drain_until = time.monotonic() + _CANCEL_RESPONSE_SECONDS
            stop_process(process, drain_until=drain_until)
            break
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            timed_out = True
            drain_until = time.monotonic() + _READER_GRACE_SECONDS
            stop_process(process, drain_until=drain_until)
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
    groups.discard(process.pid)
    if reader_errors:
        first = reader_errors[0]
        raise ProcessFailure("capture_failed", f"sandbox run failed while reading helper output: {type(first).__name__}: {first}")
    effects, helper_exit = _read_exec_report(helper_pid, helper_env, scratch_dir(anchor), backend_name)
    # The HELPER's exit is transport; the CHILD's exit rides the report. A helper crash
    # (nonzero with no report) is a visible failure — never a host fallback.
    exit_code: int | None
    if cancelled or timed_out:
        exit_code = None
    elif helper_exit is not None:
        exit_code = helper_exit
    elif process.returncode != 0:
        raise ProcessFailure("sandbox_failed", f"sandbox helper failed (exit {process.returncode}): {stderr_capture.text()[:2000]}")
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


def _read_exec_report(helper_pid: int, helper_env: dict[str, str], scratch: str, backend: str) -> tuple[ObservedEffects | None, int | None]:
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
        _reap_stage(stage)
        return read_effects_file(Path(scratch) / "effects.json", backend=backend), None
    _reap_stage(stage)
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


def stop_process(process: subprocess.Popen[Any], *, drain_until: float) -> None:
    """Signal the group, escalating to SIGKILL, without ever blocking past the drain budget."""
    _kill_group(process, signal.SIGTERM)
    if _wait_exit(process, drain_until):
        return
    _kill_group(process, signal.SIGKILL)
    _wait_exit(process, drain_until)


def _wait_exit(process: subprocess.Popen[Any], drain_until: float) -> bool:
    while process.poll() is None:
        if time.monotonic() >= drain_until:
            return False
        time.sleep(_POLL_SECONDS)
    return True
