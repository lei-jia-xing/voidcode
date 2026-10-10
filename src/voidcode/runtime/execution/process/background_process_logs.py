from __future__ import annotations

import io
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Literal, cast

from pydantic import BaseModel, Field

from ....core.tool_context import ToolContext
from ....tools._pydantic_args import NonEmptyProcessId, parse_tool_args
from ....tools.contracts import OutputBounds, OutputReference, TextOutput, ToolCall, ToolFailure, ToolResult, ToolSuccess
from ....tools.output import create_tool_output_artifact
from .background_process_results import BackgroundProcessLogsBody, BackgroundProcessStaleBody

if TYPE_CHECKING:
    from .background_process import BackgroundProcessRuntime

_MAX_LOG_PAGE_BYTES = 40 * 1024
_LOG_READ_CHUNK_BYTES = 8192


class _BackgroundProcessLogsArgs(BaseModel):
    process_id: NonEmptyProcessId
    stream: Literal["stdout", "stderr", "both"] = "both"
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=500, ge=1, le=500)


class _SnapshotReader(io.RawIOBase):
    def __init__(self, stream: BinaryIO, byte_count: int) -> None:
        self._stream = stream
        self._remaining = byte_count

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        size = self._remaining if size < 0 else min(size, self._remaining)
        chunk = self._stream.read(size)
        self._remaining -= len(chunk)
        return chunk

    def readinto(self, buffer: Any) -> int:
        chunk = self.read(len(buffer))
        buffer[: len(chunk)] = chunk
        return len(chunk)


