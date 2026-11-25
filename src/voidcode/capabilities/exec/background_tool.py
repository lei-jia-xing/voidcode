"""The `background_process` tool over core-owned process supervision."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, final
from uuid import uuid4

from voidcode.capabilities.tool_errors import expected_failures, raise_tool_diagnostic
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices, ProcessAlreadyRunningError, ProcessHandle
from voidcode.sdk.tools import (
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

from . import background_limits as _limits
from .background_args import _ARGS_ADAPTER, _LogsArgs, _SendArgs, _StartArgs, _StopArgs
from .background_models import (
    BackgroundProcessListBody,
    BackgroundProcessLogsBody,
    BackgroundProcessRow,
    BackgroundProcessSendBody,
    BackgroundProcessStartBody,
    BackgroundProcessStopBody,
    ProcessLiveness,
    _StreamName,
)
from .background_render import (
    _PARTIAL_WRITE_MESSAGE,
    _STOP_MESSAGE,
    _UNKNOWN_MESSAGE,
    _clamp_log_line,
    _external_failure,
    _is_aborted,
    _logs_guidance,
    _ps_line,
    _start_guidance,
    _write_message,
)
from .background_scan import _scan_stream, _wait_for_ready
from .background_state import _identity_record, _live_handle, _ProcessRecord, _read_records, _with_exit, _write_records
from .shell_exec import shell_argv


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
                shell_argv(args.command),
                cwd=workspace,
                stdin=True,
                process_id=process_id,
            )
        except ValueError as exc:
            duplicate = isinstance(exc, ProcessAlreadyRunningError)
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
        state: ProcessLiveness = "running" if running else "exited"
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
        listed = tuple(rows[: _limits.MAX_PROCESS_ROWS])
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
                blocks.append(f"{row_limit_omitted} additional process(es) omitted by the {_limits.MAX_PROCESS_ROWS}-row limit.")
            if budget_omitted:
                blocks.append(f"{budget_omitted} process(es) omitted from the model text by the render budget; the typed body retains them.")
            blocks.extend(line for line, _clipped in clipped_lines[:shown])
            output = "\n".join(blocks)
            if shown == 0 or len(output.encode("utf-8")) <= _limits._LOG_RENDER_BYTES:
                break
            shown -= 1
        clipped = any(line_clipped for _line, line_clipped in clipped_lines[:shown])
        truncated = bool(row_limit_omitted) or bool(budget_omitted) or clipped
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(output, bounds=OutputBounds(truncated=truncated, partial=truncated)),
            body=BackgroundProcessListBody(processes=listed, count=len(listed), limit=_limits.MAX_PROCESS_ROWS),
        )

    def _logs(self, args: _LogsArgs, *, services: PluginServices, context: ToolContext) -> ToolResult:
        records, _ = _read_records(services)
        handle = _live_handle(services, args.process_id)
        if handle is None:
            return self._unsupervised(records=records, process_id=args.process_id, session_id=context.session_id)
        selected: tuple[_StreamName, ...] = ("stdout", "stderr") if args.stream == "both" else (args.stream,)
        pages: dict[_StreamName, list[str]] = {"stdout": [], "stderr": []}
        next_offsets: dict[_StreamName, int] = {}
        capped: list[tuple[_StreamName, int]] = []
        clipped = False
        remaining = _limits._LOG_RENDER_BYTES
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
        if requested > _limits.MAX_SEND_BYTES:
            raise_tool_diagnostic(
                message=f"background_process input is {requested} UTF-8 bytes; the limit is {_limits.MAX_SEND_BYTES}.",
                error_kind="tool_input_validation",
                reason="input_too_large",
                retry_guidance=(
                    f"Send at most {_limits.MAX_SEND_BYTES} bytes per call, or split the input. The schema limits input to that many characters."
                ),
                details={"requested_bytes": requested, "max_bytes": _limits.MAX_SEND_BYTES},
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
