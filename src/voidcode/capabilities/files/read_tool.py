"""Bounded file reads and typed resource URIs."""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import mimetypes
import re
from collections.abc import Mapping
from concurrent.futures import CancelledError
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from typing import ClassVar, final
from urllib.parse import parse_qs

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidcode.capabilities.workspace_queries import RULE_PAGES, RULE_URI_PREFIX, RulePageRequest
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import ArtifactPage, PluginServices, QueryContractError, QueryUnavailable
from voidcode.sdk.tools import (
    AttachmentOutput,
    ConfirmedStop,
    OpaqueToolBody,
    OutputBounds,
    OutputReference,
    ReadObservation,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
    is_read_tier,
)

_ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_ARTIFACT_PAGE_BYTES = 50 * 1024 - 4096
MAX_INLINE_ATTACHMENT_BYTES = (50 * 1024 - 128) * 3 // 4
MAX_BINARY_READ_BYTES = 50 * 1024 * 1024
RASTER_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})

TOOL_URI_PREFIX = "tool://"
ARTIFACT_URI_PREFIX = "artifact://"
TRANSCRIPT_URI_PREFIX = "transcript://"


def _spill_artifact(
    services: PluginServices,
    *,
    kind: str,
    content: str | bytes,
    mime: str,
    source: str,
) -> tuple[Mapping[str, object], str, str]:
    """Spill a payload and project its reference as ``(artifact, artifact_id, uri)``."""
    reference = services.spill_artifact(content, media_type=mime)
    uri = f"artifact://{reference.sha256}"
    artifact: dict[str, object] = {
        "artifact_id": reference.sha256,
        "sha256": reference.sha256,
        "byte_count": reference.bytes,
        "content_type": reference.media_type,
        "mime": reference.media_type,
        "uri": uri,
        "kind": kind,
        "status": "available",
        "source": source,
    }
    return artifact, reference.sha256, uri


def _render_tool_documentation(
    path: str, *, context: ToolContext, services: PluginServices, tool_name_label: str = "read"
) -> _ReadOutcome | ToolFailure:
    tool_name = path[len(TOOL_URI_PREFIX) :].strip()
    if not tool_name:
        return _uri_failure(tool_name_label, "tool://<name> requires a tool name")
    context.require_session_id()
    definition = services.tool_definition(tool_name)
    if definition is None:
        return _uri_failure(
            tool_name_label,
            f"no tool documentation for {tool_name}: the active revision admits no such tool, "
            "or this session's profile scope/read-only stance hides it",
        )
    guidance = definition.description
    schema = json_wire_object(definition.input_schema)
    effects = sorted(effect.value for effect in definition.effects)
    read_only = is_read_tier(definition.effects)
    content = "\n\n".join(
        (
            f"# Tool: {definition.name}",
            guidance,
            "JSON input schema (input_schema):",
            json.dumps(schema, indent=2),
            "effects: " + ", ".join(effects),
            f"read_only: {str(read_only).lower()}",
        )
    )
    return _ReadOutcome(
        output=_text_output(f"Read documentation for tool {tool_name}.", raw_content=content),
        body=OpaqueToolBody(
            {
                "path": path,
                "type": "tool_documentation",
                "tool_name": definition.name,
                "effects": effects,
                "read_only": read_only,
                "guidance": guidance,
                "input_schema": schema,
                "raw_content": content,
            }
        ),
    )


