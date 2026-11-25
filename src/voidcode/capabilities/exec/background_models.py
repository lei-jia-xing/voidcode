"""Result bodies and shared literal types for `background_process`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

#: The two streams the host serves; `both` is a selection, not a stream.
type _StreamName = Literal["stdout", "stderr"]
type ProcessLiveness = Literal["running", "exited"]
type ProcessRowState = ProcessLiveness | Literal["externally managed"]
type LogStreamSelection = Literal["stdout", "stderr", "both"]


@dataclass(frozen=True, slots=True)
class BackgroundProcessRow:
    """One process as the caller sees it: this host's truth, plus the persisted description."""

    process_id: str
    pid: int
    state: ProcessRowState
    description: str
    command: str
    stdout_path: str
    stderr_path: str
    started_at: str | None = None
    exit_code: int | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "pid": self.pid,
            "state": self.state,
            "description": self.description,
            "command": self.command,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "started_at": self.started_at,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True, slots=True)
class BackgroundProcessListBody:
    processes: tuple[BackgroundProcessRow, ...]
    count: int
    limit: int

    def as_payload(self) -> dict[str, object]:
        return {
            "processes": [process.as_payload() for process in self.processes],
            "count": self.count,
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class BackgroundProcessStartBody:
    process_id: str
    resource_uri: str
    pid: int
    state: ProcessLiveness
    command: str
    cwd: str
    description: str
    stdout_path: str
    stderr_path: str
    started_at: str
    running: bool
    ready: bool | None
    ready_pattern: str | None
    exit_code: int | None
    guidance: str

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "resource_uri": self.resource_uri,
            "pid": self.pid,
            "state": self.state,
            "command": self.command,
            "cwd": self.cwd,
            "description": self.description,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "started_at": self.started_at,
            "running": self.running,
            "ready": self.ready,
            "ready_pattern": self.ready_pattern,
            "exit_code": self.exit_code,
            "guidance": self.guidance,
        }


@dataclass(frozen=True, slots=True)
class BackgroundProcessLogsBody:
    process_id: str
    state: ProcessLiveness
    stream: LogStreamSelection
    offset: int
    limit: int
    stdout: str
    stderr: str
    stdout_lines: int
    stderr_lines: int
    running: bool
    exit_code: int | None
    truncated: bool
    next_offset: int | None
    next_offsets: Mapping[_StreamName, int]
    guidance: str

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "state": self.state,
            "stream": self.stream,
            "offset": self.offset,
            "limit": self.limit,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_lines": self.stdout_lines,
            "stderr_lines": self.stderr_lines,
            "running": self.running,
            "exit_code": self.exit_code,
            "truncated": self.truncated,
            "next_offset": self.next_offset,
            "next_offsets": dict(self.next_offsets),
            "guidance": self.guidance,
        }


@dataclass(frozen=True, slots=True)
class BackgroundProcessSendBody:
    process_id: str
    input: str
    newline: bool
    requested_bytes: int
    written_bytes: int
    error: str | None

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "input": self.input,
            "newline": self.newline,
            "requested_bytes": self.requested_bytes,
            "written_bytes": self.written_bytes,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class BackgroundProcessStopBody:
    process_id: str
    state: ProcessLiveness
    exit_code: int | None
    running: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "state": self.state,
            "exit_code": self.exit_code,
            "running": self.running,
        }


@dataclass(frozen=True, slots=True)
class BackgroundProcessExternalBody:
    """A persisted identity this host does not supervise: what the caller may know, and no more."""

    process_id: str
    pid: int
    start_time: str
    command: str
    description: str
    stdout_path: str
    stderr_path: str
    reason: str
    state: Literal["externally managed"] = "externally managed"

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "pid": self.pid,
            "start_time": self.start_time,
            "command": self.command,
            "description": self.description,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "state": self.state,
            "reason": self.reason,
        }
