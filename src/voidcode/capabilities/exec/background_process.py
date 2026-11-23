"""Background process tools over core-owned process supervision.

The core owns live processes and byte-paged logs. This tool persists identities,
provides line-paged views, waits for readiness text and forwards stdin/control.
Persisted records are history; live control requires a current service handle.
Every start mints a fresh identity.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, ClassVar, Literal, final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from voidcode.capabilities.tool_errors import expected_failures, raise_tool_diagnostic
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.plugin import StateScope
from voidcode.sdk.resources import ResourceReaderServices
from voidcode.sdk.services import PluginServices, ProcessHandle, ProcessServices, StateServices
from voidcode.sdk.tools import (
    TOOL_OUTPUT_BYTES,
    ConfirmedStop,
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolDiagnostics,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
    UnconfirmedStop,
)

from .args import NonEmptyCommand, NonEmptyProcessId, OptionalDescription
from .shell_exec import _shell_argv

#: Row bound of one `ps` listing, as the legacy command had.
MAX_PROCESS_ROWS = 64
#: One `send` payload, including the optional newline; larger input is refused before it is written.
MAX_SEND_BYTES = 64 * 1024
#: Byte window of one log read, inside the host's own page bound.
_LOG_PAGE_BYTES = 64 * 1024
#: Ceiling on the bytes one `logs` call scans, so a runaway log costs a bounded read.
_LOG_SCAN_BYTES = 8 * 1024 * 1024
#: Model-visible line cap, matching the read tool's display policy.
_LOG_LINE_CHARS = 2000
_LOG_LINE_SUFFIX = f"... (line truncated to {_LOG_LINE_CHARS} chars)"
#: Model-visible content budget; the rest of a large page stays available at its next offset.
_LOG_RENDER_BYTES = TOOL_OUTPUT_BYTES - 4096
#: Tail window of both streams scanned for the ready pattern, as the legacy wait scanned.
_READY_SCAN_BYTES = 64 * 1024
#: Poll interval of the ready wait, as the legacy manager polled.
_READY_POLL_SECONDS = 0.03
#: Current persisted process-record payload version; unsupported versions fail visibly.
_STATE_VERSION = 1
_EXTERNAL_MESSAGE = (
    "background process {process_id} is externally managed: this host does not supervise it "
    "(it is not a retained job; it may have survived a host restart or been evicted after exit), so its logs, stdin, and "
    "process group are unavailable to this session"
)
_UNKNOWN_MESSAGE = "unknown background process: {process_id}"
_STOP_MESSAGE = "could not confirm background process {process_id} stopped; process group remains or termination is unconfirmed"
_PARTIAL_WRITE_MESSAGE = "process stdin accepted only part of the input"

#: The two streams the host serves; `both` is a selection, not a stream.
type _StreamName = Literal["stdout", "stderr"]


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _StartArgs(_StrictArgs):
    op: Literal["start"] = Field(description="Start a supervised process that outlives this call.")
    command: NonEmptyCommand = Field(description="Shell command to run in the workspace for the lifetime of the process.")
    description: OptionalDescription = Field(default=None, description="Human-readable description of the process, persisted with its identity.")
    ready_pattern: str | None = Field(
        default=None, min_length=1, max_length=512, description="Text to wait for in the process output before returning."
    )
    ready_timeout: int = Field(default=10, ge=1, le=30, description="Seconds to wait for ready_pattern (1-30, default 10).")


class _PsArgs(_StrictArgs):
    op: Literal["ps"] = Field(description="List the processes of this workspace and session.")


class _ProcessIdArgs(_StrictArgs):
    process_id: NonEmptyProcessId = Field(description="Process id returned by op=start.")


class _LogsArgs(_ProcessIdArgs):
    op: Literal["logs"] = Field(description="Read a bounded, line-paged window of a process's output.")
    stream: Literal["stdout", "stderr", "both"] = Field(default="both", description="Which output stream to read.")
    offset: int = Field(default=0, ge=0, description="Number of leading lines to skip; page from next_offset to read on.")
    limit: int = Field(default=500, ge=1, le=500, description="Maximum lines to return (1-500).")


class _SendArgs(_ProcessIdArgs):
    op: Literal["send"] = Field(description="Write to a process's standard input.")
    input: str = Field(min_length=1, max_length=MAX_SEND_BYTES, description="Text written to the process's standard input.")
    newline: bool = Field(default=True, description="Append a newline to input (default true).")


class _StopArgs(_ProcessIdArgs):
    op: Literal["stop"] = Field(description="Stop a process and its whole process group.")


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
    resource_uri: str
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
    bytes_scanned: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _ProcessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    process_id: NonEmptyProcessId
    origin_session_id: str | None = Field(default=None, min_length=1)
    pid: int = Field(gt=0)
    start_time: str = Field(min_length=1)
    description: str
    command: NonEmptyCommand
    stdout_path: str = Field(min_length=1)
    stderr_path: str = Field(min_length=1)
    started_at: str = Field(min_length=1)
    state: Literal["running", "exited"]
    exit_code: int | None = None


class _ProcessState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    schema_version: int = Field(ge=1, le=1)
    processes: dict[str, _ProcessRecord]


def _read_records(services: StateServices | ResourceReaderServices) -> tuple[dict[str, _ProcessRecord], int]:
    """Validate persisted identities once; malformed state is never an empty inventory."""
    state, version = services.get_state("workspace")
    if state is None:
        return {}, version
    try:
        parsed = _ProcessState.model_validate(json_wire_object(state))
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "<state>"
        raise_tool_diagnostic(
            message=f"background process state is unreadable: {location}: {first.get('msg', 'invalid value')}",
            error_kind="tool_execution_error",
            reason="corrupt_process_state",
            retry_guidance="The persisted process inventory is malformed; repair or reset workspace state before retrying.",
            details={"error": str(first.get("msg", "invalid value"))},
        )
    for process_id, record in parsed.processes.items():
        if record.process_id != process_id:
            raise_tool_diagnostic(
                message="background process state is unreadable: a record id does not match its state key",
                error_kind="tool_execution_error",
                reason="corrupt_process_state",
                retry_guidance="The persisted process inventory is malformed; repair or reset workspace state before retrying.",
            )
    return parsed.processes, version


def _write_records(services: StateServices, records: Mapping[str, _ProcessRecord], version: int) -> int:
    return services.replace_state(
        {"schema_version": _STATE_VERSION, "processes": {name: record.model_dump(exclude_unset=True) for name, record in records.items()}},
        expected_version=version,
        scope="workspace",
    )


def prepare_exec_deactivation(
    state: Mapping[str, object],
    scope: StateScope,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Pure deactivation transform: persisted process identities survive untouched as history.

    Records are identities (pid + start time + log paths), not supervision handles: the host
    owns live processes and supervised background jobs survive a generation swap by design.
    Disabling exec only drops the control surface until re-enable, when the same identities
    re-attach to still-supervised ids or report honestly as externally managed. Nothing here
    stops a process, and a running process never makes exec undisablable.
    """
    _ = (scope, scope_key, tip_sequence)
    return state


