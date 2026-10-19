"""Native ``!`` shell escape ported from ``voidcode.runtime.user_shell``.

Mirrors upstream Codex ``codex-rs/core/src/tasks/user_shell.rs`` (rust-v0.153.4):
a ``!command`` submission executes locally under the user's login shell and the
output lands in conversation history as model-visible items without starting a
provider turn (upstream ``shouldQuery: false`` semantics).

Intentional consent bypass: submission of a ``!`` command IS the user's explicit
consent, so this path performs NO approval check, NO permission-gate evaluation,
and NO sandboxing (upstream ``PermissionProfile::Disabled`` + ``SandboxType::None``).
It always executes, even in read-only/plan mode — plan mode narrows model tool
calls, never a direct user action.

The bounded output capture (``_ProcessOutputCapture``) and timeout kill helper
(``_kill_timed_out_process``) are copied from ``voidcode.tools.process_output``
and ``voidcode.tools.shell_exec``; only the imports/types were adjusted.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final

#: Upstream ``USER_SHELL_TIMEOUT_MS`` (``user_shell.rs:49``): 1 hour, no
#: user-exposed timeout syntax.
USER_SHELL_TIMEOUT_SECONDS: Final[int] = 3600
#: Upstream truncation fallback: middle-truncation at 10_000 bytes. Raw full
#: output is kept on the record via the artifact surface when one exists.
USER_SHELL_TRUNCATION_BYTES: Final[int] = 10_000

#: Bare-``!`` help (upstream ``chatwidget.rs:496-497``), shown instead of executing.
USER_SHELL_HELP_TITLE: Final[str] = "Prefix a command with ! to run it locally"
USER_SHELL_HELP_HINT: Final[str] = "Example: !ls"

# Copied from ``voidcode.tools.process_output``.
_MAX_CAPTURE_BYTES = 128 * 1024 * 1024
_PREVIEW_BYTES = 25 * 1024


def parse_shell_escape(text: str) -> str | None:
    """Return the command for a ``!``-prefixed submission, else None.

    Detection runs on the trimmed input; the strip is exactly one leading
    ``!`` (``!!foo`` executes ``!foo`` verbatim — there is no ``!!`` form).
    """
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None
    return stripped[1:]


def login_shell_args(command: str) -> list[str]:
    """Login-shell argv for ``command`` (upstream ``shell.rs:22-46``).

    ``bash``/``zsh``/``sh`` get ``-lc``; PowerShell gets ``-Command``; ``cmd``
    gets ``/c``. The command string passes through verbatim — no parsing here.
    """
    path, shell_type = _default_user_shell()
    if shell_type in {"bash", "zsh", "sh"}:
        return [path, "-lc", command]
    if shell_type == "powershell":
        return [path, "-Command", command]
    return [path, "/c", command]


def _default_user_shell() -> tuple[str, str]:
    """Resolve the login shell path and type, mirroring upstream discovery.

    POSIX prefers the user's shell (``$SHELL``), then ``bash``, ``zsh``, and
    finally ``/bin/sh``; Windows prefers PowerShell, then ``cmd``.
    """
    if sys.platform == "win32":
        for candidate in ("pwsh", "powershell"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved, "powershell"
        return "cmd.exe", "cmd"
    user_shell = os.environ.get("SHELL", "")
    name = Path(user_shell).name if user_shell else ""
    if name in {"bash", "zsh", "sh"}:
        resolved = shutil.which(name) or (user_shell if Path(user_shell).exists() else None)
        if resolved:
            return resolved, name
    for name in ("bash", "zsh"):
        resolved = shutil.which(name)
        if resolved:
            return resolved, name
    return "/bin/sh", "sh"


def truncate_middle_chars(text: str, max_bytes: int) -> str:
    """Middle-truncate ``text`` to ``max_bytes`` (upstream ``truncate.rs``).

    Keeps a byte-budget head and tail split evenly on char boundaries with a
    ``…N chars truncated…`` marker. Pure function of the input.
    """
    if max_bytes <= 0:
        return f"…{len(text)} chars truncated…" if text else ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    left_budget, right_budget = max_bytes // 2, max_bytes - max_bytes // 2
    head = encoded[:left_budget].decode("utf-8", errors="ignore")
    tail = encoded[len(encoded) - right_budget :].decode("utf-8", errors="ignore")
    removed = max(0, len(text) - len(head) - len(tail))
    return f"{head}…{removed} chars truncated…{tail}"


@dataclass(slots=True)
class _ProcessOutputCapture:
    """Bounded full process capture with an independently bounded preview."""

    stream_name: str
    file: BinaryIO
    byte_count: int = 0
    limit_exceeded: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def create(cls, stream_name: str) -> _ProcessOutputCapture:
        return cls(stream_name, tempfile.TemporaryFile(mode="w+b"))

    def append(self, chunk: bytes) -> None:
        with self._lock:
            room = max(0, _MAX_CAPTURE_BYTES - self.byte_count)
            retained = chunk[:room]
            if retained:
                self.file.write(retained)
                self.byte_count += len(retained)
            if len(chunk) > room:
                self.limit_exceeded = True

    def preview(self) -> str:
        with self._lock:
            self.file.flush()
            self.file.seek(0)
            if self.byte_count <= _PREVIEW_BYTES * 2:
                data = self.file.read()
            else:
                start = self.file.read(_PREVIEW_BYTES)
                self.file.seek(-_PREVIEW_BYTES, 2)
                data = start + b"\n... output preview omitted ...\n" + self.file.read()
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n")

    def close(self) -> None:
        self.file.close()


def _is_windows_platform() -> bool:
    return os.name == "nt"


def _taskkill_command() -> str | None:
    if not _is_windows_platform():
        return None
    taskkill = shutil.which("taskkill")
    if taskkill is not None:
        return taskkill
    return "taskkill"


def _kill_timed_out_process(process: subprocess.Popen[bytes]) -> None:
    taskkill = _taskkill_command()
    if taskkill is not None:
        taskkill_succeeded = False
        try:
            completed = subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
            taskkill_succeeded = completed.returncode == 0
        except OSError:
            pass
        if taskkill_succeeded:
            return
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return
    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if killpg is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return
    if sigkill is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return
    try:
        killpg(process.pid, sigkill)
    except AttributeError:
        try:
            process.kill()
        except ProcessLookupError:
            pass


@dataclass(frozen=True, slots=True)
class UserShellResult:
    """Completed ``!`` execution, ready to record into history."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    output: str
    truncated: bool
    duration_seconds: float
    timed_out: bool = False

    @property
    def status(self) -> str:
        return "completed" if self.exit_code == 0 else "failed"


