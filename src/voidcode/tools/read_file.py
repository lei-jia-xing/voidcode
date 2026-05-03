"""Safe read-only file tool for the deterministic slice."""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast, final

from pydantic import ValidationError

from ..security.path_policy import resolve_workspace_path as resolve_workspace_path_policy
from ._pydantic_args import ReadFileArgs, format_validation_error
from ._workspace import suggest_workspace_paths
from .contracts import ToolCall, ToolDefinition, ToolResult

DEFAULT_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000
MAX_BYTES = 50 * 1024
MAX_ATTACHMENT_BYTES = 50 * 1024
BINARY_SNIFF_BYTES = 4096


@dataclass(frozen=True, slots=True)
class _ReadOutcome:
    content: str
    data: dict[str, object]


def _truncate_line(line: str) -> tuple[str, bool]:
    if len(line) <= MAX_LINE_LENGTH:
        return line, False
    return f"{line[:MAX_LINE_LENGTH]}... (line truncated to {MAX_LINE_LENGTH} chars)", True


def _is_binary_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in {
        ".zip",
        ".tar",
        ".gz",
        ".exe",
        ".dll",
        ".so",
        ".class",
        ".jar",
        ".war",
        ".7z",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".bin",
        ".dat",
        ".obj",
        ".o",
        ".a",
        ".lib",
        ".wasm",
        ".pyc",
        ".pyo",
    }:
        return True

    try:
        with path.open("rb") as fh:
            sample = fh.read(BINARY_SNIFF_BYTES)
    except OSError:
        return False

    if not sample:
        return False
    if b"\x00" in sample:
        return True

    non_printable = 0
    for byte in sample:
        if byte < 9 or (byte > 13 and byte < 32):
            non_printable += 1
    return non_printable / len(sample) > 0.3


def _render_file(candidate: Path, *, relative_path: str, offset: int, limit: int) -> _ReadOutcome:
    mime, _ = mimetypes.guess_type(candidate.name)
    if mime and (mime.startswith("image/") or mime == "application/pdf"):
        attachment_size = candidate.stat().st_size
        if attachment_size > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                "read_file attachment exceeds the maximum supported size "
                f"({MAX_ATTACHMENT_BYTES} bytes): {relative_path}"
            )
        raw = candidate.read_bytes()
        data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        label = "Image" if mime.startswith("image/") else "PDF"
        message = f"{label} read successfully"
        return _ReadOutcome(
            content=message,
            data={
                "path": relative_path,
                "type": "attachment",
                "content_type": mime,
                "byte_count": len(raw),
                "attachment": {"mime": mime, "data_uri": data_uri},
                "truncated": False,
                "partial": False,
            },
        )

    if _is_binary_file(candidate):
        raise ValueError(
            f"read_file only supports text files or image/pdf attachments: {relative_path}"
        )

    limit = min(limit, DEFAULT_READ_LIMIT)
    rendered_lines: list[str] = []
    total_lines = 0
    bytes_used = 0
    content_truncated = False
    has_more = False

    try:
        with candidate.open("r", encoding="utf-8", newline="") as fh:
            for line_number, raw_line in enumerate(fh, start=1):
                total_lines = line_number
                if line_number < offset:
                    continue
                if len(rendered_lines) >= limit:
                    has_more = True
                    continue

                line_text = raw_line.rstrip("\r\n")
                line_text, line_truncated = _truncate_line(line_text)
                encoded_size = len(line_text.encode("utf-8")) + (1 if rendered_lines else 0)
                if bytes_used + encoded_size > MAX_BYTES:
                    content_truncated = True
                    has_more = True
                    break

                rendered_lines.append(f"{line_number}: {line_text}")
                bytes_used += encoded_size
                content_truncated = content_truncated or line_truncated
    except UnicodeDecodeError as exc:
        raise ValueError("read_file only supports UTF-8 text files") from exc

    if total_lines < offset and not (total_lines == 0 and offset == 1):
        raise ValueError(f"Offset {offset} is out of range for this file ({total_lines} lines)")

    next_offset = offset + len(rendered_lines)
    content_truncated = content_truncated or has_more

    rendered: list[str] = [
        f"<path>{relative_path}</path>",
        "<type>file</type>",
        "<content>",
    ]
    rendered.extend(rendered_lines)
    if has_more:
        if bytes_used >= MAX_BYTES:
            rendered.append(
                "(Output capped at "
                f"{MAX_BYTES // 1024} KB. Showing lines {offset}-{next_offset - 1}. "
                f"Use offset={next_offset} to continue.)"
            )
        else:
            rendered.append(
                "(Showing lines "
                f"{offset}-{next_offset - 1} of {total_lines}. "
                f"Use offset={next_offset} to continue.)"
            )
    else:
        rendered.append(f"(End of file - total {total_lines} lines)")
    rendered.append("</content>")

    return _ReadOutcome(
        content="\n".join(rendered),
        data={
            "path": relative_path,
            "type": "file",
            "line_count": total_lines,
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset if has_more else None,
            "truncated": content_truncated,
            "partial": content_truncated,
            "byte_count": bytes_used,
            "copy_guidance": (
                "Displayed lines include '<line>: ' prefixes for navigation. "
                "When passing text to edit oldString, omit those prefixes and use only file text."
            ),
        },
    )


@final
class ReadFileTool:
    """Read a file or supported attachment from the current workspace."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="read_file",
        description="Read a file inside the current workspace.",
        input_schema={
            "filePath": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = ReadFileArgs.model_validate(
                {
                    "filePath": call.arguments.get("filePath", call.arguments.get("path")),
                    "offset": call.arguments.get("offset"),
                    "limit": call.arguments.get("limit"),
                }
            )
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        resolution = resolve_workspace_path_policy(
            workspace=workspace,
            raw_path=args.filePath,
            allow_outside_workspace=True,
        )
        candidate = resolution.candidate
        relative_path = (
            str(candidate.resolve()) if resolution.is_external else resolution.relative_path
        )

        if not candidate.exists():
            raise ValueError(f"read_file target does not exist: {args.filePath}")

        offset = args.offset or 1
        limit = args.limit or DEFAULT_READ_LIMIT
        if candidate.is_dir():
            suggestions = suggest_workspace_paths(workspace=workspace, raw_path=args.filePath)
            suffix = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise ValueError(f"read_file does not support directories: {args.filePath}.{suffix}")
        if not candidate.is_file():
            raise ValueError(f"read_file only supports regular files: {args.filePath}")

        outcome = _render_file(candidate, relative_path=relative_path, offset=offset, limit=limit)

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=outcome.content,
            data=outcome.data,
            truncated=bool(outcome.data.get("truncated", False)),
            partial=bool(outcome.data.get("partial", False)),
            attachment=cast(dict[str, object], outcome.data.get("attachment"))
            if isinstance(outcome.data.get("attachment"), dict)
            else None,
        )
