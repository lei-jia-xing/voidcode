"""Bounded file reads and typed resource URIs."""

from __future__ import annotations

import codecs
import hashlib
import json
import mimetypes
from collections.abc import Iterator
from concurrent.futures import CancelledError
from dataclasses import dataclass
from io import DEFAULT_BUFFER_SIZE
from pathlib import Path
from typing import BinaryIO, ClassVar, NoReturn, final

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
from .errors import expected_failures, raise_tool_diagnostic


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
    hash_only: bool = Field(
        default=False,
        description="Stream the whole regular file and return only its SHA-256 and byte size; mutually exclusive with page arguments.",
    )

    _path_non_empty = field_validator("path", mode="after")(validate_non_empty)


DEFAULT_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000
#: The rendered content budget leaves room for the TextOutput summary, hash,
#: cursor, and JSON envelope inside TOOL_OUTPUT_BYTES.
MAX_PAGE_CONTENT_BYTES = MAX_RESOURCE_PAGE_BYTES
BINARY_SNIFF_BYTES = 4096
_READ_CHUNK_BYTES = 8 * 1024
_MAX_RETAINED_LINE_BYTES = MAX_LINE_LENGTH * 4
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
    """Typed observation of one rendered workspace-file page.

    ``content_hash`` and ``line_count`` are whole-file facts: they are present
    only when this page actually consumed the file from offset 1 to EOF.
    """

    line_count: int | None
    offset: int
    limit: int
    next_offset: int | None
    truncated: bool
    partial: bool
    byte_count: int
    content_hash: str | None
    lines: tuple[ReadLine, ...]
    raw_content: str

    @property
    def whole_file(self) -> bool:
        return (
            self.offset == 1
            and self.next_offset is None
            and self.content_hash is not None
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
class ReadHashBody:
    """Typed result of the explicit whole-file hash-only read."""

    path: str
    sha256: str
    bytes: int

    def as_payload(self) -> dict[str, object]:
        return {"type": "hash_only", "path": self.path, "sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True, slots=True)
class _ReadOutcome:
    output: ToolOutput
    body: ReadResultBody | ReadHashBody | OpaqueToolBody
    #: File versions this result made visible to the model; only a whole-file
    #: page of a real file claims coverage; attachments, hash-only reads,
    #: composite URI reads, and other URI reads grant nothing.
    coverage: tuple[ReadObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class _RawLine:
    data: bytes
    ending: bytes
    clipped: bool


class _ReadCancelled(Exception):
    """Cancellation landed inside the bounded line scan."""


class _RawLineSplitter:
    """Split raw bytes into bounded lines without decoding unseen bytes."""

    __slots__ = ("_awaiting_lf", "_buffer", "_clipped", "_line_started", "_max_retained", "_parts")

    def __init__(self, max_retained: int = _MAX_RETAINED_LINE_BYTES) -> None:
        self._max_retained = max_retained
        self._buffer = b""
        self._parts = bytearray()
        self._clipped = False
        self._line_started = False
        self._awaiting_lf = False

    def has_buffered_content(self) -> bool:
        """True when bytes for a line beyond the delivered one are already buffered."""
        return bool(self._buffer) or self._awaiting_lf or self._line_started

    def clipped_pending(self) -> bool:
        """True when the open line already exceeds the retained cap and is still growing."""
        return self._line_started and self._clipped and not self._awaiting_lf

    def visible_prefix(self) -> bytes:
        """The retained raw prefix of the currently open line."""
        return bytes(self._parts)

    def feed(self, chunk: bytes, *, eof: bool = False) -> list[_RawLine]:
        if chunk:
            self._buffer += chunk
        completed: list[_RawLine] = []
        while True:
            if self._awaiting_lf:
                if self._buffer:
                    if self._buffer[0] == 0x0A:
                        self._buffer = self._buffer[1:]
                        completed.append(self._emit(b"\r\n"))
                    else:
                        completed.append(self._emit(b"\r"))
                    self._awaiting_lf = False
                    continue
                if eof:
                    self._awaiting_lf = False
                    completed.append(self._emit(b"\r"))
                break
            lf_at = self._buffer.find(b"\n")
            cr_at = self._buffer.find(b"\r")
            if cr_at != -1 and (lf_at == -1 or cr_at < lf_at):
                if cr_at + 1 < len(self._buffer):
                    self._append(self._buffer[:cr_at])
                    if self._buffer[cr_at + 1] == 0x0A:
                        self._buffer = self._buffer[cr_at + 2 :]
                        completed.append(self._emit(b"\r\n"))
                    else:
                        self._buffer = self._buffer[cr_at + 1 :]
                        completed.append(self._emit(b"\r"))
                    continue
                if eof:
                    self._append(self._buffer[:cr_at])
                    self._buffer = b""
                    completed.append(self._emit(b"\r"))
                    continue
                self._append(self._buffer[:cr_at])
                self._buffer = b""
                self._awaiting_lf = True
                break
            if lf_at != -1:
                self._append(self._buffer[:lf_at])
                self._buffer = self._buffer[lf_at + 1 :]
                completed.append(self._emit(b"\n"))
                continue
            self._append(self._buffer)
            self._buffer = b""
            break
        if eof and not self._awaiting_lf and self._line_started:
            completed.append(self._emit(b""))
        return completed

    def _append(self, data: bytes) -> None:
        if not data:
            return
        self._line_started = True
        if self._clipped:
            return
        room = self._max_retained - len(self._parts)
        if len(data) > room:
            self._parts.extend(data[:room])
            self._clipped = True
        else:
            self._parts.extend(data)

    def _emit(self, ending: bytes) -> _RawLine:
        line = _RawLine(bytes(self._parts), ending, self._clipped)
        self._parts = bytearray()
        self._clipped = False
        self._line_started = False
        return line


def _decode_line_text(raw: _RawLine) -> tuple[str, bool]:
    """Decode only this line's retained bytes; unseen later bytes stay untouched."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        text = decoder.decode(raw.data, final=not raw.clipped)
    except UnicodeDecodeError:
        raise_tool_diagnostic(
            message="read only supports UTF-8 text files",
            error_kind="tool_input_mismatch",
            reason="unsupported_encoding",
            retry_guidance="Use a binary or external tool for non-UTF-8 content.",
        )
    clipped = raw.clipped
    if len(text) > MAX_LINE_LENGTH:
        text = text[:MAX_LINE_LENGTH]
        clipped = True
    if clipped:
        return f"{text}{_LINE_TRUNCATION_SUFFIX}", True
    return text, False


class _PageBuilder:
    """Render at most one page; keep counting lines when the caller needs EOF."""

    __slots__ = (
        "bytes_used",
        "clipped",
        "done",
        "has_more",
        "limit",
        "offset",
        "raw_lines",
        "raw_prefix",
        "prefix_closed",
        "rendered",
        "total_lines",
    )

    def __init__(self, offset: int, limit: int) -> None:
        self.offset = offset
        self.limit = limit
        self.rendered: list[ReadLine] = []
        self.raw_lines: list[str] = []
        self.raw_prefix = bytearray()
        self.prefix_closed = False
        self.bytes_used = 0
        self.total_lines = 0
        self.clipped = False
        self.has_more = False
        self.done = False

    def captured_prefix(self) -> bytes:
        return bytes(self.raw_prefix)

    def consume(self, raw: _RawLine) -> None:
        self.total_lines += 1
        if self.done:
            self.has_more = True
            return
        if self.total_lines < self.offset:
            return
        if len(self.rendered) >= self.limit:
            self.has_more = True
            self.done = True
            return
        text, clipped = _decode_line_text(raw)
        raw_line = f"{text}{raw.ending.decode('ascii')}"
        encoded_size = len(raw_line.encode("utf-8"))
        if self.bytes_used + encoded_size > MAX_PAGE_CONTENT_BYTES:
            self.has_more = True
            self.done = True
            return
        if not self.prefix_closed:
            self.raw_prefix.extend(raw.data)
            if raw.clipped:
                self.prefix_closed = True
            else:
                self.raw_prefix.extend(raw.ending)
        self.rendered.append(ReadLine(self.total_lines, text, clipped))
        self.raw_lines.append(raw_line)
        self.bytes_used += encoded_size
        self.clipped = self.clipped or clipped
        if len(self.rendered) >= self.limit:
            self.done = True


def _scan_normal_page(
    source: BinaryIO,
    *,
    offset: int,
    limit: int,
    context: ToolContext,
) -> tuple[_PageBuilder, bool, bool, str | None, bytes | None]:
    """Scan only through the page plus a one-byte EOF probe.

    Returns ``(page, eof, needs_full_pass, content_hash, expected_prefix)``.
    ``needs_full_pass`` means a visible line is already known to exceed the
    retained cap, so the caller must restart for the artifact-backed page.
    ``expected_prefix`` is that pass's captured visible raw prefix. Unseen bytes
    after the page are never decoded.
    """
    splitter = _RawLineSplitter()
    page = _PageBuilder(offset, limit)
    digest = hashlib.sha256() if offset == 1 else None
    while True:
        if context.abort_signal is not None and context.abort_signal.cancelled:
            raise _ReadCancelled
        chunk = source.read(_READ_CHUNK_BYTES)
        if digest is not None:
            digest.update(chunk)
        eof = not chunk
        lines = splitter.feed(chunk, eof=eof)
        for index, raw in enumerate(lines):
            page.consume(raw)
            if page.clipped:
                return page, eof, True, None, page.captured_prefix()
            if page.done:
                if page.has_more or index + 1 < len(lines) or splitter.has_buffered_content():
                    page.has_more = True
                    return page, False, False, None, None
                if eof:
                    return page, True, False, digest.hexdigest() if digest is not None else None, None
                probe = source.read(1)
                if probe:
                    page.has_more = True
                    return page, False, False, None, None
                return page, True, False, digest.hexdigest() if digest is not None else None, None
        if not page.done and splitter.clipped_pending() and page.total_lines + 1 >= offset:
            return page, eof, True, None, page.captured_prefix() + splitter.visible_prefix()
        if eof:
            return page, True, False, digest.hexdigest() if digest is not None else None, None


def _raise_recovery_changed(relative_path: str) -> NoReturn:
    raise_tool_diagnostic(
        message=f"file changed while preparing long-line recovery; retry read: {relative_path}",
        error_kind="stale_read",
        reason="file_changed_during_recovery",
        retry_guidance="Retry the read; the file changed between the page read and the artifact pass.",
        details={"path": relative_path},
    )


def _scan_clipped_page(
    source: BinaryIO,
    *,
    offset: int,
    limit: int,
    services: ArtifactServices,
    relative_path: str,
    context: ToolContext,
    expected_prefix: bytes | None,
) -> tuple[_PageBuilder, ArtifactReference | None]:
    """One full-file pass that both renders the clipped page and spills the artifact."""
    digest = hashlib.sha256()
    page = _PageBuilder(offset, limit)
    splitter = _RawLineSplitter()
    bytes_read = 0
    prefix_window = bytearray()
    prefix_remaining = len(expected_prefix) if expected_prefix is not None and offset == 1 else 0

    def chunks() -> Iterator[bytes]:
        nonlocal bytes_read, prefix_remaining
        while True:
            if context.abort_signal is not None and context.abort_signal.cancelled:
                raise _ReadCancelled
            chunk = source.read(_READ_CHUNK_BYTES)
            if not chunk:
                for raw in splitter.feed(b"", eof=True):
                    page.consume(raw)
                break
            bytes_read += len(chunk)
            digest.update(chunk)
            if prefix_remaining:
                take = min(len(chunk), prefix_remaining)
                prefix_window.extend(chunk[:take])
                prefix_remaining -= take
            for raw in splitter.feed(chunk):
                page.consume(raw)
            yield chunk

    artifact = services.spill_artifact_stream(chunks(), media_type="text/plain; charset=utf-8")
    if artifact.sha256 != digest.hexdigest() or artifact.bytes != bytes_read:
        _raise_recovery_changed(relative_path)
    prefix_mismatch = prefix_remaining != 0 or (offset == 1 and expected_prefix is not None and bytes(prefix_window) != expected_prefix)
    if not page.clipped:
        # The overlong line did not survive into the visible page (for example it
        # exceeded the page budget); there is nothing to recover for this page.
        if prefix_mismatch or not page.has_more:
            _raise_recovery_changed(relative_path)
        return page, None
    if prefix_mismatch or (expected_prefix is not None and page.captured_prefix() != expected_prefix):
        _raise_recovery_changed(relative_path)
    return page, artifact


def _assemble_page_outcome(
    page: _PageBuilder,
    *,
    line_count: int | None,
    content_hash: str | None,
    recovery_reference: ArtifactReference | None,
    relative_path: str,
    offset: int,
    limit: int,
) -> _ReadOutcome:
    truncated = page.has_more or page.clipped
    next_offset = offset + len(page.rendered) if page.has_more else None
    raw_content = "".join(page.raw_lines)
    body = ReadResultBody(
        line_count=line_count,
        offset=offset,
        limit=limit,
        next_offset=next_offset,
        truncated=truncated,
        partial=truncated,
        byte_count=page.bytes_used,
        content_hash=content_hash,
        lines=tuple(page.rendered),
        raw_content=raw_content,
    )

    coverage: tuple[ReadObservation, ...] = ()
    if body.whole_file:
        assert body.content_hash is not None and body.line_count is not None
        coverage = (
            ReadObservation(
                path=relative_path,
                content_hash=body.content_hash,
                lines=tuple(line.line for line in body.lines),
                line_count=body.line_count,
                whole_file=True,
                clipped=False,
            ),
        )

    summary = f"Read {len(page.rendered)} line(s) from {relative_path}"
    summary += "; output is truncated." if truncated else "."
    if recovery_reference is not None:
        summary += f" Full text: read(path={json.dumps(recovery_reference.uri)}, byte_offset=0)."
    return _ReadOutcome(
        output=format_resource_text(
            summary,
            raw_content=raw_content,
            content_hash=content_hash,
            next_offset=next_offset,
            truncated=truncated,
            partial=truncated,
            bounds=OutputBounds(truncated=truncated, partial=truncated, reference=recovery_reference, source=relative_path),
        ),
        body=body,
        coverage=coverage,
    )


def _resolve_read_path(workspace: Path, raw_path: str) -> tuple[Path, str]:
    try:
        return resolve_workspace_path(workspace, raw_path)
    except ValueError as exc:
        raise_tool_diagnostic(
            message=f"read could not resolve the target path {raw_path!r}: {exc}",
            error_kind="tool_input_mismatch",
            reason="invalid_path",
            retry_guidance="Retry with a valid file path or resource URI.",
            details={"path": raw_path},
        )


def _uri_failure(tool_name: str, message: str) -> ToolFailure:
    """A model-visible failure for a resource URI the registered reader could not resolve.

    URI failures are returned as typed results so the model sees them directly;
    workspace file read errors keep the files' existing raise-and-redact behavior.
    """
    return ToolFailure(tool_name=tool_name, error=message, output=TextOutput(text=message))


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


def _read_text_page(
    candidate: Path,
    *,
    relative_path: str,
    offset: int,
    limit: int,
    services: ArtifactServices,
    context: ToolContext,
) -> _ReadOutcome | ToolFailure:
    try:
        with candidate.open("rb") as source:
            page, eof, needs_full_pass, content_hash, expected_prefix = _scan_normal_page(source, offset=offset, limit=limit, context=context)
        if needs_full_pass:
            with candidate.open("rb") as source:
                page, artifact = _scan_clipped_page(
                    source,
                    offset=offset,
                    limit=limit,
                    services=services,
                    relative_path=relative_path,
                    context=context,
                    expected_prefix=expected_prefix,
                )
            return _assemble_page_outcome(
                page,
                line_count=page.total_lines,
                content_hash=None,
                recovery_reference=artifact,
                relative_path=relative_path,
                offset=offset,
                limit=limit,
            )
    except _ReadCancelled:
        return ToolFailure("read", error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))

    if eof and page.total_lines < offset and not (page.total_lines == 0 and offset == 1):
        raise_tool_diagnostic(
            message=f"Offset {offset} is out of range for this file ({page.total_lines} lines)",
            error_kind="tool_input_mismatch",
            reason="offset_out_of_range",
            retry_guidance="Retry from offset 1 or a line within the file.",
            details={"offset": offset, "line_count": page.total_lines},
        )
    return _assemble_page_outcome(
        page,
        line_count=page.total_lines if eof else None,
        content_hash=content_hash,
        recovery_reference=None,
        relative_path=relative_path,
        offset=offset,
        limit=limit,
    )


def _hash_regular_file(candidate: Path, *, relative_path: str, context: ToolContext) -> _ReadOutcome | ToolFailure:
    digest = hashlib.sha256()
    size = 0
    with candidate.open("rb") as source:
        while True:
            if context.abort_signal is not None and context.abort_signal.cancelled:
                return ToolFailure("read", error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
            chunk = source.read(DEFAULT_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    content_hash = digest.hexdigest()
    return _ReadOutcome(
        output=TextOutput(f"SHA-256 content hash for {relative_path}: {content_hash}\nByte size: {size} bytes"),
        body=ReadHashBody(path=relative_path, sha256=content_hash, bytes=size),
    )


def _render_file(
    candidate: Path, *, relative_path: str, offset: int, limit: int, services: ArtifactServices, context: ToolContext
) -> _ReadOutcome | ToolFailure:
    mime, _ = mimetypes.guess_type(candidate.name)
    is_svg = candidate.suffix.lower() == ".svg" or mime == "image/svg+xml"
    attachment = not is_svg and mime in RASTER_IMAGE_MIMES

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

    return _read_text_page(candidate, relative_path=relative_path, offset=offset, limit=limit, services=services, context=context)


def _read_success(outcome: _ReadOutcome) -> ToolSuccess[ReadResultBody | ReadHashBody | OpaqueToolBody]:
    return ToolSuccess("read", output=outcome.output, body=outcome.body, coverage=outcome.coverage)


@final
class ReadTool:
    """Read a regular file or a resource URI."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="read",
        icon=Icon(glyph="⛁"),
        description=(
            "Read a file or a resource URI. Whole-file pages report a SHA-256 content hash; partial pages do not. "
            "Use hash_only=true to stream a whole regular file and obtain only its SHA-256 for an edit."
        ),
        input_schema=ReadArgs.model_json_schema(),
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
    )

    @expected_failures
    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        args = ReadArgs.model_validate(dict(call.arguments))
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(self.definition.name, error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))

        if args.hash_only:
            if args.offset is not None or args.limit is not None or args.byte_offset is not None or args.byte_limit is not None:
                raise_tool_diagnostic(
                    message="hash_only cannot be combined with offset, limit, byte_offset, or byte_limit",
                    error_kind="tool_input_mismatch",
                    reason="hash_only_with_page_arguments",
                    retry_guidance="Retry with hash_only alone, or omit hash_only to page the file.",
                )
            if "://" in args.path:
                raise_tool_diagnostic(
                    message="hash_only only supports regular file paths",
                    error_kind="tool_input_mismatch",
                    reason="hash_only_uri",
                    retry_guidance="Retry with a regular file path; URI resources use their own readers.",
                    details={"path": args.path},
                )
            workspace = context.require_workspace()
            candidate, relative_path = _resolve_read_path(workspace, args.path)
            if not candidate.exists():
                raise_tool_diagnostic(
                    message=f"read target does not exist: {args.path}",
                    error_kind="tool_input_mismatch",
                    reason="target_not_found",
                    retry_guidance="Retry with an existing file path.",
                    details={"path": args.path},
                )
            if not candidate.is_file():
                raise_tool_diagnostic(
                    message=f"read only supports regular files: {args.path}",
                    error_kind="tool_input_mismatch",
                    reason="not_a_regular_file",
                    retry_guidance="Retry with a regular file path.",
                    details={"path": args.path},
                )
            outcome = _hash_regular_file(candidate, relative_path=relative_path, context=context)
            return outcome if isinstance(outcome, ToolFailure) else _read_success(outcome)

        if args.byte_limit is not None and args.byte_offset is None:
            raise_tool_diagnostic(
                message="byte_limit requires byte_offset",
                error_kind="tool_input_mismatch",
                reason="byte_limit_without_byte_offset",
                retry_guidance="Retry with both byte_offset and byte_limit, or omit both.",
            )

        if "://" in args.path:
            try:
                request = ResourceReadRequest(
                    uri=args.path,
                    offset=args.offset or 1,
                    limit=args.limit,
                    byte_offset=args.byte_offset,
                    byte_limit=args.byte_limit,
                )
            except ValueError:
                raise_tool_diagnostic(
                    message=f"unsupported resource URI: {args.path}",
                    error_kind="tool_input_mismatch",
                    reason="unsupported_resource_uri",
                    retry_guidance="Retry with a supported resource URI.",
                    details={"uri": args.path},
                )
            try:
                page = services.read_resource(request)
            except ResourceUnavailable:
                raise_tool_diagnostic(
                    message=f"unsupported resource URI: {args.path}",
                    error_kind="tool_input_mismatch",
                    reason="unsupported_resource_uri",
                    retry_guidance="Retry with a supported resource URI.",
                    details={"uri": args.path},
                )
            except CancelledError:
                return ToolFailure(self.definition.name, error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
            if page is None:
                return _uri_failure(self.definition.name, f"read cannot resolve resource URI {args.path}: no such resource")
            return _read_success(_ReadOutcome(output=page.output, body=OpaqueToolBody(dict(page.body))))

        if args.byte_offset is not None or args.byte_limit is not None:
            raise_tool_diagnostic(
                message="byte_offset and byte_limit require a resource URI",
                error_kind="tool_input_mismatch",
                reason="byte_arguments_require_resource_uri",
                retry_guidance="Retry with a resource URI, or omit byte_offset and byte_limit for a text file page.",
            )
        workspace = context.require_workspace()
        candidate, relative_path = _resolve_read_path(workspace, args.path)
        offset = args.offset or 1
        limit = args.limit or DEFAULT_READ_LIMIT

        if not candidate.exists():
            raise_tool_diagnostic(
                message=f"read target does not exist: {args.path}",
                error_kind="tool_input_mismatch",
                reason="target_not_found",
                retry_guidance="Retry with an existing file path.",
                details={"path": args.path},
            )
        if not candidate.is_file():
            raise_tool_diagnostic(
                message=f"read only supports regular files: {args.path}",
                error_kind="tool_input_mismatch",
                reason="not_a_regular_file",
                retry_guidance="Retry with a regular file path.",
                details={"path": args.path},
            )
        outcome = _render_file(candidate, relative_path=relative_path, offset=offset, limit=limit, services=services, context=context)
        return outcome if isinstance(outcome, ToolFailure) else _read_success(outcome)