def _render_artifact(
    path: str,
    *,
    context: ToolContext,
    services: PluginServices,
    offset: int,
    limit: int,
    byte_offset: int | None = None,
    byte_limit: int | None = None,
) -> _ReadOutcome:
    """Read a bounded text page or an existing image artifact."""

    artifact_id = path[len(ARTIFACT_URI_PREFIX) :].strip()
    if not artifact_id:
        raise ValueError("artifact://<id> requires an artifact id")
    if _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
        raise ValueError(f"invalid artifact id: {artifact_id}")

    def _window(byte_start: int, byte_length: int = MAX_ARTIFACT_PAGE_BYTES) -> ArtifactPage:
        if context.abort_signal is not None and context.abort_signal.cancelled:
            raise CancelledError("read cancelled")
        try:
            return services.read_artifact(artifact_id, offset=byte_start, limit=byte_length)
        except (OSError, ValueError, LookupError) as exc:
            raise ValueError(f"read cannot resolve artifact URI {path}: {exc}") from exc

    head: ArtifactPage | None = None

    def _page(byte_start: int, byte_length: int = MAX_ARTIFACT_PAGE_BYTES) -> ArtifactPage:
        """Reuse the first page for image classification and text paging."""
        nonlocal head
        if byte_start == 0 and byte_length == MAX_ARTIFACT_PAGE_BYTES:
            if head is None:
                head = _window(0, byte_length)
            return head
        return _window(byte_start, byte_length)

    first_page = _page(0)
    media_type = first_page.media_type

    if byte_offset is None and media_type in RASTER_IMAGE_MIMES:
        total_bytes = first_page.total_bytes
        content_hash = first_page.sha256
        artifact: Mapping[str, object] = {"artifact_id": artifact_id, "sha256": content_hash, "byte_count": total_bytes, "uri": path}
        uri = path
        data_uri = None
        if total_bytes <= MAX_INLINE_ATTACHMENT_BYTES:
            raw = first_page.data
            data_uri = f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"
        presentation = "Image artifact read successfully"
        if data_uri is None:
            presentation += f"; the full image is stored at {uri}."
        return _ReadOutcome(
            output=AttachmentOutput(
                media_type,
                data_uri,
                presentation=presentation,
                bounds=OutputBounds(reference=OutputReference(uri=uri, artifact=artifact), source=path),
            ),
            body=OpaqueToolBody(
                {
                    "path": path,
                    "type": "attachment",
                    "content_type": media_type,
                    "mime": media_type,
                    "byte_count": total_bytes,
                    "content_hash": content_hash,
                    "sha256": content_hash,
                    "artifact_id": artifact_id,
                    "uri": uri,
                    "attachment": {"mime": media_type, "reference": uri},
                    "bounds": {"reference": uri},
                }
            ),
        )

    if byte_offset is not None:
        window_limit = min(byte_limit if byte_limit is not None else MAX_ARTIFACT_PAGE_BYTES, MAX_ARTIFACT_PAGE_BYTES)
        window = _page(byte_offset, window_limit)
        data = window.data
        total_bytes = window.total_bytes
        content_hash = window.sha256
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        raw_content = decoder.decode(data, final=window.eof)
        if len(raw_content.encode("utf-8")) > MAX_ARTIFACT_PAGE_BYTES:
            # Each invalid input byte can expand to a three-byte replacement.
            data = data[: MAX_ARTIFACT_PAGE_BYTES // 3]
            decoder.reset()
            raw_content = decoder.decode(data, final=False)
        consumed = len(data) - len(decoder.getstate()[0])
        next_byte_offset = None if window.eof and consumed == len(window.data) else window.offset + consumed
        truncated = next_byte_offset is not None
        artifact: Mapping[str, object] = {"artifact_id": artifact_id, "sha256": content_hash, "byte_count": total_bytes, "uri": path}
        summary = f"Read {consumed} artifact byte(s) from byte offset {byte_offset}"
        summary += f"; continue with byte_offset={next_byte_offset}." if next_byte_offset is not None else "."
        return _ReadOutcome(
            output=_text_output(
                summary,
                raw_content=raw_content,
                content_hash=content_hash,
                next_byte_offset=next_byte_offset,
                truncated=truncated,
                partial=truncated,
                bounds=OutputBounds(truncated=truncated, partial=truncated, reference=OutputReference(uri=path, artifact=artifact)),
            ),
            body=OpaqueToolBody(
                {
                    "path": path,
                    "type": "artifact",
                    "artifact_id": artifact_id,
                    "offset": byte_offset,
                    "offset_unit": "bytes",
                    "byte_offset": byte_offset,
                    "byte_limit": window_limit,
                    "next_byte_offset": next_byte_offset,
                    "truncated": truncated,
                    "partial": truncated,
                    "byte_count": consumed,
                    "artifact_byte_count": total_bytes,
                    "content_hash": content_hash,
                    "raw_content": raw_content,
                }
            ),
        )

    # The store pages by bytes, so line paging walks windows until the
    # requested 1-based line starts, then renders one bounded line page.
    page_limit = min(limit, DEFAULT_READ_LIMIT)
    byte_start = 0
    remaining = max(0, offset - 1)
    while remaining > 0:
        window = _page(byte_start)
        chunk = window.data
        newlines = chunk.count(b"\n")
        if newlines >= remaining:
            index = -1
            for _ in range(remaining):
                index = chunk.index(b"\n", index + 1)
            byte_start += index + 1
            break
        remaining -= newlines
        if not chunk or window.eof:
            raise ValueError(f"Offset {offset} is out of range for this artifact")
        byte_start += len(chunk)

    window = _page(byte_start)
    data = window.data
    total_bytes = window.total_bytes
    content_hash = window.sha256
    # A byte window may stop inside a line. Leave that fragment for the next
    # line page; a line larger than the window must continue by byte instead.
    if not window.eof:
        last_newline = data.rfind(b"\n")
        if last_newline == -1:
            return _render_artifact(path, context=context, services=services, offset=offset, limit=limit, byte_offset=byte_start)
        data = data[: last_newline + 1]
    text = data.decode("utf-8", "replace")
    if len(text.encode("utf-8")) > MAX_ARTIFACT_PAGE_BYTES:
        return _render_artifact(path, context=context, services=services, offset=offset, limit=limit, byte_offset=byte_start)
    page_lines = text.split("\n")
    if page_lines[-1] == "":
        page_lines.pop()
    page_lines = [line[:-1] if line.endswith("\r") else line for line in page_lines]
    shown = page_lines[:page_limit]
    has_more = len(shown) < len(page_lines) or not window.eof
    next_offset = offset + len(shown) if has_more else None
    raw_content = "\n".join(shown)
    if shown and (len(shown) < len(page_lines) or text.endswith("\n")):
        raw_content += "\n"
    artifact = {"artifact_id": artifact_id, "sha256": content_hash, "byte_count": total_bytes, "uri": path}
    summary = f"Read {len(shown)} line(s) from {path}"
    if has_more:
        summary += f"; continue with offset={next_offset}." if next_offset is not None else "; output is truncated."
    else:
        summary += "."
    return _ReadOutcome(
        output=_text_output(
            summary,
            raw_content=raw_content,
            content_hash=content_hash,
            next_offset=next_offset,
            truncated=has_more,
            partial=has_more,
            bounds=OutputBounds(truncated=has_more, partial=has_more, reference=OutputReference(uri=path, artifact=artifact)),
        ),
        body=OpaqueToolBody(
            {
                "path": path,
                "type": "artifact",
                "artifact_id": artifact_id,
                "offset": offset,
                "offset_unit": "lines",
                "limit": page_limit,
                "next_offset": next_offset,
                "truncated": has_more,
                "partial": has_more,
                "byte_count": len(raw_content.encode("utf-8")),
                "artifact_byte_count": total_bytes,
                "content_hash": content_hash,
                "raw_content": raw_content,
                "line_count": len(shown),
            }
        ),
    )


def _render_transcript(
    path: str,
    *,
    services: PluginServices,
    limit: int,
    tool_name_label: str = "read",
) -> _ReadOutcome | ToolFailure:
    """Render a readable, bounded transcript page of the caller or one of its descendants.

    The core enforces lineage: parents, siblings, unrelated roots, and foreign workspaces are
    refused before any event is read. Payloads are included so the page shows actual prompts,
    assistant text, tool calls/results, and progress instead of event names only; provider
    reasoning is deliberately not rendered. ``?after_sequence=<n>&offset=<bytes>`` continues
    exactly where the previous page stopped.
    """
    raw_target = path[len(TRANSCRIPT_URI_PREFIX) :].strip()
    session_id, _, query = raw_target.partition("?")
    session_id = session_id.strip()
    if not session_id:
        return _uri_failure(tool_name_label, "transcript://<session_id> requires a session id")
    if "/" in session_id or "?" in session_id:
        return _uri_failure(tool_name_label, "session_id must not contain '/' or '?'")
    after_sequence = 0
    offset = 0
    if query:
        try:
            params = parse_qs(query, strict_parsing=True, keep_blank_values=True)
        except ValueError as exc:
            return _uri_failure(tool_name_label, f"invalid transcript query: {exc}")
        unknown = sorted(set(params) - {"after_sequence", "offset"})
        if unknown:
            return _uri_failure(tool_name_label, f"unsupported transcript query parameter: {unknown[0]}")
        for name in ("after_sequence", "offset"):
            values = params.get(name)
            if values is None:
                continue
            if len(values) != 1 or not values[0].isdecimal():
                return _uri_failure(tool_name_label, f"transcript {name} must be one non-negative integer")
            try:
                value = int(values[0])
            except ValueError:
                return _uri_failure(tool_name_label, f"transcript {name} exceeds the supported integer length")
            if name == "after_sequence":
                after_sequence = value
            else:
                offset = value
    limit = max(1, min(limit, MAX_EVENT_PAGE))
    try:
        page = services.read_session_history(session_id, after_sequence=after_sequence, offset=offset, limit=limit)
    except Exception as exc:
        return _uri_failure(tool_name_label, f"transcript not accessible for session {session_id}: {exc}")
    truncated = page.truncated
    body_text = page.text if page.text else "(no readable events in this range)"
    lines = [
        f"Transcript for session {session_id} after sequence {after_sequence} (untrusted session data).",
        body_text,
    ]
    next_uri: str | None = None
    if truncated:
        next_uri = f"{TRANSCRIPT_URI_PREFIX}{session_id}?after_sequence={page.next_after_sequence}&offset={page.next_offset}"
        lines.append(f"More history: read(path={json.dumps(next_uri)}).")
    body = {
        "path": path,
        "type": "transcript",
        "session_id": session_id,
        "after_sequence": after_sequence,
        "next_after_sequence": page.next_after_sequence,
        "next_offset": page.next_offset,
        "truncated": truncated,
    }
    return _ReadOutcome(
        output=_text_output(
            "\n".join(lines),
            truncated=truncated,
            partial=truncated,
            bounds=OutputBounds(
                truncated=truncated,
                partial=truncated,
                reference=None if next_uri is None else OutputReference(uri=next_uri),
            ),
        ),
        body=OpaqueToolBody(body),
    )


class ReadArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(description="File path, artifact://<id>, tool://<name>, rule://<name>, or transcript://<session_id>.")
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
        default=None, ge=0, description="Zero-based UTF-8 byte offset for continuing a long artifact line; use the returned next_byte_offset."
    )
    byte_limit: int | None = Field(
        default=None, ge=4, le=MAX_ARTIFACT_PAGE_BYTES, description="Maximum UTF-8 bytes in an artifact page; requires byte_offset."
    )

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        return value


