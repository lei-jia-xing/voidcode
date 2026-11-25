"""Model-visible text and failure shapes for background processes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.tools import TextOutput, ToolDiagnostics, ToolFailure

from . import background_limits as _limits
from .background_models import BackgroundProcessExternalBody, BackgroundProcessRow, _StreamName
from .background_state import _ProcessRecord

_EXTERNAL_MESSAGE = (
    "background process {process_id} is externally managed: this host does not supervise it "
    "(it is not a retained job; it may have survived a host restart or been evicted after exit), so its logs, stdin, and "
    "process group are unavailable to this session"
)
_UNKNOWN_MESSAGE = "unknown background process: {process_id}"
_STOP_MESSAGE = "could not confirm background process {process_id} stopped; process group remains or termination is unconfirmed"
_PARTIAL_WRITE_MESSAGE = "process stdin accepted only part of the input"


def _start_guidance(process_id: str) -> str:
    resource_uri = f"proc://{process_id}"
    return (
        f"Read {resource_uri} for status and stdout, or {resource_uri}/stderr for stderr; "
        "use background_process with op=logs, send, or stop for line paging and process control."
    )


def _logs_guidance(*, running: bool, next_offsets: Mapping[_StreamName, int], capped: Sequence[tuple[_StreamName, int]], process_id: str) -> str:
    parts = ["This is a bounded page from the current log snapshot." if running else "This is a bounded page from the final log snapshot."]
    if next_offsets:
        parts.append("Continue with the next offset(s) shown above.")
    for stream, bytes_scanned in capped:
        suffix = "/stderr" if stream == "stderr" else ""
        parts.append(
            f"Only the first {bytes_scanned} bytes of {stream} were scanned; "
            f"continue from proc://{process_id}{suffix} at byte_offset={bytes_scanned}."
        )
    return " ".join(parts)


def _clamp_log_line(line: str) -> tuple[str, bool]:
    """One model-visible line: capped in characters, ending preserved, clipping disclosed."""
    ending = "\n" if line.endswith("\n") else ""
    body = line[:-1] if ending else line
    if len(body) <= _limits._LOG_LINE_CHARS:
        return line, False
    return f"{body[: _limits._LOG_LINE_CHARS]}{_limits._LOG_LINE_SUFFIX}{ending}", True


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