def _open_log_snapshot(path: Path) -> tuple[BinaryIO, int]:
    if path.is_symlink():
        raise PermissionError(f"background process log must not be a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    stream = os.fdopen(descriptor, "rb")
    metadata = os.fstat(stream.fileno())
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        stream.close()
        raise PermissionError(f"background process log is not an owned regular file: {path}")
    return stream, metadata.st_size


# ponytail: offset pagination scans earlier lines; add an index only if large logs make this a measured bottleneck.
def _read_snapshot_line(stream: BinaryIO, *, byte_count: int, max_bytes: int) -> tuple[bytes, bool, bool]:
    start = stream.tell()
    line = bytearray()
    clipped = False
    while stream.tell() < byte_count:
        chunk = stream.read(min(_LOG_READ_CHUNK_BYTES, byte_count - stream.tell()))
        if not chunk:
            break
        newline = chunk.find(b"\n")
        segment = chunk if newline < 0 else chunk[: newline + 1]
        room = max_bytes - len(line)
        if room > 0:
            line.extend(segment[:room])
        clipped = clipped or len(segment) > room
        if newline >= 0:
            stream.seek(stream.tell() - (len(chunk) - newline - 1))
            return bytes(line), True, clipped
    return bytes(line), stream.tell() > start, clipped


def _read_log_page(stream: BinaryIO, *, byte_count: int, offset: int, limit: int) -> tuple[str, int, bool, bool]:
    stream.seek(0)
    for _ in range(offset):
        if stream.tell() >= byte_count:
            return "", 0, False, False
        _, found, _ = _read_snapshot_line(stream, byte_count=byte_count, max_bytes=0)
        if not found:
            return "", 0, False, False

    page = bytearray()
    line_count = 0
    clipped_line = False
    while line_count < limit and len(page) < _MAX_LOG_PAGE_BYTES and stream.tell() < byte_count:
        line, found, clipped = _read_snapshot_line(
            stream,
            byte_count=byte_count,
            max_bytes=_MAX_LOG_PAGE_BYTES - len(page),
        )
        if not found:
            break
        page.extend(line)
        line_count += 1
        clipped_line = clipped_line or clipped
    return (
        page.decode("utf-8", errors="replace").replace("\r\n", "\n"),
        line_count,
        stream.tell() < byte_count,
        clipped_line,
    )


def _background_process_logs_guidance(*, running: bool, clipped_streams: tuple[str, ...]) -> str:
    if running:
        guidance = "This is a bounded page from the current log snapshot; use offset to inspect another page or continue other work."
    else:
        guidance = "This is a bounded page from the final log snapshot; use offset to inspect another page if needed."
    if clipped_streams:
        guidance += f" A long line was clipped in {', '.join(clipped_streams)}; consult the full snapshot artifact for its remaining bytes."
    return guidance


class BackgroundProcessLogsTool:
    name = "background_process_logs"

    def __init__(self, *, runtime: BackgroundProcessRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        caller_session_id = context.require_session_id()
        args = parse_tool_args(_BackgroundProcessLogsArgs, call.arguments, tool_name=self.name)

        state = self._runtime.background_process_manager.load(
            args.process_id,
            workspace=workspace,
            owner_session_id=caller_session_id,
            enforce_owner=True,
        )
        if state is None:
            raise ValueError(f"unknown background process: {args.process_id}")
        if state.prior_runtime:
            message = state.reconciliation_reason or (
                "Prior-runtime managed process was observed after restart; it was not reattached "
                "and is externally managed/unavailable to this runtime."
            )
            return ToolFailure(
                tool_name=self.name,
                error=message,
                output=TextOutput(message),
                body=BackgroundProcessStaleBody(
                    process_id=state.process_id,
                    pid=state.process.pid,
                    status="stale",
                    prior_runtime=True,
                    reconciliation_reason=message,
                    running=None,
                    observed_running=state.observed_running,
                    identity_match=state.identity_match,
                    controllable=False,
                ),
            )
        paths = {"stdout": state.stdout_path, "stderr": state.stderr_path}
        artifacts: dict[str, dict[str, object]] = {}
        selected_streams = ("stdout", "stderr") if args.stream == "both" else (args.stream,)
        pages = {"stdout": "", "stderr": ""}
        line_counts = {"stdout": 0, "stderr": 0}
        has_more = {"stdout": False, "stderr": False}
        clipped_lines = {"stdout": False, "stderr": False}
        for stream_name in selected_streams:
            path = paths[stream_name]
            if path is None or not path.exists():
                continue
            snapshot, snapshot_bytes = _open_log_snapshot(path)
            with snapshot:
                artifacts[stream_name] = create_tool_output_artifact(
                    session_id=caller_session_id,
                    tool_call_id=state.process_id,
                    tool_name=self.name,
                    stream=cast(BinaryIO, _SnapshotReader(snapshot, snapshot_bytes)),
                    kind=stream_name,
                    mime="text/plain; charset=utf-8",
                    source=f"background_process:{state.process_id}:pid={state.process.pid}:{stream_name}:snapshot_bytes={snapshot_bytes}",
                )
                (
                    pages[stream_name],
                    line_counts[stream_name],
                    has_more[stream_name],
                    clipped_lines[stream_name],
                ) = _read_log_page(
                    snapshot,
                    byte_count=snapshot_bytes,
                    offset=args.offset,
                    limit=args.limit,
                )
        stdout = pages["stdout"]
        stderr = pages["stderr"]
        sections = [f"[{name}]\n{pages[name]}" for name in selected_streams if pages[name]]
        output = "\n".join(sections)
        references = [str(artifact["uri"]) for artifact in artifacts.values() if isinstance(artifact.get("uri"), str)]
        if references:
            output = f"{output}\n\nFull snapshot: {', '.join(references)}" if output else f"Full snapshot: {', '.join(references)}"
        running = state.process.poll() is None
        exit_code = state.process.poll()
        clipped_streams = tuple(name for name in selected_streams if clipped_lines[name])
        guidance = _background_process_logs_guidance(running=running, clipped_streams=clipped_streams)
        output = f"{output}\n\nGuidance: {guidance}" if output else f"Guidance: {guidance}"
        truncated = args.offset > 0 or any(has_more.values()) or bool(clipped_streams)
        next_offsets: dict[str, int] = {name: args.offset + line_counts[name] for name in selected_streams if has_more[name]}
        next_offset = next_offsets.get(args.stream) if args.stream != "both" else None
        first_artifact = next(iter(artifacts.values()), None)
        first_reference = (
            OutputReference(str(first_artifact["uri"]), first_artifact)
            if first_artifact is not None and isinstance(first_artifact.get("uri"), str)
            else None
        )
        return ToolSuccess(
            tool_name=self.name,
            output=TextOutput(
                output,
                bounds=OutputBounds(
                    truncated=truncated,
                    partial=truncated,
                    reference=first_reference,
                ),
            ),
            body=BackgroundProcessLogsBody(
                process_id=state.process_id,
                status=state.status,
                prior_runtime=state.prior_runtime,
                reconciliation_reason=state.reconciliation_reason,
                running=running,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                stdout_retained_lines=line_counts["stdout"],
                stderr_retained_lines=line_counts["stderr"],
                stdout_dropped_lines=state.stdout_dropped_lines,
                stderr_dropped_lines=state.stderr_dropped_lines,
                stdout_artifact=artifacts.get("stdout"),
                stderr_artifact=artifacts.get("stderr"),
                truncated=truncated,
                references=tuple(references),
                guidance=guidance,
                stream=args.stream,
                offset=args.offset,
                limit=args.limit,
                clipped_streams=clipped_streams,
                next_offsets=next_offsets,
                next_offset=next_offset,
            ),
        )