DEFAULT_READ_LIMIT = 2000
DEFAULT_TRANSCRIPT_LIMIT = 20
#: One transcript read never asks the core for more events than one event page carries.
MAX_EVENT_PAGE = 200
MAX_LINE_LENGTH = 2000
MAX_BYTES = 50 * 1024
MAX_ATTACHMENT_BYTES = 50 * 1024
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
    output: TextOutput | AttachmentOutput
    body: ReadResultBody | OpaqueToolBody
    #: File versions this result made visible to the model; only bounded text
    #: pages of real files claim coverage; attachments and URI reads grant nothing.
    coverage: tuple[ReadObservation, ...] = ()


def _uri_failure(tool_name: str, message: str) -> ToolFailure:
    """A model-visible URI failure: the runtime redacts raised errors, so URI branches return text.

    Only resource URI reads use this; workspace file errors keep the file's existing
    raise-and-redact behavior, which is out of scope.
    """
    return ToolFailure(tool_name=tool_name, error=message, output=TextOutput(text=message))


def _text_output(
    content: str,
    *,
    raw_content: str | None = None,
    content_hash: str | None = None,
    next_offset: int | None = None,
    next_byte_offset: int | None = None,
    truncated: bool = False,
    partial: bool = False,
    bounds: OutputBounds | None = None,
) -> TextOutput:
    text = content
    if content_hash is not None:
        text = f"{text}\nSHA-256 content hash: {content_hash}"
    if next_offset is not None:
        text = f"{text}\nNext offset: {next_offset}"
    if next_byte_offset is not None:
        text = f"{text}\nNext byte offset: {next_byte_offset} (zero-based UTF-8 byte offset; continue with byte_offset={next_byte_offset})"
    if raw_content:
        text = f"{text}\n\n{raw_content}"
    if bounds is not None:
        return TextOutput(text, bounds=bounds)
    return TextOutput(text, bounds=OutputBounds(truncated=truncated, partial=partial))


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
    candidate: Path, *, relative_path: str, offset: int, limit: int, services: PluginServices, context: ToolContext
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
    recovery_reference: OutputReference | None = None

    with candidate.open("rb") as source:
        sample = source.read(BINARY_SNIFF_BYTES)
        source.seek(0)
        if attachment or (not is_svg and _is_binary_content(candidate, sample)):
            maximum = MAX_ATTACHMENT_BYTES if attachment else MAX_BINARY_READ_BYTES
            raw = source.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError(f"read file exceeds the maximum supported size ({maximum} bytes): {relative_path}")
            effective_mime = mime or "application/octet-stream"
            content_hash = hashlib.sha256(raw).hexdigest()
            artifact, artifact_id, uri = _spill_artifact(services, kind="content", content=raw, mime=effective_mime, source=relative_path)
            bounds = OutputBounds(reference=OutputReference(uri, artifact), source=relative_path)
            body: dict[str, object] = {
                "path": relative_path,
                "type": "attachment" if attachment else "binary",
                "content_type": effective_mime,
                "byte_count": len(raw),
                "content_hash": content_hash,
                "artifact_id": artifact_id,
            }
            if attachment:
                data_uri = (
                    f"data:{effective_mime};base64,{base64.b64encode(raw).decode('ascii')}"
                    if effective_mime in RASTER_IMAGE_MIMES and len(raw) <= MAX_INLINE_ATTACHMENT_BYTES
                    else None
                )
                message = "Image read successfully"
                body.update(attachment={"mime": effective_mime, "reference": uri}, bounds={"reference": uri})
                return _ReadOutcome(output=AttachmentOutput(effective_mime, data_uri, presentation=message, bounds=bounds), body=OpaqueToolBody(body))
            body.update(source=relative_path, reference=uri)
            presentation = (
                f'Binary file {relative_path} ({effective_mime}, {len(raw)} bytes, sha256:{content_hash}). Read full content with read(path="{uri}").'
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
                    original = source.read(MAX_BINARY_READ_BYTES + 1)
                    if len(original) > MAX_BINARY_READ_BYTES:
                        raise ValueError(f"long-line recovery exceeds the maximum supported size ({MAX_BINARY_READ_BYTES} bytes): {relative_path}")
                    if hashlib.sha256(original).digest() != digest.digest():
                        raise ValueError(f"file changed while preparing long-line recovery; retry read: {relative_path}")
                    artifact, _, uri = _spill_artifact(
                        services, kind="text_source", content=original, mime="text/plain; charset=utf-8", source=relative_path
                    )
                    recovery_reference = OutputReference(uri=uri, artifact=artifact)
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
        output=_text_output(
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
        description="Read a file or a resource URI.",
        input_schema=ReadArgs.model_json_schema(),
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        args = ReadArgs.model_validate(dict(call.arguments))
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(self.definition.name, error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
        if (args.byte_offset is not None or args.byte_limit is not None) and not args.path.startswith(ARTIFACT_URI_PREFIX):
            raise ValueError("byte_offset and byte_limit are only valid for artifact:// URIs")
        if args.byte_limit is not None and args.byte_offset is None:
            raise ValueError("byte_limit requires byte_offset")

        if args.path.startswith(RULE_URI_PREFIX):
            try:
                page = RULE_PAGES.ask(services, RulePageRequest(uri=args.path, offset=args.offset or 1, limit=args.limit or DEFAULT_READ_LIMIT))
            except QueryUnavailable:
                return _uri_failure(self.definition.name, f"rule catalog is unavailable in this runtime generation: {args.path}")
            except QueryContractError:
                return _uri_failure(self.definition.name, f"unreadable rule page for {args.path}: provider shape violated")
            if page is None:
                return _uri_failure(self.definition.name, f"unknown rule in this workspace: {args.path}")
            next_offset = page.next_offset
            truncated = next_offset is not None
            body = page.as_payload()
            body.update(
                {
                    "type": "rule",
                    "truncated": truncated,
                    "partial": truncated,
                    "byte_count": len(page.text.encode("utf-8")),
                    "raw_content": page.text,
                }
            )
            return _read_success(
                _ReadOutcome(
                    output=_text_output(
                        f"Read rule {page.rule} from {args.path}" + ("; output is truncated; continue with next_offset." if truncated else "."),
                        raw_content=page.text,
                        content_hash=page.content_hash,
                        next_offset=next_offset,
                        truncated=truncated,
                        partial=truncated,
                    ),
                    body=OpaqueToolBody(body),
                )
            )

        if args.path.startswith(TOOL_URI_PREFIX):
            outcome = _render_tool_documentation(args.path, context=context, services=services, tool_name_label=self.definition.name)
            return outcome if isinstance(outcome, ToolFailure) else _read_success(outcome)
        if args.path.startswith(ARTIFACT_URI_PREFIX):
            try:
                outcome = _render_artifact(
                    args.path,
                    context=context,
                    services=services,
                    offset=args.offset or 1,
                    limit=args.limit or DEFAULT_READ_LIMIT,
                    byte_offset=args.byte_offset,
                    byte_limit=args.byte_limit,
                )
            except CancelledError:
                return ToolFailure(self.definition.name, error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
            return _read_success(outcome)
        if args.path.startswith(TRANSCRIPT_URI_PREFIX):
            outcome = _render_transcript(
                args.path,
                services=services,
                limit=args.limit or DEFAULT_TRANSCRIPT_LIMIT,
                tool_name_label=self.definition.name,
            )
            return outcome if isinstance(outcome, ToolFailure) else _read_success(outcome)

        if "://" in args.path:
            raise ValueError(f"unsupported resource URI: {args.path}")
        workspace = context.require_workspace()
        workspace_root = workspace.resolve()
        requested_path = Path(args.path)
        candidate = (requested_path if requested_path.is_absolute() else workspace_root / requested_path).resolve()
        is_external = not candidate.is_relative_to(workspace_root)
        relative_path = candidate.as_posix() if is_external else candidate.relative_to(workspace_root).as_posix()
        offset = args.offset or 1
        limit = args.limit or DEFAULT_READ_LIMIT

        if not candidate.exists():
            raise ValueError(f"read target does not exist: {args.path}")
        if not candidate.is_file():
            raise ValueError(f"read only supports regular files: {args.path}")
        outcome = _render_file(candidate, relative_path=relative_path, offset=offset, limit=limit, services=services, context=context)
        return outcome if isinstance(outcome, ToolFailure) else _read_success(outcome)
