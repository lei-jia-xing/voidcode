"""The `background_process` tool: workspace processes supervised by the plugin host.

Ported from the runtime-owned process command (``voidcode.runtime.execution.process``) onto the
core's background-process service. The host owns the live process, its group, and the log
files — this tool never spawns a child itself. What it owns is the contract around them: the shell
wrapper, the legacy log layout, the ready-pattern wait, line paged logs, stdin writes, and the
identity it persists in workspace state (pid + start time + log paths). That persisted identity is
what lets a later host report a process it no longer supervises as externally managed instead of
guessing from a pid.

P4 boundary (accepted): every start mints a fresh id; legacy reuse-by-exact-command-match is
dropped deliberately, and the core's 'already running' refusal for a repeated id propagates.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, ClassVar, Literal, final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices, ProcessHandle
from voidcode.sdk.tools import (
    ConfirmedStop,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from .args import NonEmptyCommand, NonEmptyProcessId, OptionalDescription, format_validation_error
from .shell_exec import _shell_argv

#: Row bound of one `ps` listing, as the legacy command had.
MAX_PROCESS_ROWS = 64
#: Byte window of one log read, inside the host's own page bound.
_LOG_PAGE_BYTES = 64 * 1024
#: Ceiling on the bytes one `logs` call scans, so a runaway log costs a bounded read.
_LOG_SCAN_BYTES = 8 * 1024 * 1024
#: Tail window of both streams scanned for the ready pattern, as the legacy wait scanned.
_READY_SCAN_BYTES = 64 * 1024
#: Poll interval of the ready wait, as the legacy manager polled.
_READY_POLL_SECONDS = 0.03
#: Persisted workspace-state shape (versioned so a future shape can be migrated, not guessed).
_STATE_VERSION = 1
_LOG_DIR_PARTS = (".voidcode", "background-processes")
#: The service calls this tool cannot work without; a handler missing them fails visibly.
_PROCESS_SERVICES = (
    "spawn_process",
    "write_process",
    "read_process_output",
    "cancel_process",
    "list_processes",
    "get_state",
    "replace_state",
)
_EXTERNAL_MESSAGE = (
    "background process {process_id} is externally managed: this host does not supervise it "
    "(it survived a host restart or was started by another session), so its logs, stdin, and "
    "process group are unavailable to this session"
)
_UNKNOWN_MESSAGE = "unknown background process: {process_id}"
_STOP_MESSAGE = "could not confirm background process {process_id} stopped; process group remains or termination is unconfirmed"
_PARTIAL_WRITE_MESSAGE = "process stdin accepted only part of the input"

#: The two streams the host serves; `both` is a selection, not a stream.
type _StreamName = Literal["stdout", "stderr"]


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StartArgs(_StrictArgs):
    op: Literal["start"]
    command: NonEmptyCommand
    description: OptionalDescription = None
    ready_pattern: str | None = Field(default=None, min_length=1, max_length=512)
    ready_timeout: int = Field(default=10, ge=1, le=30)


class _PsArgs(_StrictArgs):
    op: Literal["ps"]


class _ProcessIdArgs(_StrictArgs):
    process_id: NonEmptyProcessId


class _LogsArgs(_ProcessIdArgs):
    op: Literal["logs"]
    stream: Literal["stdout", "stderr", "both"] = "both"
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=500, ge=1, le=500)


class _SendArgs(_ProcessIdArgs):
    op: Literal["send"]
    input: str
    newline: bool = True

    @field_validator("input", mode="after")
    @classmethod
    def _validate_input(cls, value: str) -> str:
        if not value:
            raise ValueError("input must be a non-empty string")
        return value


class _StopArgs(_ProcessIdArgs):
    op: Literal["stop"]


_ProcessArgs = Annotated[_StartArgs | _PsArgs | _LogsArgs | _SendArgs | _StopArgs, Field(discriminator="op")]
_ARGS_ADAPTER = TypeAdapter(_ProcessArgs)


@dataclass(frozen=True, slots=True)
class BackgroundProcessRow:
    """One process as the caller sees it: this host's truth, plus the persisted description."""

    process_id: str
    pid: int
    state: str
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
    pid: int
    state: str
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
    state: str
    stream: str
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
    next_offsets: Mapping[str, int]
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
    state: str
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
    state: str = "externally managed"

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


@dataclass(frozen=True, slots=True)
class _StreamScan:
    """One stream's log lines as read through the host's bounded byte windows."""

    lines: list[str]
    truncated: bool