def format_user_shell_record(command: str, result: UserShellResult) -> str:
    """Model-visible history record (upstream ``user_shell_command.rs`` body)."""
    return (
        "<user_shell_command>\n"
        f"\n<command>\n{command}\n</command>\n<result>\n"
        f"Exit code: {result.exit_code}\n"
        f"Duration: {result.duration_seconds:.4f} seconds\n"
        f"Output:\n{result.output}\n</result>\n"
        "</user_shell_command>"
    )


def execute_user_shell_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: int = USER_SHELL_TIMEOUT_SECONDS,
) -> UserShellResult:
    """Run ``command`` under the login shell and capture its output.

    Verbatim pass-through: no shell parsing of its own. A spawn failure records
    exit code -1 with the error message and still returns a recordable result;
    callers never need an error-event path.
    """
    argv = login_shell_args(command)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:
        message = f"execution error: {exc}"
        return UserShellResult(
            command=command,
            exit_code=-1,
            stdout="",
            stderr=message,
            output=truncate_middle_chars(message, USER_SHELL_TRUNCATION_BYTES),
            truncated=len(message.encode("utf-8")) > USER_SHELL_TRUNCATION_BYTES,
            duration_seconds=max(0.0, time.monotonic() - started),
        )
    stdout_capture = _ProcessOutputCapture.create("stdout")
    stderr_capture = _ProcessOutputCapture.create("stderr")
    stdout_reader = threading.Thread(
        target=_drain_pipe,
        args=(process.stdout, stdout_capture),
        name="user-shell-stdout-reader",
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=_drain_pipe,
        args=(process.stderr, stderr_capture),
        name="user-shell-stderr-reader",
        daemon=True,
    )
    stdout_reader.start()
    stderr_reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_timed_out_process(process)
        process.wait()
    finally:
        stdout_reader.join(timeout=2.0)
        stderr_reader.join(timeout=2.0)
    duration = max(0.0, time.monotonic() - started)
    stdout = stdout_capture.preview()
    stderr = stderr_capture.preview()
    stdout_capture.close()
    stderr_capture.close()
    exit_code = process.returncode if process.returncode is not None else -1
    if timed_out:
        exit_code = 124
    raw_output = f"{stdout}{stderr}"
    if timed_out:
        raw_output = f"command timed out after {timeout_seconds * 1000} milliseconds\n{raw_output}"
    truncated = len(raw_output.encode("utf-8")) > USER_SHELL_TRUNCATION_BYTES
    return UserShellResult(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        output=truncate_middle_chars(raw_output, USER_SHELL_TRUNCATION_BYTES),
        truncated=truncated,
        duration_seconds=duration,
        timed_out=timed_out,
    )


def _drain_pipe(pipe: object, capture: _ProcessOutputCapture) -> None:
    read = getattr(pipe, "read", None)
    try:
        while callable(read):
            chunk = read(8192)
            if not chunk:
                break
            capture.append(chunk)
    except Exception:
        pass
    finally:
        close = getattr(pipe, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