def _with_exit(record: _ProcessRecord, *, exit_code: int | None) -> _ProcessRecord:
    """Preserve a known historical exit code when the host has no new answer."""
    if exit_code is None:
        return record.model_copy(update={"state": "exited"})
    return record.model_copy(update={"state": "exited", "exit_code": exit_code})


def _identity_record(*, handle: ProcessHandle, command: str, description: str, origin_session_id: str) -> _ProcessRecord:
    record = _ProcessRecord(
        process_id=handle.process_id,
        origin_session_id=origin_session_id,
        pid=handle.pid,
        start_time=handle.start_time,
        description=description,
        command=command,
        stdout_path=handle.stdout_path,
        stderr_path=handle.stderr_path,
        started_at=_now(),
        state="running",
    )
    if handle.exit_code is not None:
        record.exit_code = handle.exit_code
    return record


def _live_handle(services: ProcessServices, process_id: str) -> ProcessHandle | None:
    """This session's handle for one process; `None` when the host does not supervise that id."""
    for handle in services.list_processes():
        if handle.process_id == process_id:
            return handle
    return None


def _scan_stream(services: ProcessServices, process_id: str, stream: _StreamName) -> _StreamScan:
    """Every line of one stream's log, read out of the host's bounded byte windows.

    Lines retain endings with CRLF normalized to LF. The scan stops at a byte
    ceiling: a runaway log costs a bounded read, and the caller is told `truncated` rather than
    handed a silently short page. `bytes_scanned` is the honest byte cursor for the next read.
    """
    chunks: list[bytes] = []
    offset = 0
    while True:
        remaining = _LOG_SCAN_BYTES - offset
        if remaining <= 0:
            text = b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n")
            return _StreamScan(lines=text.splitlines(keepends=True), truncated=True, bytes_scanned=offset)
        page = services.read_process_output(process_id, stream=stream, offset=offset, limit=min(_LOG_PAGE_BYTES, remaining))
        offset += len(page.data)
        chunks.append(page.data)
        if page.eof or not page.data:
            text = b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n")
            return _StreamScan(lines=text.splitlines(keepends=True), truncated=not page.eof, bytes_scanned=offset)


