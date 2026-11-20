"""Bounded file reads and typed resource URIs."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from concurrent.futures import CancelledError
from dataclasses import dataclass
from io import DEFAULT_BUFFER_SIZE, TextIOWrapper
from pathlib import Path
from typing import ClassVar, final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidcode.capabilities.resource_pages import MAX_RESOURCE_PAGE_BYTES, format_resource_text
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.resources import ResourceReadRequest, ResourceUnavailable
from voidcode.sdk.services import ArtifactReference, ArtifactServices, PluginServices
from voidcode.sdk.tools import (
    RASTER_IMAGE_MIMES,
    AttachmentOutput,
    ConfirmedStop,
    OpaqueToolBody,
    OutputBounds,
    ReadObservation,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolOutput,
    ToolResult,
    ToolSuccess,
)

from ._common import resolve_workspace_path, validate_non_empty


class ReadArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(description="File path or a resource URI.")
    offset: int | None = Field(
        default=None,
        ge=1,
        description="1-based starting text line.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=2000,
        description="Maximum text lines to return.",
    )
    byte_offset: int | None = Field(
        default=None, ge=0, description="Zero-based UTF-8 byte offset for continuing a long resource line; use the returned next_byte_offset."
    )
    byte_limit: int | None = Field(
        default=None, ge=4, le=MAX_RESOURCE_PAGE_BYTES, description="Maximum UTF-8 bytes in a resource page; requires byte_offset."
    )

    _path_non_empty = field_validator("path", mode="after")(validate_non_empty)


DEFAULT_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000
MAX_BYTES = 50 * 1024
BINARY_SNIFF_BYTES = 4096
_LINE_TRUNCATION_SUFFIX = f"... (line truncated to {MAX_LINE_LENGTH} chars)"


@dataclass(frozen=True, slots=True)
class ReadLine:
    line: int
    text: str
    truncated: bool

    def as_payload(self) -> dict[str, object]:
        return {"line": self.line, "text": self.text, "truncated": self.truncated}


@dataclass(frozen=True, slots=True)
class ReadResultBody:
    """Typed observation of one rendered workspace-file page."""

    line_count: int
    offset: int
    limit: int
    next_offset: int | None
    truncated: bool
    partial: bool
    byte_count: int
    content_hash: str
    lines: tuple[ReadLine, ...]
    raw_content: str

    @property
    def whole_file(self) -> bool:
        return (
            self.offset == 1
            and self.next_offset is None
            and self.line_count == len(self.lines)
            and not self.truncated
            and all(not line.truncated for line in self.lines)
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "type": "file",
            "line_count": self.line_count,
            "offset": self.offset,
            "limit": self.limit,
            "next_offset": self.next_offset,
            "truncated": self.truncated,
            "partial": self.partial,
            "byte_count": self.byte_count,
            "content_hash": self.content_hash,
            "lines": [line.as_payload() for line in self.lines],
            "raw_content": self.raw_content,
        }


@dataclass(frozen=True, slots=True)
class _ReadOutcome:
    output: ToolOutput
    body: ReadResultBody | OpaqueToolBody
    #: File versions this result made visible to the model; only bounded text
    #: pages of real files claim coverage; attachments, composite URI reads, and
    #: other URI reads grant nothing (part content is not verified file bytes).
    coverage: tuple[ReadObservation, ...] = ()


def _uri_failure(tool_name: str, message: str) -> ToolFailure:
    """A model-visible URI failure: the runtime redacts raised errors, so URI branches return text.

    Only resource URI reads use this; workspace file errors keep the file's existing
    raise-and-redact behavior, which is out of scope.
    """
    return ToolFailure(tool_name=tool_name, error=message, output=TextOutput(text=message))


def _truncate_line(line: str) -> tuple[str, bool]:
    if len(line) <= MAX_LINE_LENGTH:
        return line, False
    return f"{line[:MAX_LINE_LENGTH]}{_LINE_TRUNCATION_SUFFIX}", True


def _is_binary_content(path: Path, sample: bytes) -> bool:
    suffix = path.suffix.lower()
    if suffix in {
        ".pdf",
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

    if not sample:
        return False
    if b"\x00" in sample:
        return True

    non_printable = 0
    for byte in sample:
        if byte < 9 or (byte > 13 and byte < 32):
            non_printable += 1
    return non_printable / len(sample) > 0.3


def _render_file(
    candidate: Path, *, relative_path: str, offset: int, limit: int, services: ArtifactServices, context: ToolContext
) -> _ReadOutcome | ToolFailure:
    mime, _ = mimetypes.guess_type(candidate.name)
    is_svg = candidate.suffix.lower() == ".svg" or mime == "image/svg+xml"
    attachment = not is_svg and mime in RASTER_IMAGE_MIMES
    rendered_lines: list[ReadLine] = []
    raw_content_lines: list[str] = []
    total_lines = 0
    bytes_used = 0
    content_truncated = False
    has_more = False
    digest = hashlib.sha256()
    recovery_reference: ArtifactReference | None = None

    with candidate.open("rb") as source:
        sample = source.read(BINARY_SNIFF_BYTES)
        source.seek(0)
        if attachment or (not is_svg and _is_binary_content(candidate, sample)):
            effective_mime = mime or "application/octet-stream"
            artifact = services.spill_artifact_stream(iter(lambda: source.read(DEFAULT_BUFFER_SIZE), b""), media_type=effective_mime)
            uri = artifact.uri
            bounds = OutputBounds(reference=artifact, source=relative_path)
            body: dict[str, object] = {
                "path": relative_path,
                "type": "attachment" if attachment else "binary",
                "artifact": artifact.as_payload(),
            }
            if attachment:
                message = "Image read successfully"
                return _ReadOutcome(output=AttachmentOutput(effective_mime, None, presentation=message, bounds=bounds), body=OpaqueToolBody(body))
            presentation = (
                f"Binary file {relative_path} ({effective_mime}, {artifact.bytes} bytes, sha256:{artifact.sha256}). "
                f'Read full content with read(path="{uri}").'
            )
            return _ReadOutcome(output=TextOutput(presentation, bounds=bounds), body=OpaqueToolBody(body))
        try:
            with TextIOWrapper(source, encoding="utf-8", newline="") as text_source:
                for line_number, raw_line in enumerate(text_source, start=1):
                    if context.abort_signal is not None and context.abort_signal.cancelled:
                        return ToolFailure("read", error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
                    digest.update(raw_line.encode("utf-8"))
                    total_lines = line_number
                    if line_number < offset:
                        continue
                    if has_more or len(rendered_lines) >= limit:
                        has_more = True
                        continue
                    line_text = raw_line.rstrip("\r\n")
                    line_ending = raw_line[len(line_text) :]
                    line_text, line_truncated = _truncate_line(line_text)
                    rendered_raw_line = line_text + line_ending if line_truncated else raw_line
                    encoded_size = len(rendered_raw_line.encode("utf-8"))
                    if bytes_used + encoded_size > MAX_BYTES:
                        content_truncated = True
                        has_more = True
                        continue
                    rendered_lines.append(ReadLine(line_number, line_text, line_truncated))
                    raw_content_lines.append(rendered_raw_line)
                    bytes_used += encoded_size
                    content_truncated |= line_truncated
                if any(line.truncated for line in rendered_lines):
                    source.seek(0)
                    recovery_reference = services.spill_artifact_stream(
                        iter(lambda: source.read(DEFAULT_BUFFER_SIZE), b""), media_type="text/plain; charset=utf-8"
                    )
                    if recovery_reference.sha256 != digest.hexdigest():
                        raise ValueError(f"file changed while preparing long-line recovery; retry read: {relative_path}")
        except UnicodeDecodeError as exc:
            raise ValueError("read only supports UTF-8 text files") from exc
    content_hash = digest.hexdigest()

    if total_lines < offset and not (total_lines == 0 and offset == 1):
        raise ValueError(f"Offset {offset} is out of range for this file ({total_lines} lines)")

    next_offset = offset + len(rendered_lines)
    # Paging at a whole-line boundary does not clip the lines actually shown.
    lines_clipped = any(line.truncated for line in rendered_lines)
    content_truncated = content_truncated or has_more

    raw_content = "".join(raw_content_lines)
    body = ReadResultBody(
        line_count=total_lines,
        offset=offset,
        limit=limit,
        next_offset=next_offset if has_more else None,
        truncated=content_truncated,
        partial=content_truncated,
        byte_count=bytes_used,
        content_hash=content_hash,
        lines=tuple(rendered_lines),
        raw_content=raw_content,
    )
    return _ReadOutcome(
        output=format_resource_text(
            f"Read {len(rendered_lines)} line(s) from {relative_path}"
            + ("; output is truncated." if content_truncated else ".")
            + (f" Full text: read(path={json.dumps(recovery_reference.uri)}, byte_offset=0)." if recovery_reference is not None else ""),
            raw_content=body.raw_content,
            content_hash=body.content_hash,
            next_offset=body.next_offset,
            truncated=body.truncated,
            partial=body.partial,
            bounds=OutputBounds(truncated=body.truncated, partial=body.partial, reference=recovery_reference, source=relative_path),
        ),
        body=body,
        coverage=(
            ReadObservation(
                path=relative_path,
                content_hash=body.content_hash,
                lines=tuple(line.line for line in body.lines),
                line_count=body.line_count,
                whole_file=body.whole_file,
                clipped=lines_clipped,
            ),
        ),
    )


def _read_success(outcome: _ReadOutcome) -> ToolSuccess[ReadResultBody | OpaqueToolBody]:
    return ToolSuccess("read", output=outcome.output, body=outcome.body, coverage=outcome.coverage)


@final
class ReadTool:
    """Read a regular file or a resource URI."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="read",
        icon=Icon(glyph="⛁"),
        description="Read a file or a resource URI.",
        input_schema=ReadArgs.model_json_schema(),
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        args = ReadArgs.model_validate(dict(call.arguments))
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(self.definition.name, error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
        if args.byte_limit is not None and args.byte_offset is None:
            raise ValueError("byte_limit requires byte_offset")

        if "://" in args.path:
            try:
                request = ResourceReadRequest(
                    uri=args.path,
                    offset=args.offset or 1,
                    limit=args.limit,
                    byte_offset=args.byte_offset,
                    byte_limit=args.byte_limit,
                )
            except ValueError as exc:
                raise ValueError(f"unsupported resource URI: {args.path}") from exc
            try:
                page = services.read_resource(request)
            except ResourceUnavailable as exc:
                raise ValueError(f"unsupported resource URI: {args.path}") from exc
            except CancelledError:
                return ToolFailure(self.definition.name, error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
            if page is None:
                return _uri_failure(self.definition.name, f"read cannot resolve resource URI {args.path}: no such resource")
            return _read_success(_ReadOutcome(output=page.output, body=OpaqueToolBody(dict(page.body))))

        if args.byte_offset is not None or args.byte_limit is not None:
            raise ValueError("byte_offset and byte_limit require a resource URI")
        workspace = context.require_workspace()
        candidate, relative_path = resolve_workspace_path(workspace, args.path)
        offset = args.offset or 1
        limit = args.limit or DEFAULT_READ_LIMIT

        if not candidate.exists():
            raise ValueError(f"read target does not exist: {args.path}")
        if not candidate.is_file():
            raise ValueError(f"read only supports regular files: {args.path}")
        outcome = _render_file(candidate, relative_path=relative_path, offset=offset, limit=limit, services=services, context=context)
        return outcome if isinstance(outcome, ToolFailure) else _read_success(outcome)
