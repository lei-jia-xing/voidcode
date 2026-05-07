from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ._pydantic_args import format_validation_error
from .contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolResult
from .runtime_context import current_runtime_tool_context

_MAX_BACKGROUND_PROCESS_LOG_LINES = 500


@dataclass(slots=True)
class BackgroundProcessState:
    process_id: str
    command: str
    cwd: str
    process: subprocess.Popen[str]
    stdout_chunks: list[str]
    stderr_chunks: list[str]
    stdout_dropped_lines: int = 0
    stderr_dropped_lines: int = 0
    stdout_artifact: dict[str, object] | None = None
    stderr_artifact: dict[str, object] | None = None


class BackgroundProcessManager:
    def __init__(self) -> None:
        self._processes: dict[str, BackgroundProcessState] = {}
        self._lock = threading.RLock()

    def start(self, *, command: str, workspace: Path) -> BackgroundProcessState:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        state = BackgroundProcessState(
            process_id=f"proc-{uuid.uuid4().hex}",
            command=command,
            cwd=str(workspace),
            process=process,
            stdout_chunks=[],
            stderr_chunks=[],
            stdout_dropped_lines=0,
            stderr_dropped_lines=0,
        )
        with self._lock:
            self._processes[state.process_id] = state
        self._start_reader(state, stream_name="stdout")
        self._start_reader(state, stream_name="stderr")
        return state

    def load(self, process_id: str) -> BackgroundProcessState | None:
        with self._lock:
            return self._processes.get(process_id)

    def stop(self, process_id: str) -> BackgroundProcessState:
        state = self._require(process_id)
        if state.process.poll() is None:
            _terminate_background_process_group(state.process)
        return state

    def stop_all(self) -> None:
        with self._lock:
            process_ids = tuple(self._processes)
        for process_id in process_ids:
            try:
                self.stop(process_id)
            except ValueError:
                continue

    def _require(self, process_id: str) -> BackgroundProcessState:
        state = self.load(process_id)
        if state is None:
            raise ValueError(f"unknown background process: {process_id}")
        return state

    @staticmethod
    def _start_reader(state: BackgroundProcessState, *, stream_name: str) -> None:
        stream = state.process.stdout if stream_name == "stdout" else state.process.stderr

        def _read() -> None:
            if stream is None:
                return
            for line in stream:
                if stream_name == "stdout":
                    state.stdout_chunks.append(line)
                    if len(state.stdout_chunks) > _MAX_BACKGROUND_PROCESS_LOG_LINES:
                        state.stdout_chunks.pop(0)
                        state.stdout_dropped_lines += 1
                else:
                    state.stderr_chunks.append(line)
                    if len(state.stderr_chunks) > _MAX_BACKGROUND_PROCESS_LOG_LINES:
                        state.stderr_chunks.pop(0)
                        state.stderr_dropped_lines += 1

        threading.Thread(
            target=_read,
            name=f"background-process-{stream_name}",
            daemon=True,
        ).start()


def _terminate_background_process_group(process: subprocess.Popen[str]) -> None:
    if _is_windows():
        _terminate_windows_process_tree(process)
        return

    killpg = getattr(os, "killpg", None)
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

        if _process_group_exists(process_group_id):
            try:
                killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                return
            if process.poll() is None:
                process.wait(timeout=1)
            _wait_for_process_group_exit(process_group_id, timeout=1)
            return

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
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(process_group_id: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group_id):
            return
        time.sleep(0.02)


class _BackgroundProcessStartArgs(BaseModel):
    command: str
    description: str | None = None

    @field_validator("command", mode="after")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty")
        return value

    @field_validator("description", mode="after")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("description must not be empty when provided")
        return value


class BackgroundProcessStartRuntime(Protocol):
    @property
    def background_process_manager(self) -> BackgroundProcessManager: ...


class BackgroundProcessStartTool:
    definition = ToolDefinition(
        name="background_process_start",
        description=(
            "Start a long-running non-interactive process without blocking the current turn."
        ),
        input_schema={
            "command": {"type": "string"},
            "description": {"type": "string"},
        },
        read_only=False,
    )

    def __init__(self, *, runtime: BackgroundProcessStartRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = _BackgroundProcessStartArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        runtime_context = current_runtime_tool_context()
        if runtime_context is not None and runtime_context.abort_signal is not None:
            if runtime_context.abort_signal.cancelled:
                raise RuntimeToolTimeoutError(
                    "background_process_start aborted before launching process"
                )

        state = self._runtime.background_process_manager.start(
            command=args.command, workspace=workspace
        )
        pid = state.process.pid
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=(
                f"Started background process {state.process_id} (pid={pid}) "
                f"for command: {args.command}. "
                f"Use background_process_logs(process_id='{state.process_id}') to inspect output."
            ),
            data={
                "process_id": state.process_id,
                "pid": pid,
                "command": args.command,
                "cwd": state.cwd,
                "running": state.process.poll() is None,
            },
        )