def _require_process_service(services: object) -> None:
    """Refuse a handler without the core's background-process service (never spawn locally)."""
    for name in _PROCESS_SERVICES:
        if not callable(getattr(services, name, None)):
            raise ValueError("background_process requires the core background process service")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_records(services: PluginServices) -> tuple[dict[str, Mapping[str, object]], int]:
    """The workspace's persisted identities and their version; a missing or empty row is empty."""
    state, version = services.get_state("workspace")
    records: dict[str, Mapping[str, object]] = {}
    raw = state.get("processes") if state is not None else None
    if isinstance(raw, Mapping):
        for process_id, record in raw.items():
            if isinstance(process_id, str) and isinstance(record, Mapping):
                records[process_id] = {key: value for key, value in record.items() if isinstance(key, str)}
    return records, version


def _write_records(services: PluginServices, records: Mapping[str, Mapping[str, object]], version: int) -> int:
    return services.replace_state(
        {"schema_version": _STATE_VERSION, "processes": {name: dict(record) for name, record in records.items()}},
        expected_version=version,
        scope="workspace",
    )


def _with_exit(record: Mapping[str, object], *, state: str, exit_code: int | None) -> dict[str, object]:
    """The record after a process ended: the state, and its exit code when the host has one."""
    updated = {**record, "state": state}
    if exit_code is not None:
        updated["exit_code"] = exit_code
    return updated


def _record_value(record: Mapping[str, object], key: str, default: str = "") -> str:
    value = record.get(key)
    return value if isinstance(value, str) else default


def _record_int(record: Mapping[str, object], key: str) -> int | None:
    value = record.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _identity_record(*, handle: ProcessHandle, command: str, description: str, state: str) -> dict[str, object]:
    record: dict[str, object] = {
        "process_id": handle.process_id,
        "pid": handle.pid,
        "start_time": handle.start_time,
        "description": description,
        "command": command,
        "stdout_path": handle.stdout_path,
        "stderr_path": handle.stderr_path,
        "started_at": _now(),
        "state": state,
    }
    if handle.exit_code is not None:
        record["exit_code"] = handle.exit_code
    return record


def _live_handle(services: PluginServices, process_id: str) -> ProcessHandle | None:
    """This session's handle for one process; `None` when the host does not supervise that id."""
    for handle in services.list_processes():
        if handle.process_id == process_id:
            return handle
    return None


def _scan_stream(services: PluginServices, process_id: str, stream: _StreamName) -> _StreamScan:
    """Every line of one stream's log, read out of the host's bounded byte windows.

    Lines keep their endings, so a page of them is the log's own bytes. The scan stops at a byte
    ceiling: a runaway log costs a bounded read, and the caller is told `truncated` rather than
    handed a silently short page.
    """
    text = ""
    offset = 0
    while True:
        page = services.read_process_output(process_id, stream=stream, offset=offset, limit=_LOG_PAGE_BYTES)
        offset += len(page.data)
        if page.data:
            text += page.text.replace("\r\n", "\n")
        if page.eof or not page.data or offset >= _LOG_SCAN_BYTES:
            return _StreamScan(lines=text.splitlines(keepends=True), truncated=not page.eof)


def _wait_for_ready(services: PluginServices, process_id: str, *, pattern: str | None, timeout: float) -> bool | None:
    """Whether the process printed `pattern`; `None` when no pattern was asked for.

    Both streams are polled from the byte the last poll consumed, keeping the legacy tail window,
    and a process that exits first ends the wait without waiting out the whole timeout.
    """
    if pattern is None:
        return None
    deadline = time.monotonic() + timeout
    offsets = {"stdout": 0, "stderr": 0}
    scanned = {"stdout": "", "stderr": ""}
    while True:
        running = True
        for stream in ("stdout", "stderr"):
            page = services.read_process_output(process_id, stream=stream, offset=offsets[stream], limit=_LOG_PAGE_BYTES)
            offsets[stream] += len(page.data)
            running = page.running
            if not page.data:
                continue
            window = scanned[stream] + page.text.replace("\r\n", "\n")
            scanned[stream] = window[-_READY_SCAN_BYTES:]
            if any(pattern in line for line in scanned[stream].splitlines()):
                return True
        if not running or time.monotonic() >= deadline:
            return False
        time.sleep(_READY_POLL_SECONDS)


