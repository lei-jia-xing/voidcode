"""Safe read-only file tool for the deterministic slice."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast, final

from pydantic import BaseModel, ValidationError, field_validator

from ..runtime.contracts import validate_session_id
from ..security.path_policy import resolve_workspace_path as resolve_workspace_path_policy
from ._pydantic_args import format_validation_error
from ._workspace import suggest_workspace_paths
from .contracts import ToolCall, ToolDefinition, ToolResult
from .guidance import guidance_for_tool
from .output import _ARTIFACT_ID_PATTERN
from .runtime_context import require_runtime_tool_context

#: Internal URL scheme for on-demand tool documentation (essential/discoverable
#: split): read(path="voidcode://tool/<name>") returns the tool's guidance
#: text plus its JSON input schema, read from the same guidance files and the
#: live tool registry used for execution.
VOIDCODE_TOOL_DOC_PREFIX = "voidcode://tool/"

#: Internal URL scheme for session-scoped artifact reads:
#: read(path="voidcode://artifact/<id>") returns a bounded slice of a
#: spilled tool-output artifact, resolved through the runtime's own
#: session-validated artifact reader — never through the external-directory
#: permission path.
VOIDCODE_ARTIFACT_PREFIX = "voidcode://artifact/"

#: Internal URL scheme for lineage-guarded transcript reads:
#: read(path="voidcode://transcript/<session_id>") returns a bounded,
#: payload-stripped transcript of the caller's own session or of a child
#: session the caller spawned, resolved through the runtime's session-validated
#: transcript reader.
VOIDCODE_TRANSCRIPT_PREFIX = "voidcode://transcript/"


def _render_tool_documentation(path: str) -> _ReadOutcome:
    tool_name = path[len(VOIDCODE_TOOL_DOC_PREFIX) :].strip()
    if not tool_name:
        raise ValueError("voidcode://tool/<name> requires a tool name")
    context = require_runtime_tool_context("read")
    catalog = context.tool_catalog
    if catalog is None:
        raise ValueError("read cannot resolve voidcode://tool URLs without a runtime tool catalog")
    definition = catalog.lookup(tool_name)
    if definition is None:
        raise ValueError(f"unknown tool in runtime registry: {tool_name}")
    guidance = guidance_for_tool(tool_name)
    sections = [
        f"# Tool: {definition.name}",
        "",
        ("Schema" if not guidance else "Agent usage guidance"),
        "",
    ]
    if guidance:
        sections.append(guidance)
        sections.append("")
        sections.append("JSON input schema (input_schema):")
        sections.append("")
    else:
        sections.append("(no sidecar guidance file for this tool)")
        sections.append("")
        sections.append("JSON input schema (input_schema):")
        sections.append("")
    schema_text = json.dumps(definition.input_schema, indent=2)
    sections.append(schema_text)
    sections.append("")
    sections.append(f"read_only: {str(definition.read_only).lower()}")
    content = "\n".join(sections).strip()
    return _ReadOutcome(
        content=f"Read documentation for tool {tool_name}.",
        data={
            "path": path,
            "type": "tool_documentation",
            "tool_name": definition.name,
            "read_only": definition.read_only,
            "guidance": guidance,
            "input_schema": definition.input_schema,
            "raw_content": content,
        },
    )


def _render_artifact(path: str, *, offset: int, limit: int) -> _ReadOutcome:
    """Render a bounded slice of a spilled tool-output artifact by URI.

    The artifact is resolved through the runtime's session-validated reader
    (``RuntimeToolInvocationContext.artifact``), which applies the session and
    artifact-path guards; the URI never falls through to workspace path
    resolution.
    """

    artifact_id = path[len(VOIDCODE_ARTIFACT_PREFIX) :].strip()
    if not artifact_id:
        raise ValueError("voidcode://artifact/<id> requires an artifact id")
    if _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
        raise ValueError(f"invalid artifact id: {artifact_id}")
    context = require_runtime_tool_context("read")
    facade = context.artifact
    if facade is None:
        raise ValueError("read cannot resolve voidcode://artifact URLs without a runtime artifact reader")
    result = facade.read_artifact(
        artifact_id=artifact_id,
        offset=max(0, offset - 1),
        limit=limit,
    )
    if result is None:
        raise ValueError(f"artifact not found in current session: {artifact_id}")
    status = result.get("status")
    if status == "missing":
        raise ValueError(f"artifact is missing from storage: {artifact_id}")
    if status != "available":
        raise ValueError(f"artifact read failed with status {status}: {artifact_id}")
    content = result.get("content")
    if not isinstance(content, str):
        raise ValueError(f"artifact read returned no content: {artifact_id}")
    line_count = result.get("line_count")
    next_offset = result.get("next_offset")
    truncated = next_offset is not None
    rendered_lines = content.splitlines()
    return _ReadOutcome(
        content=(
            f"Read {len(rendered_lines)} line(s) from {path}"
            + ("; output is truncated; continue reading with the returned next_offset." if truncated else ".")
        ),
        data={
            "path": path,
            "type": "artifact",
            "artifact_id": artifact_id,
            "status": status,
            "line_count": line_count if isinstance(line_count, int) else len(rendered_lines),
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset,
            "truncated": truncated,
            "partial": truncated,
            "byte_count": len(content.encode("utf-8")),
            "raw_content": content,
        },
    )


def _render_transcript(path: str, *, limit: int) -> _ReadOutcome:
    """Render a bounded, payload-stripped transcript of a session by URI.

    The transcript is resolved through the runtime's lineage-guarded reader
    (``RuntimeToolInvocationContext.transcript``): the caller may read its own
    session or a direct child session, never an unrelated session. Per event
    only ``sequence``, ``event_type``, and ``source`` are returned; raw tool
    output payloads are not included.
    """

    session_id = path[len(VOIDCODE_TRANSCRIPT_PREFIX) :].strip()
    if not session_id:
        raise ValueError("voidcode://transcript/<session_id> requires a session id")
    validate_session_id(session_id)
    context = require_runtime_tool_context("read")
    facade = context.transcript
    if facade is None:
        raise ValueError("read cannot resolve voidcode://transcript URLs without a runtime transcript reader")
    result = facade.read_transcript(session_id=session_id, limit=limit)
    if result is None:
        raise ValueError(f"transcript not accessible for session: {session_id}")
    transcript = result.get("transcript")
    if not isinstance(transcript, list):
        raise ValueError(f"transcript read returned no events for session: {session_id}")
    truncated = result.get("transcript_truncated") is True
    return _ReadOutcome(
        content=(
            f"Read {len(transcript)} transcript event(s) from {path}"
            + ("; transcript is truncated; raise the limit to see more." if truncated else ".")
        ),
        data={
            "path": path,
            "type": "transcript",
            "session_id": session_id,
            "status": result.get("status"),
            "summary": result.get("summary"),
            "last_event_sequence": result.get("last_event_sequence"),
            "message_limit": result.get("message_limit"),
            "transcript_count": result.get("transcript_count"),
            "transcript_truncated": truncated,
            "transcript": transcript,
        },
    )


class ReadArgs(BaseModel):
    path: str
    offset: int | None = None
    limit: int | None = None

    @field_validator("path", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        return value

    @field_validator("offset", mode="after")
    @classmethod
    def _validate_offset(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError("offset must be greater than or equal to 1")
        return value

    @field_validator("limit", mode="after")
    @classmethod
    def _validate_limit(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError("limit must be greater than or equal to 1")
        return value


DEFAULT_READ_LIMIT = 2000
DEFAULT_TRANSCRIPT_LIMIT = 20
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
            raise ValueError(f"read attachment exceeds the maximum supported size ({MAX_ATTACHMENT_BYTES} bytes): {relative_path}")
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
                "content_hash": hashlib.sha256(raw).hexdigest(),
                "attachment": {"mime": mime, "data_uri": data_uri},
                "truncated": False,
                "partial": False,
            },
        )

    if _is_binary_file(candidate):
        raise ValueError(f"read only supports text files or image/pdf attachments: {relative_path}")

    digest = hashlib.sha256()
    with candidate.open("rb") as hash_handle:
        for chunk in iter(lambda: hash_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    content_hash = digest.hexdigest()

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

                rendered_lines.append(line_text)
                bytes_used += encoded_size
                content_truncated = content_truncated or line_truncated
    except UnicodeDecodeError as exc:
        raise ValueError("read only supports UTF-8 text files") from exc

    if total_lines < offset and not (total_lines == 0 and offset == 1):
        raise ValueError(f"Offset {offset} is out of range for this file ({total_lines} lines)")

    next_offset = offset + len(rendered_lines)
    content_truncated = content_truncated or has_more

    return _ReadOutcome(
        content=(f"Read {len(rendered_lines)} line(s) from {relative_path}" + ("; output is truncated." if content_truncated else ".")),
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
            "content_hash": content_hash,
            "lines": [{"line": offset + index, "text": line} for index, line in enumerate(rendered_lines)],
            "raw_content": "\n".join(rendered_lines),
        },
    )


@final
class ReadTool:
    """Read a file or supported attachment from the current workspace."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="read",
        description="Read a file inside the current workspace.",
        input_schema={
            "path": {
                "type": "string",
                "description": (
                    "Path relative to the workspace (or an explicitly permitted external path). "
                    "Internal URLs: voidcode://tool/<name> reads a tool's guidance and input schema; "
                    "voidcode://artifact/<id> reads a bounded slice of the current session's spilled "
                    "tool-output artifact; voidcode://transcript/<session_id> reads a bounded, "
                    "payload-stripped transcript of the current session or of a child session it spawned."
                ),
            },
            "offset": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based line number to start reading from; defaults to the first line.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum lines to return; use data.next_offset to continue when truncated.",
            },
            "required": ["path"],
        },
        read_only=True,
        path_argument_keys=("path",),
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = ReadArgs.model_validate(
                {
                    "path": call.arguments.get("path"),
                    "offset": call.arguments.get("offset"),
                    "limit": call.arguments.get("limit"),
                }
            )
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        if args.path.startswith(VOIDCODE_TOOL_DOC_PREFIX):
            outcome = _render_tool_documentation(args.path)
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=outcome.content,
                data=outcome.data,
                truncated=bool(outcome.data.get("truncated", False)),
                partial=bool(outcome.data.get("partial", False)),
            )

        if args.path.startswith(VOIDCODE_ARTIFACT_PREFIX):
            outcome = _render_artifact(
                args.path,
                offset=args.offset or 1,
                limit=args.limit or DEFAULT_READ_LIMIT,
            )
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=outcome.content,
                data=outcome.data,
                truncated=bool(outcome.data.get("truncated", False)),
                partial=bool(outcome.data.get("partial", False)),
            )

        if args.path.startswith(VOIDCODE_TRANSCRIPT_PREFIX):
            outcome = _render_transcript(
                args.path,
                limit=args.limit or DEFAULT_TRANSCRIPT_LIMIT,
            )
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=outcome.content,
                data=outcome.data,
                truncated=bool(outcome.data.get("transcript_truncated", False)),
                partial=bool(outcome.data.get("transcript_truncated", False)),
            )

        resolution = resolve_workspace_path_policy(
            workspace=workspace,
            raw_path=args.path,
            allow_outside_workspace=True,
        )
        candidate = resolution.candidate
        relative_path = str(candidate.resolve()) if resolution.is_external else resolution.relative_path
        if not candidate.exists():
            raise ValueError(f"read target does not exist: {args.path}")

        offset = args.offset or 1
        limit = args.limit or DEFAULT_READ_LIMIT
        if candidate.is_dir():
            suggestions = suggest_workspace_paths(workspace=workspace, raw_path=args.path)
            suffix = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise ValueError(f"read does not support directories: {args.path}.{suffix}")
        if not candidate.is_file():
            raise ValueError(f"read only supports regular files: {args.path}")

        outcome = _render_file(candidate, relative_path=relative_path, offset=offset, limit=limit)

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=outcome.content,
            data=outcome.data,
            truncated=bool(outcome.data.get("truncated", False)),
            partial=bool(outcome.data.get("partial", False)),
            attachment=cast(dict[str, object], outcome.data.get("attachment")) if isinstance(outcome.data.get("attachment"), dict) else None,
        )
