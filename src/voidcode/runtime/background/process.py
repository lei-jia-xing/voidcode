from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

_MAX_BACKGROUND_PROCESS_LOG_LINES = 500


class BackgroundProcessPersistence(Protocol):
    """Persistence boundary used by the runtime-owned process manager."""

    def register_background_process(self, **kwargs: object) -> None: ...

    def load_background_process(self, *, workspace: Path, process_id: str) -> dict[str, object] | None: ...

    def list_background_processes(self, *, workspace: Path) -> tuple[dict[str, object], ...]: ...

    def mark_background_process_exit(
        self,
        *,
        workspace: Path,
        process_id: str,
        status: str,
        exit_code: int | None,
        reconciliation_reason: str | None = None,
    ) -> None: ...


@dataclass(slots=True)
class BackgroundProcessState:
    process_id: str
    command: str
    cwd: str
    process: subprocess.Popen[str]
    stdout_chunks: list[str]
    stderr_chunks: list[str]
    owner_session_id: str | None = None
    process_identity: str | None = None
    process_group_id: int | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    stdout_dropped_lines: int = 0
    stderr_dropped_lines: int = 0
    stdout_artifact: dict[str, object] | None = None
    stderr_artifact: dict[str, object] | None = None
    status: str = "running"
    prior_runtime: bool = False
    reconciliation_reason: str | None = None
    identity_match: bool | None = None
    observed_running: bool | None = None
    reconciled: bool = False


class _DetachedProcess:
    def __init__(self, *, pid: int, exit_code: int | None) -> None:
        self.pid = pid
        self._exit_code = exit_code
        self.stdin = None
        self.stdout = None
        self.stderr = None

    def poll(self) -> int | None:
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        if self._exit_code is None:
            raise RuntimeError("detached process cannot be waited on after runtime restart")
        return self._exit_code