def _wait_for_ready(services: ProcessServices, process_id: str, *, pattern: str | None, timeout: float, context: ToolContext) -> bool | None:
    """Whether the process printed `pattern`; `None` when not requested or waiting was cancelled.

    Both streams are polled from the byte the last poll consumed, keeping the legacy tail window,
    and a process that exits first ends the wait without waiting out the whole timeout.
    """
    if pattern is None:
        return None
    deadline = time.monotonic() + timeout
    offsets = {"stdout": 0, "stderr": 0}
    scanned = {"stdout": b"", "stderr": b""}
    while True:
        running = False
        drained = True
        for stream in ("stdout", "stderr"):
            if context.abort_signal is not None and context.abort_signal.cancelled:
                return None
            page = services.read_process_output(process_id, stream=stream, offset=offsets[stream], limit=_LOG_PAGE_BYTES)
            offsets[stream] += len(page.data)
            running = running or page.running
            drained = drained and page.eof
            if not page.data:
                continue
            window = scanned[stream] + page.data
            scanned[stream] = window[-_READY_SCAN_BYTES:]
            text = window.decode("utf-8", errors="replace").replace("\r\n", "\n")
            if any(pattern in line for line in text.splitlines()):
                return True
        if (not running and drained) or time.monotonic() >= deadline:
            return False
        time.sleep(_READY_POLL_SECONDS)


def _start_guidance(process_id: str) -> str:
    resource_uri = f"proc://{process_id}"
    return (
        f"Read {resource_uri} for status and stdout, or {resource_uri}/stderr for stderr; "
        "use background_process with op=logs, send, or stop for line paging and process control."
    )


def _logs_guidance(*, running: bool, next_offsets: Mapping[str, int], capped: Sequence[tuple[str, int]], process_id: str) -> str:
    parts = ["This is a bounded page from the current log snapshot." if running else "This is a bounded page from the final log snapshot."]
    if next_offsets:
        parts.append("Continue with the next offset(s) shown above.")
    for stream, bytes_scanned in capped:
        suffix = "/stderr" if stream == "stderr" else ""
        parts.append(
            f"Only the first {bytes_scanned} bytes of {stream} were scanned; "
            f"continue with read(proc://{process_id}{suffix}, byte_offset={bytes_scanned})."
        )
    return " ".join(parts)


def _clamp_log_line(line: str) -> tuple[str, bool]:
    """One model-visible line: capped in characters, ending preserved, clipping disclosed."""
    ending = "\n" if line.endswith("\n") else ""
    body = line[:-1] if ending else line
    if len(body) <= _LOG_LINE_CHARS:
        return line, False
    return f"{body[:_LOG_LINE_CHARS]}{_LOG_LINE_SUFFIX}{ending}", True


def _ps_line(row: BackgroundProcessRow) -> str:
    line = f"- {row.process_id} (proc://{row.process_id}): {row.state}"
    if row.exit_code is not None:
        line += f", exit code {row.exit_code}"
    line += f"; description={row.description!r}; command={row.command!r}"
    return line


def _is_aborted(context: ToolContext) -> bool:
    return context.abort_signal is not None and context.abort_signal.cancelled


