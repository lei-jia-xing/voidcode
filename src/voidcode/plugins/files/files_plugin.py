"""Files plugin implementing workspace read and write tools."""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, tool_definition
from voidcode.sdk.tools import (
    ConfirmedStop,
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

MAX_LINES = 1000
MAX_LINE_LENGTH = 2000
MAX_TOTAL_OUTPUT_BYTES = 50 * 1024  # Total tool output text capped at 50 KiB
MAX_WRITE_BYTES = 64 * 1024  # Max single-file write 64 KiB to fit comfortably in 1 MiB wire frames


@dataclass(frozen=True, slots=True)
class ReadResultBody:
    path: str
    sha256: str
    whole_file: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "whole_file": self.whole_file,
        }


@dataclass(frozen=True, slots=True)
class WriteResultBody:
    path: str
    sha256: str
    bytes: int

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


# ponytail: UTF-8 files only in P1; richer formats, directories and archives migrate in P4
class ReadTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if not context.has_capability("workspace.read"):
            return ToolFailure(self.definition.name, error="Missing required capability: workspace.read")

        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                self.definition.name,
                error="Invocation cancelled before read",
                execution=ConfirmedStop(cancellation_signalled=True),
            )

        workspace = context.require_workspace().resolve()
        raw_path_arg = call.arguments.get("path")
        if not isinstance(raw_path_arg, str) or not raw_path_arg.strip():
            return ToolFailure(self.definition.name, error="path must be a non-empty string")

        raw_offset = call.arguments.get("offset", 1)
        raw_limit = call.arguments.get("limit", MAX_LINES)

        if not isinstance(raw_offset, int) or isinstance(raw_offset, bool) or raw_offset < 1:
            return ToolFailure(self.definition.name, error="offset must be a positive integer (>= 1)")

        if not isinstance(raw_limit, int) or isinstance(raw_limit, bool) or raw_limit < 1:
            return ToolFailure(self.definition.name, error="limit must be a positive integer (>= 1)")

        offset = raw_offset
        limit = min(raw_limit, MAX_LINES)

        p = Path(raw_path_arg)
        try:
            target = p.resolve() if p.is_absolute() else (workspace / p).resolve()
        except Exception as exc:
            return ToolFailure(self.definition.name, error=f"Invalid path: {exc}")

        if not target.is_relative_to(workspace):
            return ToolFailure(self.definition.name, error=f"Path escapes workspace: {raw_path_arg}")

        if not target.exists():
            return ToolFailure(self.definition.name, error=f"File not found: {raw_path_arg}")

        if target.is_dir():
            return ToolFailure(self.definition.name, error=f"Path is a directory: {raw_path_arg}")

        if not target.is_file():
            return ToolFailure(self.definition.name, error=f"Path is not a regular file: {raw_path_arg}")

        display_path = target.relative_to(workspace).as_posix()

        # Single-pass incremental decode + hash on the same opened file handle
        hasher = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")

        rendered_lines: list[str] = []
        total_lines = 0
        content_truncated = False
        has_more = False
        reached_eof = False

        line_chars: list[str] = []
        current_line_truncated = False

        try:
            with target.open("rb") as f:
                while True:
                    if context.abort_signal is not None and context.abort_signal.cancelled:
                        return ToolFailure(
                            self.definition.name,
                            error="Invocation cancelled during read",
                            execution=ConfirmedStop(cancellation_signalled=True),
                        )

                    chunk = f.read(16384)
                    is_eof = len(chunk) == 0
                    if not is_eof:
                        hasher.update(chunk)

                    text_piece = decoder.decode(chunk, final=is_eof)
                    if text_piece or is_eof:
                        pos = 0
                        while pos < len(text_piece):
                            nl_pos = text_piece.find("\n", pos)
                            if nl_pos != -1:
                                segment = text_piece[pos : nl_pos + 1]
                                pos = nl_pos + 1
                                is_line_end = True
                            else:
                                segment = text_piece[pos:]
                                pos = len(text_piece)
                                is_line_end = False

                            if not current_line_truncated:
                                current_len = sum(len(s) for s in line_chars)
                                remaining = MAX_LINE_LENGTH - current_len
                                if len(segment) <= remaining:
                                    line_chars.append(segment)
                                else:
                                    line_chars.append(segment[:remaining])
                                    suffix = (
                                        f"... (line truncated to {MAX_LINE_LENGTH} chars)\n"
                                        if is_line_end
                                        else f"... (line truncated to {MAX_LINE_LENGTH} chars)"
                                    )
                                    line_chars.append(suffix)
                                    current_line_truncated = True
                            else:
                                if is_line_end:
                                    if not line_chars or not line_chars[-1].endswith("\n"):
                                        line_chars.append("\n")

                            if is_line_end:
                                total_lines += 1
                                full_line = "".join(line_chars)
                                line_chars.clear()
                                was_truncated = current_line_truncated
                                current_line_truncated = False

                                if total_lines < offset:
                                    continue

                                if has_more or content_truncated:
                                    continue

                                if len(rendered_lines) >= limit:
                                    has_more = True
                                    continue

                                line_bytes = len(full_line.encode("utf-8"))
                                current_bytes = sum(len(line.encode("utf-8")) for line in rendered_lines)
                                header_overhead = len(
                                    (
                                        f"Read {len(rendered_lines) + 1} line(s) from {display_path}; output is truncated.\n"
                                        f"SHA-256 content hash: {'0' * 64}\n\n"
                                    ).encode()
                                )
                                if current_bytes + line_bytes + header_overhead > MAX_TOTAL_OUTPUT_BYTES:
                                    content_truncated = True
                                    continue

                                rendered_lines.append(full_line)
                                if was_truncated:
                                    content_truncated = True

                    if is_eof:
                        reached_eof = True
                        if line_chars:
                            total_lines += 1
                            full_line = "".join(line_chars)
                            was_truncated = current_line_truncated
                            if total_lines >= offset and not has_more and not content_truncated and len(rendered_lines) < limit:
                                line_bytes = len(full_line.encode("utf-8"))
                                current_bytes = sum(len(line.encode("utf-8")) for line in rendered_lines)
                                header_overhead = len(
                                    (
                                        f"Read {len(rendered_lines) + 1} line(s) from {display_path}; output is truncated.\n"
                                        f"SHA-256 content hash: {'0' * 64}\n\n"
                                    ).encode()
                                )
                                if current_bytes + line_bytes + header_overhead <= MAX_TOTAL_OUTPUT_BYTES:
                                    rendered_lines.append(full_line)
                                    if was_truncated:
                                        content_truncated = True
                                else:
                                    content_truncated = True
                            elif total_lines >= offset and len(rendered_lines) >= limit:
                                has_more = True
                        break

        except UnicodeDecodeError as exc:
            return ToolFailure(self.definition.name, error=f"Invalid UTF-8 or binary file: {exc}")
        except OSError as exc:
            return ToolFailure(self.definition.name, error=f"Failed to read file: {exc}")

        full_file_sha256 = hasher.hexdigest()

        if total_lines < offset and not (total_lines == 0 and offset == 1):
            return ToolFailure(self.definition.name, error=f"Offset {offset} is out of range for this file ({total_lines} lines)")

        truncated = content_truncated or has_more
        whole_file = (offset == 1) and not truncated and reached_eof and (total_lines == len(rendered_lines))

        raw_content = "".join(rendered_lines)
        status_note = "; output is truncated." if truncated else "."
        text = f"Read {len(rendered_lines)} line(s) from {display_path}{status_note}\nSHA-256 content hash: {full_file_sha256}\n\n{raw_content}"

        # Guarantee total text bytes within MAX_TOTAL_OUTPUT_BYTES
        while rendered_lines and len(text.encode("utf-8")) > MAX_TOTAL_OUTPUT_BYTES:
            rendered_lines.pop()
            truncated = True
            whole_file = False
            raw_content = "".join(rendered_lines)
            text = (
                f"Read {len(rendered_lines)} line(s) from {display_path}; output is truncated.\n"
                f"SHA-256 content hash: {full_file_sha256}\n\n{raw_content}"
            )

        body = ReadResultBody(
            path=display_path,
            sha256=full_file_sha256,
            whole_file=whole_file,
        )
        return ToolSuccess(
            self.definition.name,
            output=TextOutput(text, bounds=OutputBounds(truncated=truncated, partial=truncated)),
            body=body,
        )