# Process identity is read from Linux procfs and distinguishes a reused PID. Other
# platforms fail closed: a process started by a prior runtime cannot be re-attached.
def _process_identity(pid: int) -> str | None:
    if _is_windows():
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    marker = stat.rfind(")")
    if marker < 0:
        return None
    fields = stat[marker + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_log_tail(path: Path) -> tuple[list[str], int]:
    lines: deque[str] = deque(maxlen=_MAX_BACKGROUND_PROCESS_LOG_LINES)
    count = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                lines.append(line)
                count += 1
    except OSError:
        return [], 0
    return list(lines), max(0, count - len(lines))


def _safe_restored_log_path(raw_path: object, *, workspace: Path) -> Path | None:
    """Return a restored log path only when it is inside the canonical log root.

    Persisted paths are untrusted restart metadata: a corrupt or tampered row
    must not turn reconciliation into an arbitrary file read. ``resolve`` also
    collapses symlinks, so a path that appears under the root but points outside
    it is rejected.
    """
    if not isinstance(raw_path, str) or not raw_path:
        return None
    workspace_root = workspace.resolve()
    root = (workspace_root / ".voidcode" / "background-processes").resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    try:
        # The canonical log root itself must remain under the workspace (for
        # example, reject a malicious .voidcode symlink to /var/tmp).
        root.relative_to(workspace_root)
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except OSError, RuntimeError, ValueError:
        return None
    return resolved


def _append_log_lines(state: BackgroundProcessState, *, stream_name: str, lines: list[str]) -> None:
    chunks = state.stdout_chunks if stream_name == "stdout" else state.stderr_chunks
    for line in lines:
        chunks.append(line)
        if len(chunks) > _MAX_BACKGROUND_PROCESS_LOG_LINES:
            chunks.pop(0)
            if stream_name == "stdout":
                state.stdout_dropped_lines += 1
            else:
                state.stderr_dropped_lines += 1


class BackgroundProcessManager:
    """Own subprocess lifecycle, durable records, and restart reconciliation."""

    def __init__(
        self,
        *,
        persistence: BackgroundProcessPersistence | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._processes: dict[str, BackgroundProcessState] = {}
        self._lock = threading.RLock()
        self._persistence = persistence
        self._workspace = workspace.resolve() if workspace is not None else None
        if self._persistence is not None and self._workspace is not None:
            self._reconcile()

    def start(
        self,
        *,
        command: str,
        workspace: Path,
        owner_session_id: str | None = None,
        enforce_owner: bool = False,
    ) -> BackgroundProcessState:
        normalized_workspace = workspace.resolve()
        with self._lock:
            existing = self.load_running(
                command=command,
                workspace=normalized_workspace,
                owner_session_id=owner_session_id,
                enforce_owner=enforce_owner,
            )
            if existing is not None:
                return existing

            process_id = f"proc-{uuid.uuid4().hex}"
            log_dir = normalized_workspace / ".voidcode" / "background-processes"
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = log_dir / f"{process_id}.stdout.log"
            stderr_path = log_dir / f"{process_id}.stderr.log"
            stdout_file = stdout_path.open("a+", encoding="utf-8", errors="replace")
            stderr_file = stderr_path.open("a+", encoding="utf-8", errors="replace")
            try:
                process = subprocess.Popen(
                    command,
                    cwd=normalized_workspace,
                    shell=True,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            except Exception:
                stdout_file.close()
                stderr_file.close()
                raise
            state = BackgroundProcessState(
                process_id=process_id,
                command=command,
                cwd=str(normalized_workspace),
                process=process,
                stdout_chunks=[],
                stderr_chunks=[],
                owner_session_id=owner_session_id,
                process_identity=_process_identity(process.pid),
                process_group_id=None if _is_windows() else process.pid,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            try:
                self._register(state)
            except Exception:
                _terminate_background_process_group(process)
                stdout_file.close()
                stderr_file.close()
                raise
            stdout_file.close()
            stderr_file.close()
            self._processes[state.process_id] = state
            self._start_reader(state, stream_name="stdout")
            self._start_reader(state, stream_name="stderr")
            return state

    def load_running(
        self,
        *,
        command: str,
        workspace: Path,
        owner_session_id: str | None = None,
        enforce_owner: bool = False,
    ) -> BackgroundProcessState | None:
        normalized_command = command.strip()
        normalized_cwd = str(workspace.resolve())
        with self._lock:
            for state in self._processes.values():
                self._refresh(state)
                if state.prior_runtime or state.status != "running":
                    continue
                if state.process.poll() is not None:
                    continue
                if state.command.strip() != normalized_command or state.cwd != normalized_cwd:
                    continue
                if enforce_owner and state.owner_session_id != owner_session_id:
                    continue
                return state
        return None

    def load(
        self,
        process_id: str,
        *,
        workspace: Path | None = None,
        owner_session_id: str | None = None,
        enforce_owner: bool = False,
    ) -> BackgroundProcessState | None:
        with self._lock:
            state = self._processes.get(process_id)
            if state is None and self._persistence is not None and workspace is not None:
                self._restore_one(process_id, workspace.resolve())
                state = self._processes.get(process_id)
            if state is None:
                return None
            self._authorize(state, workspace=workspace, owner_session_id=owner_session_id, enforce_owner=enforce_owner)
            self._refresh(state)
            return state

    def list_processes(
        self,
        *,
        workspace: Path,
        owner_session_id: str | None = None,
        enforce_owner: bool = False,
        limit: int = 64,
    ) -> tuple[BackgroundProcessState, ...]:
        """Return a bounded, workspace/owner-scoped process projection."""
        if limit < 1:
            return ()
        normalized_workspace = workspace.resolve()
        with self._lock:
            if self._persistence is not None:
                self._reconcile()
            states: list[BackgroundProcessState] = []
            for state in self._processes.values():
                if state.cwd != str(normalized_workspace):
                    continue
                if enforce_owner and state.owner_session_id != owner_session_id:
                    continue
                self._refresh(state)
                states.append(state)
            states.sort(key=lambda state: state.process_id)
            return tuple(states[:limit])

    def find_stale(
        self,
        *,
        command: str,
        workspace: Path,
        owner_session_id: str | None = None,
        enforce_owner: bool = False,
    ) -> tuple[BackgroundProcessState, ...]:
        normalized_command = command.strip()
        normalized_cwd = str(workspace.resolve())
        with self._lock:
            matches = [
                state
                for state in self._processes.values()
                if state.status == "stale"
                and state.command.strip() == normalized_command
                and state.cwd == normalized_cwd
                and (not enforce_owner or state.owner_session_id == owner_session_id)
            ]
        return tuple(matches[:16])

    def write(
        self,
        process_id: str,
        input_text: str,
        *,
        workspace: Path | None = None,
        owner_session_id: str | None = None,
        enforce_owner: bool = False,
    ) -> None:
        state = self._require(
            process_id,
            workspace=workspace,
            owner_session_id=owner_session_id,
            enforce_owner=enforce_owner,
        )
        if state.prior_runtime:
            raise ValueError(
                f"background process {process_id} is stale: it was observed after a runtime restart "
                "and is externally managed/unavailable to this runtime"
            )
        if state.process.poll() is not None:
            raise ValueError(f"background process {process_id} is no longer running")
        stdin = state.process.stdin
        if stdin is None:
            raise ValueError(f"background process {process_id} has no stdin stream")
        stdin.write(input_text)
        stdin.flush()

    def stop(
        self,
        process_id: str,
        *,
        workspace: Path | None = None,
        owner_session_id: str | None = None,
        enforce_owner: bool = False,
    ) -> BackgroundProcessState:
        state = self._require(
            process_id,
            workspace=workspace,
            owner_session_id=owner_session_id,
            enforce_owner=enforce_owner,
        )
        if state.prior_runtime:
            raise ValueError(
                f"background process {process_id} is stale: it was observed after a runtime restart "
                "and is externally managed/unavailable to this runtime"
            )
        if state.process.poll() is None or (state.process_group_id is not None and _process_group_exists(state.process_group_id)):
            _terminate_background_process_group(state.process)
        self._mark_exit(state)
        return state

    def stop_all(self) -> None:
        with self._lock:
            process_ids = tuple(self._processes)
        for process_id in process_ids:
            try:
                state = self._processes.get(process_id)
                if state is not None and state.prior_runtime:
                    continue
                self.stop(process_id)
            except Exception:
                # A failed process/helper/persistence operation must not prevent
                # cleanup of other processes owned by this runtime.
                continue

    def _register(self, state: BackgroundProcessState) -> None:
        if self._persistence is None:
            return
        register = getattr(self._persistence, "register_background_process", None)
        if not callable(register):
            return
        register(
            workspace=Path(state.cwd),
            process_id=state.process_id,
            owner_session_id=state.owner_session_id,
            command=state.command,
            cwd=state.cwd,
            pid=state.process.pid,
            process_group_id=state.process_group_id,
            process_identity=state.process_identity,
            stdout_path=str(state.stdout_path or ""),
            stderr_path=str(state.stderr_path or ""),
        )

    def _reconcile(self) -> None:
        assert self._workspace is not None
        list_processes = getattr(self._persistence, "list_background_processes", None)
        if not callable(list_processes):
            return
        for record in list_processes(workspace=self._workspace):
            self._restore_record(record, workspace=self._workspace)

    def _restore_one(self, process_id: str, workspace: Path) -> None:
        load_process = getattr(self._persistence, "load_background_process", None)
        if not callable(load_process):
            return
        record = load_process(workspace=workspace, process_id=process_id)
        if record is not None:
            self._restore_record(record, workspace=workspace)

    def _restore_record(self, record: Mapping[str, object], *, workspace: Path | None = None) -> None:
        process_id = record.get("process_id")
        if not isinstance(process_id, str) or process_id in self._processes:
            return
        cwd = record.get("cwd")
        pid = record.get("pid")
        if not isinstance(cwd, str) or not isinstance(pid, int):
            return
        raw_status = record.get("status")
        status = raw_status if isinstance(raw_status, str) else "exited"
        identity = record.get("process_identity")
        process_identity = identity if isinstance(identity, str) else None
        observed_running = _pid_running(pid)
        observed_identity = _process_identity(pid) if observed_running else None
        identity_match: bool | None = (
            False
            if not observed_running
            else None
            if process_identity is None or observed_identity is None
            else observed_identity == process_identity
        )
        raw_exit_code = record.get("exit_code")
        exit_code = raw_exit_code if isinstance(raw_exit_code, int) else None
        reason = record.get("reconciliation_reason")
        reconciliation_reason = (
            reason
            if isinstance(reason, str)
            else (
                "Prior-runtime process record was observed after restart; it was not reattached "
                "and is externally managed/unavailable to this runtime."
            )
        )
        restored_workspace = workspace.resolve() if workspace is not None else None
        stdout_path = _safe_restored_log_path(record.get("stdout_path"), workspace=restored_workspace) if restored_workspace is not None else None
        stderr_path = _safe_restored_log_path(record.get("stderr_path"), workspace=restored_workspace) if restored_workspace is not None else None
        stdout_chunks, stdout_dropped = _read_log_tail(stdout_path) if stdout_path is not None else ([], 0)
        stderr_chunks, stderr_dropped = _read_log_tail(stderr_path) if stderr_path is not None else ([], 0)
        owner = record.get("owner_session_id")
        group_id = record.get("process_group_id")
        process = cast(subprocess.Popen[str], _DetachedProcess(pid=pid, exit_code=exit_code))
        state = BackgroundProcessState(
            process_id=process_id,
            command=str(record.get("command", "")),
            cwd=cwd,
            process=process,
            stdout_chunks=stdout_chunks,
            stderr_chunks=stderr_chunks,
            owner_session_id=owner if isinstance(owner, str) else None,
            process_identity=process_identity,
            process_group_id=group_id if isinstance(group_id, int) else None,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_dropped_lines=stdout_dropped,
            stderr_dropped_lines=stderr_dropped,
            status=status,
            prior_runtime=True,
            reconciliation_reason=reconciliation_reason,
            identity_match=identity_match,
            observed_running=observed_running,
            reconciled=False,
        )
        self._processes[process_id] = state
        if status == "running":
            reason = (
                "Prior-runtime managed process was observed after restart; it was not reattached "
                "and is externally managed/unavailable to this runtime. "
                f"PID identity match: {identity_match if identity_match is not None else 'unknown'}."
            )
            state.status = "stale"
            state.reconciliation_reason = reason
            if self._persistence is not None:
                mark_exit = getattr(self._persistence, "mark_background_process_exit", None)
                if callable(mark_exit):
                    mark_exit(
                        workspace=Path(state.cwd),
                        process_id=state.process_id,
                        status="stale",
                        exit_code=None,
                        reconciliation_reason=reason,
                    )

    def _authorize(
        self,
        state: BackgroundProcessState,
        *,
        workspace: Path | None,
        owner_session_id: str | None,
        enforce_owner: bool,
    ) -> None:
        if workspace is not None and state.cwd != str(workspace.resolve()):
            raise ValueError(f"background process {state.process_id} belongs to another workspace")
        if enforce_owner and state.owner_session_id != owner_session_id:
            raise ValueError(f"background process {state.process_id} belongs to another session")

    def _require(self, process_id: str, **kwargs: object) -> BackgroundProcessState:
        workspace = kwargs.get("workspace")
        owner_session_id = kwargs.get("owner_session_id")
        enforce_owner = kwargs.get("enforce_owner")
        state = self.load(
            process_id,
            workspace=workspace if isinstance(workspace, Path) else None,
            owner_session_id=owner_session_id if isinstance(owner_session_id, str) else None,
            enforce_owner=enforce_owner is True,
        )
        if state is None:
            raise ValueError(f"unknown background process: {process_id}")
        return state

    def _refresh(self, state: BackgroundProcessState) -> None:
        if state.prior_runtime:
            return
        if state.process.poll() is not None:
            self._mark_exit(state)

    def _mark_exit(
        self,
        state: BackgroundProcessState,
        *,
        status: str = "exited",
        reconciliation_reason: str | None = None,
    ) -> None:
        state.status = status
        if reconciliation_reason is not None:
            state.reconciliation_reason = reconciliation_reason
        if self._persistence is None:
            return
        mark_exit = getattr(self._persistence, "mark_background_process_exit", None)
        if callable(mark_exit):
            mark_exit(
                workspace=Path(state.cwd),
                process_id=state.process_id,
                status=status,
                exit_code=state.process.poll(),
                reconciliation_reason=reconciliation_reason,
            )

    @staticmethod
    def _start_reader(state: BackgroundProcessState, *, stream_name: str) -> None:
        path = state.stdout_path if stream_name == "stdout" else state.stderr_path
        if path is None:
            return

        def _read() -> None:
            initial, dropped = _read_log_tail(path)
            _append_log_lines(state, stream_name=stream_name, lines=initial)
            if stream_name == "stdout":
                state.stdout_dropped_lines = dropped
            else:
                state.stderr_dropped_lines = dropped
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    stream.seek(0, 2)
                    position = stream.tell()
                    while state.process.poll() is None:
                        time.sleep(0.03)
                        stream.seek(position)
                        lines = stream.readlines()
                        position = stream.tell()
                        _append_log_lines(state, stream_name=stream_name, lines=lines)
                    stream.seek(position)
                    _append_log_lines(state, stream_name=stream_name, lines=stream.readlines())
            except OSError:
                return

        threading.Thread(target=_read, name=f"background-process-{stream_name}", daemon=True).start()


def _terminate_background_process_group(process: subprocess.Popen[str]) -> None:
    if _is_windows():
        _terminate_windows_process_tree(process)
        return

    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if callable(killpg):
        process_group_id = process.pid
        try:
            killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        if sigkill is not None and _process_group_exists(process_group_id):
            try:
                killpg(process_group_id, sigkill)
            except ProcessLookupError:
                return
            if process.poll() is None:
                process.wait(timeout=1)
            _wait_for_process_group_exit(process_group_id, timeout=1)
        return

    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _is_windows() -> bool:
    return os.name == "nt"


def _terminate_windows_process_tree(process: subprocess.Popen[str]) -> None:
    taskkill = shutil.which("taskkill") or "taskkill"
    completed = subprocess.run(
        [taskkill, "/PID", str(process.pid), "/T", "/F"],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode == 0:
        return
    process.kill()
    process.wait(timeout=1)


def _process_group_exists(process_group_id: int) -> bool:
    killpg = getattr(os, "killpg", None)
    if not callable(killpg):
        return False
    try:
        killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(process_group_id: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _process_group_exists(process_group_id):
        time.sleep(0.02)


__all__ = [
    "BackgroundProcessManager",
    "BackgroundProcessPersistence",
    "BackgroundProcessState",
    "_DetachedProcess",
    "_MAX_BACKGROUND_PROCESS_LOG_LINES",
    "_process_group_exists",
    "_terminate_background_process_group",
]