def _start_guidance(process_id: str) -> str:
    return f"Track this process by process_id '{process_id}'. Use background_process with op=logs, send, or stop."


def _logs_guidance(*, running: bool) -> str:
    if running:
        return "This is a bounded page from the current log snapshot; use offset to inspect another page or continue other work."
    return "This is a bounded page from the final log snapshot; use offset to inspect another page if needed."


def _write_message(written: int, *, requested: int, process_id: str) -> str:
    return f"Wrote {written} of {requested} UTF-8 bytes to background process {process_id}."


def _external_failure(*, tool_name: str, process_id: str, record: Mapping[str, object]) -> ToolFailure[BackgroundProcessExternalBody]:
    message = _EXTERNAL_MESSAGE.format(process_id=process_id)
    return ToolFailure(
        tool_name=tool_name,
        error=message,
        output=TextOutput(message),
        body=BackgroundProcessExternalBody(
            process_id=process_id,
            pid=_record_int(record, "pid") or 0,
            start_time=_record_value(record, "start_time"),
            command=_record_value(record, "command"),
            description=_record_value(record, "description"),
            stdout_path=_record_value(record, "stdout_path"),
            stderr_path=_record_value(record, "stderr_path"),
            reason=message,
        ),
    )


@final
class BackgroundProcessTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="background_process",
        description=(
            "Manage a long-running background process. Use op=start to launch, ps to list the current workspace processes, "
            "logs to read bounded output, send to write stdin, or stop to terminate a process. "
            "Process rows are scoped to the current owner and workspace."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": True,
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "command"],
                    "properties": {
                        "op": {"const": "start", "description": "Start a supervised process that outlives this call."},
                        "command": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Shell command to run in the workspace for the lifetime of the process.",
                        },
                        "description": {"type": "string", "description": "Human-readable description of the process, persisted with its identity."},
                        "ready_pattern": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                            "description": "Text to wait for in the process output before returning.",
                        },
                        "ready_timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 30,
                            "default": 10,
                            "description": "Seconds to wait for ready_pattern (1-30, default 10).",
                        },
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op"],
                    "properties": {"op": {"const": "ps", "description": "List the processes of this workspace and session."}},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "process_id"],
                    "properties": {
                        "op": {"const": "logs", "description": "Read a bounded, line-paged window of a process's output."},
                        "process_id": {"type": "string", "description": "Process id returned by op=start."},
                        "stream": {
                            "type": "string",
                            "enum": ["stdout", "stderr", "both"],
                            "default": "both",
                            "description": "Which output stream to read.",
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 1000000,
                            "default": 0,
                            "description": "Number of leading lines to skip; page from next_offset to read on.",
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 500, "description": "Maximum lines to return (1-500)."},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "process_id", "input"],
                    "properties": {
                        "op": {"const": "send", "description": "Write to a process's standard input."},
                        "process_id": {"type": "string", "description": "Process id returned by op=start."},
                        "input": {"type": "string", "description": "Text written to the process's standard input."},
                        "newline": {"type": "boolean", "default": True, "description": "Append a newline to input (default true)."},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "process_id"],
                    "properties": {
                        "op": {"const": "stop", "description": "Stop a process and its whole process group."},
                        "process_id": {"type": "string", "description": "Process id returned by op=start."},
                    },
                },
            ],
        },
        effects=frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        _require_process_service(services)
        workspace = context.require_workspace().resolve()
        context.require_session_id()
        try:
            args = _ARGS_ADAPTER.validate_python(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc
        if isinstance(args, _StartArgs):
            return self._start(
                args,
                workspace=workspace,
                services=services,
                aborted=context.abort_signal is not None and context.abort_signal.cancelled,
            )
        if isinstance(args, _LogsArgs):
            return self._logs(args, services=services)
        if isinstance(args, _SendArgs):
            return self._send(args, services=services)
        if isinstance(args, _StopArgs):
            return self._stop(args, services=services)
        return self._ps(services=services)

    def _start(self, args: _StartArgs, *, workspace: Path, services: PluginServices, aborted: bool) -> ToolResult:
        if aborted:
            # Pre-flight refusal: a cancelled call launches no process, so nothing is unknown.
            message = "background_process start aborted before launching the process"
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(message),
                execution=ConfirmedStop(cancellation_signalled=True),
            )
        records, version = _read_records(services)
        process_id = f"proc-{uuid4().hex}"
        log_dir = workspace.joinpath(*_LOG_DIR_PARTS)
        log_dir.mkdir(parents=True, exist_ok=True)
        # The host refuses a duplicate id outright, so a start can never shadow a live process.
        handle = services.spawn_process(
            _shell_argv(args.command),
            cwd=workspace,
            stdin=True,
            stdout_path=log_dir / f"{process_id}.stdout.log",
            stderr_path=log_dir / f"{process_id}.stderr.log",
            process_id=process_id,
        )
        # The identity is persisted before the wait: a start that cannot report back still left
        # the caller a handle on the process it started.
        record = _identity_record(
            handle=handle,
            command=args.command,
            description=args.description or "",
            state="running",
        )
        records[handle.process_id] = record
        version = _write_records(services, records, version)
        ready = _wait_for_ready(services, handle.process_id, pattern=args.ready_pattern, timeout=float(args.ready_timeout))
        current = _live_handle(services, handle.process_id) or handle
        running = current.running
        state = "running" if running else "exited"
        if not running:
            # The mirror follows the process it names; nothing else refreshes this attempt's exit.
            record = _with_exit(record, state=state, exit_code=current.exit_code)
            records[handle.process_id] = record
            version = _write_records(services, records, version)
        guidance = _start_guidance(handle.process_id)
        readiness = ""
        if args.ready_pattern is not None and ready is not True:
            if running:
                readiness = f" Readiness pattern {args.ready_pattern!r} was not observed within {args.ready_timeout}s; process is still running."
            else:
                readiness = f" Process exited with code {current.exit_code} before readiness pattern {args.ready_pattern!r} appeared."
        content = f"Started background process {handle.process_id} (pid={handle.pid}) for command: {args.command}.{readiness} Guidance: {guidance}"
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(content),
            body=BackgroundProcessStartBody(
                process_id=handle.process_id,
                pid=handle.pid,
                state=state,
                command=args.command,
                cwd=str(workspace),
                description=args.description or "",
                stdout_path=handle.stdout_path,
                stderr_path=handle.stderr_path,
                started_at=_record_value(record, "started_at"),
                running=running,
                ready=ready,
                ready_pattern=args.ready_pattern,
                exit_code=current.exit_code,
                guidance=guidance,
            ),
        )

    def _ps(self, *, services: PluginServices) -> ToolResult:
        records, _ = _read_records(services)
        rows: list[BackgroundProcessRow] = []
        live: set[str] = set()
        for handle in services.list_processes():
            live.add(handle.process_id)
            record = records.get(handle.process_id, {})
            rows.append(
                BackgroundProcessRow(
                    process_id=handle.process_id,
                    pid=handle.pid,
                    state="running" if handle.running else "exited",
                    description=_record_value(record, "description"),
                    command=_record_value(record, "command"),
                    stdout_path=handle.stdout_path,
                    stderr_path=handle.stderr_path,
                    started_at=_record_value(record, "started_at") or None,
                    exit_code=handle.exit_code,
                )
            )
        for process_id, record in records.items():
            if process_id in live:
                continue
            rows.append(
                BackgroundProcessRow(
                    process_id=process_id,
                    pid=_record_int(record, "pid") or 0,
                    state="externally managed",
                    description=_record_value(record, "description"),
                    command=_record_value(record, "command"),
                    stdout_path=_record_value(record, "stdout_path"),
                    stderr_path=_record_value(record, "stderr_path"),
                    started_at=_record_value(record, "started_at") or None,
                    exit_code=_record_int(record, "exit_code"),
                )
            )
        rows.sort(key=lambda row: row.process_id)
        listed = tuple(rows[:MAX_PROCESS_ROWS])
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(f"Background process list: {len(listed)} process(es)."),
            body=BackgroundProcessListBody(processes=listed, count=len(listed), limit=MAX_PROCESS_ROWS),
        )

    def _logs(self, args: _LogsArgs, *, services: PluginServices) -> ToolResult:
        records, _ = _read_records(services)
        handle = _live_handle(services, args.process_id)
        if handle is None:
            return self._unsupervised(records=records, process_id=args.process_id)
        selected: tuple[_StreamName, ...] = ("stdout", "stderr") if args.stream == "both" else (args.stream,)
        pages = {"stdout": "", "stderr": ""}
        counts = {"stdout": 0, "stderr": 0}
        next_offsets: dict[str, int] = {}
        truncated = args.offset > 0
        for stream in selected:
            scan = _scan_stream(services, args.process_id, stream)
            page_lines = scan.lines[args.offset : args.offset + args.limit]
            pages[stream] = "".join(page_lines)
            counts[stream] = len(page_lines)
            if args.offset + len(page_lines) < len(scan.lines) or scan.truncated:
                next_offsets[stream] = args.offset + len(page_lines)
            truncated = truncated or scan.truncated or bool(next_offsets)
        current = _live_handle(services, args.process_id) or handle
        running = current.running
        guidance = _logs_guidance(running=running)
        sections = [f"[{name}]\n{pages[name]}" for name in selected if pages[name]]
        output = "\n".join(sections)
        output = f"{output}\n\nGuidance: {guidance}" if output else f"Guidance: {guidance}"
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(output),
            body=BackgroundProcessLogsBody(
                process_id=args.process_id,
                state="running" if running else "exited",
                stream=args.stream,
                offset=args.offset,
                limit=args.limit,
                stdout=pages["stdout"],
                stderr=pages["stderr"],
                stdout_lines=counts["stdout"],
                stderr_lines=counts["stderr"],
                running=running,
                exit_code=current.exit_code,
                truncated=truncated,
                next_offset=next_offsets.get(args.stream) if args.stream != "both" else None,
                next_offsets=next_offsets,
                guidance=guidance,
            ),
        )

    def _send(self, args: _SendArgs, *, services: PluginServices) -> ToolResult:
        records, _ = _read_records(services)
        handle = _live_handle(services, args.process_id)
        if handle is None:
            return self._unsupervised(records=records, process_id=args.process_id)
        payload = args.input + ("\n" if args.newline else "")
        requested = len(payload.encode("utf-8"))
        message = _write_message(0, requested=requested, process_id=args.process_id)
        try:
            written = services.write_process(args.process_id, payload)
        except ValueError as exc:
            error = f"background process {args.process_id} is not accepting input: {exc}"
            return ToolFailure(
                tool_name=self.definition.name,
                error=error,
                output=TextOutput(message),
                body=BackgroundProcessSendBody(
                    process_id=args.process_id,
                    input=args.input,
                    newline=args.newline,
                    requested_bytes=requested,
                    written_bytes=0,
                    error=error,
                ),
            )
        body = BackgroundProcessSendBody(
            process_id=args.process_id,
            input=args.input,
            newline=args.newline,
            requested_bytes=requested,
            written_bytes=written,
            error=None if written >= requested else _PARTIAL_WRITE_MESSAGE,
        )
        output = TextOutput(_write_message(written, requested=requested, process_id=args.process_id))
        if body.error is not None:
            return ToolFailure(tool_name=self.definition.name, error=body.error, output=output, body=body)
        return ToolSuccess(tool_name=self.definition.name, output=output, body=body)

    def _stop(self, args: _StopArgs, *, services: PluginServices) -> ToolResult:
        records, version = _read_records(services)
        handle = _live_handle(services, args.process_id)
        if handle is None:
            return self._unsupervised(records=records, process_id=args.process_id)
        if not services.cancel_process(args.process_id, reason="background_process stop"):
            message = _STOP_MESSAGE.format(process_id=args.process_id)
            return ToolFailure(tool_name=self.definition.name, error=message, output=TextOutput(message))
        current = _live_handle(services, args.process_id) or handle
        exit_code = current.exit_code
        record = records.get(args.process_id)
        if record is not None:
            records[args.process_id] = _with_exit(record, state="exited", exit_code=exit_code)
            _write_records(services, records, version)
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(f"Stopped background process {args.process_id}."),
            body=BackgroundProcessStopBody(
                process_id=args.process_id,
                state="exited",
                exit_code=exit_code,
                running=current.running,
            ),
        )

    def _unsupervised(self, *, records: Mapping[str, Mapping[str, object]], process_id: str) -> ToolResult:
        """Refuse an id this host does not supervise, naming an unknown one as unknown."""
        record = records.get(process_id)
        if record is None:
            raise ValueError(_UNKNOWN_MESSAGE.format(process_id=process_id))
        return _external_failure(tool_name=self.definition.name, process_id=process_id, record=record)


__all__ = [
    "BackgroundProcessExternalBody",
    "BackgroundProcessListBody",
    "BackgroundProcessLogsBody",
    "BackgroundProcessRow",
    "BackgroundProcessSendBody",
    "BackgroundProcessStartBody",
    "BackgroundProcessStopBody",
    "BackgroundProcessTool",
]