# ponytail: atomic tempfile + replace per file; directory locks / transactional edits migrate in P3
class WriteTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if not context.has_capability("workspace.write"):
            return ToolFailure(self.definition.name, error="Missing required capability: workspace.write")

        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                self.definition.name,
                error="Invocation cancelled before side effect",
                execution=ConfirmedStop(cancellation_signalled=True),
            )

        workspace = context.require_workspace().resolve()
        raw_path = call.arguments.get("path")
        content = call.arguments.get("content")

        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolFailure(self.definition.name, error="path must be a non-empty string")

        if not isinstance(content, str):
            return ToolFailure(self.definition.name, error="content must be a string")

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_WRITE_BYTES:
            return ToolFailure(
                self.definition.name,
                error=f"Content exceeds size limit ({len(content_bytes)} bytes > {MAX_WRITE_BYTES} bytes)",
            )

        p = Path(raw_path)
        try:
            candidate = p.resolve() if p.is_absolute() else (workspace / p).resolve()
        except Exception as exc:
            return ToolFailure(self.definition.name, error=f"Invalid path: {exc}")

        if not candidate.is_relative_to(workspace):
            return ToolFailure(self.definition.name, error=f"Path escapes workspace: {raw_path}")

        if candidate.is_dir():
            return ToolFailure(self.definition.name, error=f"Cannot write to directory: {raw_path}")

        raw_target = p if p.is_absolute() else (workspace / p)
        if raw_target.is_symlink() or candidate.is_symlink():
            return ToolFailure(self.definition.name, error=f"Cannot write to symlink: {raw_path}")

        initial_stat: os.stat_result | None = None
        initial_hash: str | None = None
        target_mode = 0o644

        if candidate.exists():
            expected_hash = call.arguments.get("expectedHash")
            if not isinstance(expected_hash, str) or not expected_hash:
                return ToolFailure(
                    self.definition.name,
                    error="write requires an expectedHash argument when overwriting an existing file",
                )
            initial_stat = candidate.stat()
            target_mode = initial_stat.st_mode
            initial_hash = _hash_file(candidate)
            if expected_hash != initial_hash:
                return ToolFailure(
                    self.definition.name,
                    error=f"Stale write rejected: expected hash {expected_hash} does not match current hash {initial_hash}",
                )

        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                self.definition.name,
                error="Invocation cancelled before side effect",
                execution=ConfirmedStop(cancellation_signalled=True),
            )

        candidate.parent.mkdir(parents=True, exist_ok=True)
        if not candidate.parent.resolve().is_relative_to(workspace):
            return ToolFailure(self.definition.name, error=f"Parent directory escapes workspace: {raw_path}")

        temp_fd, temp_path_str = tempfile.mkstemp(
            prefix=".voidcode_write_",
            dir=candidate.parent,
        )
        temp_path = Path(temp_path_str)
        try:
            with os.fdopen(temp_fd, "wb") as f:
                f.write(content_bytes)
                f.flush()
                os.fsync(f.fileno())

            # Preserve file mode for existing files
            try:
                os.chmod(temp_path, target_mode)
            except OSError:
                pass

            if context.abort_signal is not None and context.abort_signal.cancelled:
                temp_path.unlink(missing_ok=True)
                return ToolFailure(
                    self.definition.name,
                    error="Invocation cancelled before side effect",
                    execution=ConfirmedStop(cancellation_signalled=True),
                )

            # Recheck path identity and content hash at replace boundary to reject concurrent modification
            if initial_stat is not None:
                if not candidate.exists():
                    temp_path.unlink(missing_ok=True)
                    return ToolFailure(self.definition.name, error=f"File deleted concurrently during write: {raw_path}")
                cur_stat = candidate.stat()
                if cur_stat.st_ino != initial_stat.st_ino or _hash_file(candidate) != initial_hash:
                    temp_path.unlink(missing_ok=True)
                    return ToolFailure(self.definition.name, error=f"File modified concurrently during write: {raw_path}")
            else:
                if candidate.exists():
                    temp_path.unlink(missing_ok=True)
                    return ToolFailure(self.definition.name, error=f"File created concurrently during write: {raw_path}")

            os.replace(temp_path, candidate)
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            return ToolFailure(self.definition.name, error=f"Write failed: {exc}")

        written_hash = hashlib.sha256(content_bytes).hexdigest()
        display_path = candidate.relative_to(workspace).as_posix()
        output_text = f"Wrote {len(content_bytes)} byte(s) to {display_path} (SHA-256: {written_hash})"

        body = WriteResultBody(
            path=display_path,
            sha256=written_hash,
            bytes=len(content_bytes),
        )
        return ToolSuccess(
            self.definition.name,
            output=TextOutput(output_text),
            body=body,
        )


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    manifest_path = Path(__file__).parent / "voidcode-plugin.json"
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = parse_plugin_manifest(raw_manifest)
    tool_defs = {decl.name: tool_definition(decl) for decl in manifest.tools}
    read_tool = ReadTool(definition=tool_defs["read"])
    write_tool = WriteTool(definition=tool_defs["write"])
    return Plugin(tools=(read_tool, write_tool), providers=(), close=None)