def _write_message(written: int, *, requested: int, process_id: str) -> str:
    return f"Wrote {written} of {requested} UTF-8 bytes to background process {process_id}."


def _external_failure(*, tool_name: str, process_id: str, record: _ProcessRecord) -> ToolFailure[BackgroundProcessExternalBody]:
    message = _EXTERNAL_MESSAGE.format(process_id=process_id)
    return ToolFailure(
        tool_name=tool_name,
        error=message,
        output=TextOutput(message),
        body=BackgroundProcessExternalBody(
            process_id=process_id,
            pid=record.pid,
            start_time=record.start_time,
            command=record.command,
            description=record.description,
            stdout_path=record.stdout_path,
            stderr_path=record.stderr_path,
            reason=message,
        ),
        diagnostics=ToolDiagnostics(
            kind="tool_execution_error",
            summary=message,
            details={"reason": "process_not_supervised", "process_id": process_id},
            guidance="This identity survived a host restart; its logs and control are unavailable. Start a new process if needed.",
        ),
    )


@final
class BackgroundProcessTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="background_process",
        icon=Icon(glyph="◷"),
        description=(
            "Manage a long-running background process. Use op=start to launch, ps to list this session's processes, "
            "logs to read bounded output, send to write stdin, or stop to terminate a process. "
            "Process rows are scoped to the current owner, workspace, and session."
        ),
        input_schema={"type": "object", **_ARGS_ADAPTER.json_schema()},
        effects=frozenset({ToolEffect.EXECUTE}),
    )

    @expected_failures
    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        workspace = context.require_workspace().resolve()
        context.require_session_id()
        if not workspace.is_dir():
            raise_tool_diagnostic(
                message=f"background_process workspace is not an existing directory: {workspace}",
                error_kind="tool_environment_error",
                reason="invalid_workspace",
                retry_guidance="Retry from an existing workspace directory.",
            )
        args = _ARGS_ADAPTER.validate_python(dict(call.arguments))
        if _is_aborted(context):
            message = f"background_process {args.op} aborted before execution"
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(message),
                execution=ConfirmedStop(cancellation_signalled=True),
            )
        if isinstance(args, _StartArgs):
            return self._start(
                args,
                workspace=workspace,
                services=services,
                context=context,
            )
        if isinstance(args, _LogsArgs):
            return self._logs(args, services=services, context=context)
        if isinstance(args, _SendArgs):
            return self._send(args, services=services, context=context)
        if isinstance(args, _StopArgs):
            return self._stop(args, services=services, context=context)
        return self._ps(services=services, context=context)

    def _start(self, args: _StartArgs, *, workspace: Path, services: PluginServices, context: ToolContext) -> ToolResult:
        records, version = _read_records(services)
        process_id = f"proc-{uuid4().hex}"
        # The host refuses a duplicate id outright, so a start can never shadow a live process.
        try:
            handle = services.spawn_process(
                _shell_argv(args.command),
                cwd=workspace,
                stdin=True,
                process_id=process_id,
            )
        except ValueError as exc:
            duplicate = "already running" in str(exc)
            raise_tool_diagnostic(
                message=f"background_process could not start the process: {exc}",
                error_kind="tool_execution_error",
                reason="duplicate_spawn" if duplicate else "failed_process_start",
                retry_guidance=("Retry with a fresh process id." if duplicate else "Check the command and workspace permissions, then retry."),
                details={"error": str(exc)},
            )
        if _is_aborted(context):
            stopped, current, version = self._stop_started(
                process_id=handle.process_id,
                handle=handle,
                records=records,
                version=version,
                services=services,
            )
            return self._start_cancelled(
                args=args,
                workspace=workspace,
                handle=handle,
                current=current,
                stopped=stopped,
                record=None,
            )
        # The identity is persisted before the wait: a start that cannot report back still left
        # the caller a handle on the process it started.
        record = _identity_record(
            handle=handle,
            command=args.command,
            description=args.description or "",
            origin_session_id=context.session_id,
        )
        records[handle.process_id] = record
        version = _write_records(services, records, version)
        if _is_aborted(context):
            stopped, current, version = self._stop_started(
                process_id=handle.process_id,
                handle=handle,
                records=records,
                version=version,
                services=services,
            )
            return self._start_cancelled(
                args=args,
                workspace=workspace,
                handle=handle,
                current=current,
                stopped=stopped,
                record=records.get(handle.process_id),
            )
        ready = _wait_for_ready(services, handle.process_id, pattern=args.ready_pattern, timeout=float(args.ready_timeout), context=context)
        if _is_aborted(context):
            stopped, current, version = self._stop_started(
                process_id=handle.process_id,
                handle=handle,
                records=records,
                version=version,
                services=services,
            )
            return self._start_cancelled(
                args=args,
                workspace=workspace,
                handle=handle,
                current=current,
                stopped=stopped,
                record=records.get(handle.process_id),
            )
        current = _live_handle(services, handle.process_id) or handle
        running = current.running
        state = "running" if running else "exited"
        if not running:
            # The mirror follows the process it names; nothing else refreshes this attempt's exit.
            record = _with_exit(record, exit_code=current.exit_code)
            records[handle.process_id] = record
            version = _write_records(services, records, version)
        resource_uri = f"proc://{handle.process_id}"
        guidance = _start_guidance(handle.process_id)
        readiness = ""
        if args.ready_pattern is not None and ready is not True:
            if ready is None:
                readiness = " Readiness wait was cancelled; the process was started. Use ps to check its current state."
            elif running:
                readiness = f" Readiness pattern {args.ready_pattern!r} was not observed within {args.ready_timeout}s; process is still running."
            else:
                readiness = f" Process exited with code {current.exit_code} before readiness pattern {args.ready_pattern!r} appeared."
        content = f"Started background process {handle.process_id} (pid={handle.pid}) for command: {args.command}.{readiness} Guidance: {guidance}"
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(content),
            body=BackgroundProcessStartBody(
                process_id=handle.process_id,
                resource_uri=resource_uri,
                pid=handle.pid,
                state=state,
                command=args.command,
                cwd=str(workspace),
                description=args.description or "",
                stdout_path=handle.stdout_path,
                stderr_path=handle.stderr_path,
                started_at=record.started_at,
                running=running,
                ready=ready,
                ready_pattern=args.ready_pattern,
                exit_code=current.exit_code,
                guidance=guidance,
            ),
        )

    def _stop_started(
        self,
        *,
        process_id: str,
        handle: ProcessHandle,
        records: dict[str, _ProcessRecord],
        version: int,
        services: PluginServices,
    ) -> tuple[bool, ProcessHandle, int]:
        """Stop a just-started process after cancellation; the engine may discard the result."""
        try:
            stopped = services.cancel_process(process_id, reason="background_process start cancelled")
        except ValueError, PermissionError:
            stopped = False
        current = _live_handle(services, process_id) or handle
        record = records.get(process_id)
        if record is not None and stopped:
            records[process_id] = _with_exit(record, exit_code=current.exit_code)
            version = _write_records(services, records, version)
        return stopped, current, version

    def _start_cancelled(
        self,
        *,
        args: _StartArgs,
        workspace: Path,
        handle: ProcessHandle,
        current: ProcessHandle,
        stopped: bool,
        record: _ProcessRecord | None,
    ) -> ToolResult:
        guidance = _start_guidance(handle.process_id)
        if stopped:
            message = (
                f"background_process start was cancelled after starting {handle.process_id} (pid={handle.pid}); "
                "the process and its group were stopped."
            )
            execution: ConfirmedStop | UnconfirmedStop = ConfirmedStop(cancellation_signalled=True)
            reason = "start_cancelled"
        elif not current.running:
            message = (
                f"background_process start was cancelled after starting {handle.process_id} (pid={handle.pid}); "
                "the process leader exited, but the process group could not be confirmed stopped."
            )
            execution = UnconfirmedStop(cancellation_signalled=True)
            reason = "start_cancelled_leader_exited"
        else:
            message = (
                f"background_process start was cancelled after starting {handle.process_id} (pid={handle.pid}); "
                "could not confirm it stopped, so its process group may remain."
            )
            execution = UnconfirmedStop(cancellation_signalled=True)
            reason = "start_cancelled_unconfirmed"
        body = None
        if record is not None:
            running = current.running and not stopped
            body = BackgroundProcessStartBody(
                process_id=handle.process_id,
                resource_uri=f"proc://{handle.process_id}",
                pid=handle.pid,
                state="running" if running else "exited",
                command=args.command,
                cwd=str(workspace),
                description=args.description or "",
                stdout_path=handle.stdout_path,
                stderr_path=handle.stderr_path,
                started_at=record.started_at,
                running=running,
                ready=None,
                ready_pattern=args.ready_pattern,
                exit_code=current.exit_code,
                guidance=guidance,
            )
        return ToolFailure(
            tool_name=self.definition.name,
            error=message,
            output=TextOutput(f"{message} Guidance: {guidance}"),
            body=body,
            diagnostics=ToolDiagnostics(
                kind="tool_cancelled",
                summary=message,
                details={"reason": reason, "process_id": handle.process_id},
                guidance="Use ps to confirm the process state before starting another.",
            ),
            execution=execution,
        )

    def _ps(self, *, services: PluginServices, context: ToolContext) -> ToolResult:
        records, _ = _read_records(services)
        visible_records = {process_id: record for process_id, record in records.items() if record.origin_session_id == context.session_id}
        rows: list[BackgroundProcessRow] = []
        live: set[str] = set()
        for handle in services.list_processes():
            live.add(handle.process_id)
            record = visible_records.get(handle.process_id)
            rows.append(
                BackgroundProcessRow(
                    process_id=handle.process_id,
                    pid=handle.pid,
                    state="running" if handle.running else "exited",
                    description=record.description if record is not None else "",
                    command=record.command if record is not None else "",
                    stdout_path=handle.stdout_path,
                    stderr_path=handle.stderr_path,
                    started_at=record.started_at if record is not None else None,
                    exit_code=handle.exit_code,
                )
            )
        for process_id, record in visible_records.items():
            if process_id in live:
                continue
            rows.append(
                BackgroundProcessRow(
                    process_id=process_id,
                    pid=record.pid,
                    state="externally managed",
                    description=record.description,
                    command=record.command,
                    stdout_path=record.stdout_path,
                    stderr_path=record.stderr_path,
                    started_at=record.started_at,
                    exit_code=record.exit_code,
                )
            )
        rows.sort(key=lambda row: row.process_id)
        listed = tuple(rows[:MAX_PROCESS_ROWS])
        row_limit_omitted = len(rows) - len(listed)
        clipped_lines: list[tuple[str, bool]] = []
        for row in listed:
            line, clipped = _clamp_log_line(_ps_line(row))
            clipped_lines.append((line, clipped))
        shown = len(clipped_lines)
        while True:
            budget_omitted = len(clipped_lines) - shown
            if budget_omitted:
                summary = f"Background process list: {shown} of {len(clipped_lines)} process(es)."
            else:
                summary = f"Background process list: {len(clipped_lines)} process(es)."
            blocks = [summary]
            if row_limit_omitted:
                blocks.append(f"{row_limit_omitted} additional process(es) omitted by the {MAX_PROCESS_ROWS}-row limit.")
            if budget_omitted:
                blocks.append(f"{budget_omitted} process(es) omitted from the model text by the render budget; the typed body retains them.")
            blocks.extend(line for line, _clipped in clipped_lines[:shown])
            output = "\n".join(blocks)
            if shown == 0 or len(output.encode("utf-8")) <= _LOG_RENDER_BYTES:
                break
            shown -= 1
        clipped = any(line_clipped for _line, line_clipped in clipped_lines[:shown])
        truncated = bool(row_limit_omitted) or bool(budget_omitted) or clipped
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(output, bounds=OutputBounds(truncated=truncated, partial=truncated)),
            body=BackgroundProcessListBody(processes=listed, count=len(listed), limit=MAX_PROCESS_ROWS),
        )

    def _logs(self, args: _LogsArgs, *, services: PluginServices, context: ToolContext) -> ToolResult:
        records, _ = _read_records(services)
        handle = _live_handle(services, args.process_id)
        if handle is None:
            return self._unsupervised(records=records, process_id=args.process_id, session_id=context.session_id)
        selected: tuple[_StreamName, ...] = ("stdout", "stderr") if args.stream == "both" else (args.stream,)
        pages: dict[str, list[str]] = {"stdout": [], "stderr": []}
        next_offsets: dict[str, int] = {}
        capped: list[tuple[str, int]] = []
        clipped = False
        remaining = _LOG_RENDER_BYTES
        for stream in selected:
            scan = _scan_stream(services, args.process_id, stream)
            if scan.truncated:
                capped.append((stream, scan.bytes_scanned))
            consumed = 0
            for line in scan.lines[args.offset : args.offset + args.limit]:
                rendered, line_clipped = _clamp_log_line(line)
                size = len(rendered.encode("utf-8"))
                if size > remaining:
                    break
                pages[stream].append(rendered)
                remaining -= size
                consumed += 1
                clipped = clipped or line_clipped
            if args.offset + consumed < len(scan.lines):
                next_offsets[stream] = args.offset + consumed
        current = _live_handle(services, args.process_id) or handle
        running = current.running
        guidance = _logs_guidance(
            running=running,
            next_offsets=next_offsets,
            capped=capped,
            process_id=args.process_id,
        )
        if args.stream == "both":
            cursor = (
                "Next offsets: " + ", ".join(f"{stream}={offset}" for stream, offset in sorted(next_offsets.items())) + "." if next_offsets else ""
            )
            next_offset = None
        else:
            selected_offset = next_offsets.get(args.stream)
            cursor = f"Next offset: {selected_offset}." if selected_offset is not None else ""
            next_offset = selected_offset
        sections = [f"[{name}]\n{''.join(pages[name])}" for name in selected if pages[name]]
        sections.extend(block for block in (cursor, f"Guidance: {guidance}") if block)
        output = "\n\n".join(sections)
        truncated = clipped or bool(next_offsets) or bool(capped)
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(output, bounds=OutputBounds(truncated=truncated, partial=truncated)),
            body=BackgroundProcessLogsBody(
                process_id=args.process_id,
                state="running" if running else "exited",
                stream=args.stream,
                offset=args.offset,
                limit=args.limit,
                stdout="".join(pages["stdout"]),
                stderr="".join(pages["stderr"]),
                stdout_lines=len(pages["stdout"]),
                stderr_lines=len(pages["stderr"]),
                running=running,
                exit_code=current.exit_code,
                truncated=truncated,
                next_offset=next_offset,
                next_offsets=next_offsets,
                guidance=guidance,
            ),
        )

    def _send(self, args: _SendArgs, *, services: PluginServices, context: ToolContext) -> ToolResult:
        payload = args.input + ("\n" if args.newline else "")
        requested = len(payload.encode("utf-8"))
        if requested > MAX_SEND_BYTES:
            raise_tool_diagnostic(
                message=f"background_process input is {requested} UTF-8 bytes; the limit is {MAX_SEND_BYTES}.",
                error_kind="tool_input_validation",
                reason="input_too_large",
                retry_guidance=f"Send at most {MAX_SEND_BYTES} bytes per call, or split the input. The schema limits input to that many characters.",
                details={"requested_bytes": requested, "max_bytes": MAX_SEND_BYTES},
            )
        records, _ = _read_records(services)
        handle = _live_handle(services, args.process_id)
        if handle is None:
            return self._unsupervised(records=records, process_id=args.process_id, session_id=context.session_id)
        if _is_aborted(context):
            message = f"background_process send was cancelled before writing to {args.process_id}"
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(_write_message(0, requested=requested, process_id=args.process_id)),
                body=BackgroundProcessSendBody(
                    process_id=args.process_id,
                    input=args.input,
                    newline=args.newline,
                    requested_bytes=requested,
                    written_bytes=0,
                    error=message,
                ),
                diagnostics=ToolDiagnostics(
                    kind="tool_cancelled",
                    summary=message,
                    details={"reason": "send_cancelled", "process_id": args.process_id, "written_bytes": 0},
                    guidance="No input was written; retry if input is still required.",
                ),
                execution=ConfirmedStop(cancellation_signalled=True),
            )
        try:
            written = services.write_process(args.process_id, payload)
        except ValueError as exc:
            raise_tool_diagnostic(
                message=f"background process {args.process_id} is not accepting input: {exc}",
                error_kind="tool_execution_error",
                reason="stdin_unavailable",
                retry_guidance="The process was started without stdin or has exited; start a new process if input is required.",
                details={"process_id": args.process_id, "requested_bytes": requested, "error": str(exc)},
            )
        output = TextOutput(_write_message(written, requested=requested, process_id=args.process_id))
        if _is_aborted(context):
            message = f"background_process send was cancelled after writing {written} of {requested} bytes to {args.process_id}"
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=output,
                body=BackgroundProcessSendBody(
                    process_id=args.process_id,
                    input=args.input,
                    newline=args.newline,
                    requested_bytes=requested,
                    written_bytes=written,
                    error=message,
                ),
                diagnostics=ToolDiagnostics(
                    kind="tool_cancelled",
                    summary=message,
                    details={
                        "reason": "send_cancelled",
                        "process_id": args.process_id,
                        "requested_bytes": requested,
                        "written_bytes": written,
                    },
                    guidance="Only the reported bytes reached the process; retry the remaining input if it is still required.",
                ),
                execution=ConfirmedStop(cancellation_signalled=True),
            )
        if written < requested:
            message = f"{_PARTIAL_WRITE_MESSAGE}: wrote {written} of {requested} UTF-8 bytes to background process {args.process_id}"
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=output,
                body=BackgroundProcessSendBody(
                    process_id=args.process_id,
                    input=args.input,
                    newline=args.newline,
                    requested_bytes=requested,
                    written_bytes=written,
                    error=message,
                ),
                diagnostics=ToolDiagnostics(
                    kind="tool_execution_error",
                    summary=message,
                    details={
                        "reason": "partial_stdin_write",
                        "process_id": args.process_id,
                        "requested_bytes": requested,
                        "written_bytes": written,
                    },
                    guidance="Only the reported bytes reached the process; retry the remaining input or check that it is reading stdin.",
                ),
                execution=ConfirmedStop(cancellation_signalled=False),
            )
        body = BackgroundProcessSendBody(
            process_id=args.process_id,
            input=args.input,
            newline=args.newline,
            requested_bytes=requested,
            written_bytes=written,
            error=None,
        )
        return ToolSuccess(tool_name=self.definition.name, output=output, body=body)

    def _stop(self, args: _StopArgs, *, services: PluginServices, context: ToolContext) -> ToolResult:
        records, version = _read_records(services)
        handle = _live_handle(services, args.process_id)
        if handle is None:
            return self._unsupervised(records=records, process_id=args.process_id, session_id=context.session_id)
        if not services.cancel_process(args.process_id, reason="background_process stop"):
            message = _STOP_MESSAGE.format(process_id=args.process_id)
            raise_tool_diagnostic(
                message=message,
                error_kind="tool_execution_error",
                reason="stop_unconfirmed",
                retry_guidance="Check ps; if the process is still running, retry stop or inspect its process group.",
                details={"process_id": args.process_id},
            )
        current = _live_handle(services, args.process_id) or handle
        exit_code = current.exit_code
        record = records.get(args.process_id)
        if record is not None:
            records[args.process_id] = _with_exit(record, exit_code=exit_code)
            _write_records(services, records, version)
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(f"Stopped background process {args.process_id}."),
            body=BackgroundProcessStopBody(
                process_id=args.process_id,
                state="exited",
                exit_code=exit_code,
                running=False,
            ),
        )

    def _unsupervised(self, *, records: Mapping[str, _ProcessRecord], process_id: str, session_id: str) -> ToolResult:
        """Refuse an id this host does not supervise, naming an unknown or foreign one as unknown."""
        record = records.get(process_id)
        if record is None or record.origin_session_id != session_id:
            message = _UNKNOWN_MESSAGE.format(process_id=process_id)
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(message),
                diagnostics=ToolDiagnostics(
                    kind="tool_input_mismatch",
                    summary=message,
                    details={"reason": "unknown_process_id", "process_id": process_id},
                    guidance="Use ps to list the processes visible to this session.",
                ),
            )
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
