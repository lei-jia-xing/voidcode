"""Safe read-only file tool for workspace files and bounded derived views."""

from __future__ import annotations

import base64
import codecs
import hashlib
import heapq
import json
import mimetypes
import os
import re
import stat
import tarfile
import time
import zipfile
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError
from dataclasses import dataclass
from io import TextIOWrapper
from itertools import takewhile
from pathlib import Path
from typing import IO, ClassVar, Literal, cast, final
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

from .read_formats import MAX_OUTPUTS, render_notebook, render_pdf_bytes, render_pdf_path, render_sqlite

_ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_ARTIFACT_PAGE_BYTES = 50 * 1024 - 4096
MAX_INLINE_ATTACHMENT_BYTES = (50 * 1024 - 128) * 3 // 4
MAX_BINARY_READ_BYTES = 50 * 1024 * 1024
RASTER_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})

#: Internal URL scheme for on-demand tool documentation:
#: read(path="voidcode://tool/<name>") renders the admitted definition's
#: description plus its JSON input schema. The core answers from the active
#: revision's dispatch snapshot with this session's visibility applied.
VOIDCODE_TOOL_DOC_PREFIX = "voidcode://tool/"


#: Internal URL scheme for session-scoped artifact reads:
#: read(path="voidcode://artifact/<id>") returns a bounded slice of a
#: spilled tool-output artifact, resolved through the core artifact store
#: (``services.read_artifact``) — never through the external-directory
#: permission path.
VOIDCODE_ARTIFACT_PREFIX = "voidcode://artifact/"

#: Internal URL scheme for lineage-guarded transcript reads:
#: read(path="voidcode://transcript/<session_id>?after_sequence=N&offset=B") returns readable,
#: payload-included pages of the caller's own session or of one of its own descendants.
#: Parents, siblings, unrelated roots, and foreign workspaces are refused by the core.
VOIDCODE_TRANSCRIPT_PREFIX = "voidcode://transcript/"


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
    uri = f"voidcode://artifact/{reference.sha256}"
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
    tool_name = path[len(VOIDCODE_TOOL_DOC_PREFIX) :].strip()
    if not tool_name:
        return _uri_failure(tool_name_label, "voidcode://tool/<name> requires a tool name")
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
    view: Literal["raw", "pdf"] | None = None,
    page: int | None = None,
) -> _ReadOutcome:
    """Render an integrity-checked, bounded line or byte page from an artifact.

    The producer's media-type claim, read back with the first page, classifies
    the read: a raster image renders as an attachment (exactly like the
    workspace binary branch), a PDF claim takes the PDF view, and everything
    else pages as text. An artifact stored without a claim falls back to the
    ``%PDF-`` magic; no other sniffing is done, so image bytes without a claim
    still page as text. Explicit raw mode bypasses PDF extraction; explicit
    byte pagination bypasses both PDF extraction and image attachment rendering.
    """

    artifact_id = path[len(VOIDCODE_ARTIFACT_PREFIX) :].strip()
    if not artifact_id:
        raise ValueError("voidcode://artifact/<id> requires an artifact id")
    if _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
        raise ValueError(f"invalid artifact id: {artifact_id}")

    def _window(byte_start: int, byte_length: int = MAX_ARTIFACT_PAGE_BYTES) -> ArtifactPage:
        if context.abort_signal is not None and context.abort_signal.cancelled:
            raise CancelledError("read cancelled")
        try:
            return services.read_artifact(artifact_id, offset=byte_start, limit=byte_length)
        except (OSError, ValueError, LookupError) as exc:
            raise ValueError(f"read cannot resolve voidcode://artifact URL {path}: {exc}") from exc

    head: ArtifactPage | None = None

    def _page(byte_start: int, byte_length: int = MAX_ARTIFACT_PAGE_BYTES) -> ArtifactPage:
        """First page: the producer's claim plus the bytes a magic fallback needs."""
        nonlocal head
        if byte_start == 0 and byte_length == MAX_ARTIFACT_PAGE_BYTES:
            if head is None:
                head = _window(0, byte_length)
            return head
        return _window(byte_start, byte_length)

    def _full_bytes(label: str) -> tuple[bytes, int, str]:
        """Every byte of the artifact, bounded by MAX_BINARY_READ_BYTES."""
        chunks: list[bytes] = []
        byte_start = 0
        total_bytes = 0
        content_hash = artifact_id
        while True:
            window = _page(byte_start)
            total_bytes = window.total_bytes
            content_hash = window.sha256
            if total_bytes > MAX_BINARY_READ_BYTES:
                raise ValueError(f"{label} exceeds the maximum supported size ({MAX_BINARY_READ_BYTES} bytes): {path}")
            if not window.data:
                return b"".join(chunks), total_bytes, content_hash
            chunks.append(window.data)
            if window.eof:
                return b"".join(chunks), total_bytes, content_hash
            byte_start += len(window.data)

    first_page = _page(0)
    media_type = first_page.media_type

    # An explicit PDF view on a non-PDF artifact still fails below, on the magic.
    if byte_offset is None and media_type in RASTER_IMAGE_MIMES and view != "pdf":
        total_bytes = first_page.total_bytes
        content_hash = first_page.sha256
        artifact: Mapping[str, object] = {"artifact_id": artifact_id, "sha256": content_hash, "byte_count": total_bytes, "uri": path}
        uri = path
        data_uri = None
        if total_bytes <= MAX_INLINE_ATTACHMENT_BYTES:
            raw, _, _ = _full_bytes("Artifact")
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

    if (
        byte_offset is None
        and view != "raw"
        and (view == "pdf" or media_type == "application/pdf" or (not media_type and first_page.data.startswith(b"%PDF-")))
    ):
        source_bytes, total_bytes, content_hash = _full_bytes("PDF artifact")
        # A producer's PDF claim is trusted, but the bytes still have to be a
        # readable PDF; without a claim the magic is the only evidence.
        if not source_bytes.startswith(b"%PDF-"):
            raise ValueError("PDF artifact view requires a readable PDF artifact")
        rendered = render_pdf_bytes(
            source_bytes,
            page=page,
            offset=offset,
            limit=limit,
            cancelled=lambda: context.abort_signal is not None and context.abort_signal.cancelled,
        )
        full_text = cast(str, rendered.pop("full_text"))
        source_artifact: Mapping[str, object] = {
            "artifact_id": artifact_id,
            "sha256": content_hash,
            "byte_count": total_bytes,
            "uri": path,
        }
        source_reference = path
        text_reference: str | None = None
        output_artifact: Mapping[str, object] | None = None
        if rendered["truncated_text"] or len(full_text.encode("utf-8")) > MAX_BYTES:
            spilled = _spill_artifact(services, kind="pdf_text", content=full_text, mime="text/plain; charset=utf-8", source=path)
            output_artifact, text_reference = spilled[0], spilled[2]
        rendered.update(source_reference=source_reference, source_artifact=source_artifact)
        bounds_reference = (
            OutputReference(uri=cast(str, text_reference), artifact=output_artifact)
            if output_artifact is not None
            else OutputReference(uri=path, artifact=source_artifact)
        )
        bounds = OutputBounds(
            truncated=bool(rendered["truncated"]),
            partial=bool(rendered["partial"]),
            reference=bounds_reference,
            source=path,
        )
        body = {
            **rendered,
            "path": path,
            "type": "pdf",
            "source_reference": source_reference,
            "source_artifact": source_artifact,
            "text_reference": text_reference,
            "output_artifact": output_artifact,
        }
        if text_reference is not None:
            summary = f"Extracted PDF text from {path}; source PDF: {source_reference}; full extracted text: {text_reference}."
        else:
            summary = f"Extracted PDF text from {path}; source PDF: {source_reference}."
        if rendered["truncated"]:
            summary += (
                f" Continue with offset={rendered['next_offset']}." if rendered["next_offset"] is not None else " The current text page is truncated."
            )
        else:
            summary += " The extracted text is below."
        return _ReadOutcome(
            output=_text_output(
                summary,
                raw_content=cast(str, rendered["raw_content"]),
                next_offset=cast(int | None, rendered["next_offset"]),
                truncated=bool(rendered["truncated"]),
                partial=bool(rendered["partial"]),
                bounds=bounds,
            ),
            body=OpaqueToolBody(body),
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
    raw_target = path[len(VOIDCODE_TRANSCRIPT_PREFIX) :].strip()
    session_id, _, query = raw_target.partition("?")
    session_id = session_id.strip()
    if not session_id:
        return _uri_failure(tool_name_label, "voidcode://transcript/<session_id> requires a session id")
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
        next_uri = f"{VOIDCODE_TRANSCRIPT_PREFIX}{session_id}?after_sequence={page.next_after_sequence}&offset={page.next_offset}"
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

    path: str = Field(
        description=(
            "Internal URLs: voidcode://tool/<name> reads a tool's guidance and input schema; "
            "voidcode://rule/<name> reads a bounded workspace rule catalog entry; "
            "voidcode://artifact/<id> reads a bounded slice of the current session's spilled tool-output artifact; "
            "voidcode://transcript/<session_id> reads a bounded transcript with prompts and tool results "
            "of the current session or of a child session it spawned."
        )
    )
    offset: int | None = Field(
        default=None,
        ge=1,
        description="1-based starting file/member line, archive entry, SQLite row, notebook cell, or PDF text line.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=2000,
        description="Maximum file/member lines, archive entries, SQLite rows, notebook cells, or PDF text lines to return.",
    )
    byte_offset: int | None = Field(
        default=None, ge=0, description="Zero-based UTF-8 byte offset for continuing a long artifact line; use the returned next_byte_offset."
    )
    byte_limit: int | None = Field(
        default=None, ge=4, le=MAX_ARTIFACT_PAGE_BYTES, description="Maximum UTF-8 bytes in an artifact page; requires byte_offset."
    )
    view: Literal["raw", "sqlite", "notebook", "pdf"] | None = Field(
        default=None,
        description="Select a read-only derived view; raw returns the original file bytes/text and preserves ordinary file-read semantics.",
    )
    table: str | None = Field(default=None, description="SQLite table to inspect; defaults to the first user table.")
    query: str | None = Field(default=None, description="One read-only SQLite SELECT/WITH query.")
    cell: int | None = Field(default=None, ge=0, description="Zero-based notebook cell index to inspect alone.")
    page: int | None = Field(default=None, ge=1, description="One-based PDF page to extract.")
    output_offset: int | None = Field(
        default=None, ge=1, description="1-based notebook output index within `cell`; requires the zero-based cell selector."
    )
    output_limit: int | None = Field(
        default=None, ge=1, le=MAX_OUTPUTS, description="Maximum notebook outputs to return for `cell`; continue with next_output_offset."
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

    @classmethod
    def from_payload(cls, value: Mapping[str, object]) -> ReadResultBody:
        fields = {
            "type",
            "line_count",
            "offset",
            "limit",
            "next_offset",
            "truncated",
            "partial",
            "byte_count",
            "content_hash",
            "lines",
            "raw_content",
        }
        if set(value) != fields or value.get("type") != "file":
            raise ValueError("persisted read body has an unsupported file shape")

        def integer(key: str, *, minimum: int) -> int:
            item = value[key]
            if not isinstance(item, int) or isinstance(item, bool) or item < minimum:
                raise ValueError(f"persisted read body {key} must be an integer >= {minimum}")
            return item

        content_hash = value["content_hash"]
        raw_content = value["raw_content"]
        raw_lines = value["lines"]
        truncated = value["truncated"]
        partial = value["partial"]
        next_offset = value["next_offset"]
        if not isinstance(content_hash, str) or len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash):
            raise ValueError("persisted read body content hash must be lowercase SHA-256")
        if not isinstance(raw_content, str) or not isinstance(raw_lines, list):
            raise ValueError("persisted read body content and lines have invalid types")
        if not isinstance(truncated, bool) or not isinstance(partial, bool):
            raise ValueError("persisted read body bounds flags must be booleans")
        if next_offset is not None and (not isinstance(next_offset, int) or isinstance(next_offset, bool) or next_offset < 1):
            raise ValueError("persisted read body next offset must be a positive integer or null")

        line_count = integer("line_count", minimum=0)
        offset = integer("offset", minimum=1)
        limit = integer("limit", minimum=1)
        byte_count = integer("byte_count", minimum=0)
        lines: list[ReadLine] = []
        for index, raw_line in enumerate(raw_lines):
            if not isinstance(raw_line, Mapping) or set(raw_line) != {"line", "text", "truncated"}:
                raise ValueError("persisted read body line has an invalid shape")
            line, text = raw_line["line"], raw_line["text"]
            if not isinstance(line, int) or isinstance(line, bool) or line != offset + index or not isinstance(text, str):
                raise ValueError("persisted read body lines must be contiguous numbered text")
            line_truncated = raw_line["truncated"]
            if not isinstance(line_truncated, bool):
                raise ValueError("persisted read body line truncation must be a boolean")
            lines.append(ReadLine(line, text, line_truncated))
        if offset > max(1, line_count):
            raise ValueError("persisted read body offset is outside the file")
        if partial != truncated:
            raise ValueError("persisted read body partial flag disagrees with truncation")
        if len(lines) > limit or offset + len(lines) - 1 > line_count:
            raise ValueError("persisted read body lines exceed the declared file page")
        if next_offset is not None and next_offset != offset + len(lines):
            raise ValueError("persisted read body next offset does not follow its lines")
        normalized_content = raw_content.replace("\r\n", "\n").replace("\r", "\n")
        normalized_lines = normalized_content.split("\n") if normalized_content else []
        if normalized_content.endswith("\n"):
            normalized_lines.pop()
        if normalized_lines != [line.text for line in lines]:
            raise ValueError("persisted read body raw content disagrees with its lines")
        if byte_count != len(raw_content.encode("utf-8")):
            raise ValueError("persisted read body byte count disagrees with its content")
        return cls(line_count, offset, limit, next_offset, truncated, partial, byte_count, content_hash, tuple(lines), raw_content)


@dataclass(frozen=True, slots=True)
class _ReadOutcome:
    output: TextOutput | AttachmentOutput
    body: ReadResultBody | OpaqueToolBody
    #: File versions this result made visible to the model; only bounded text
    #: pages of real files claim coverage (attachments, views, listings and
    #: URI reads grant nothing).
    coverage: tuple[ReadObservation, ...] = ()


def _uri_failure(tool_name: str, message: str) -> ToolFailure:
    """A model-visible URI failure: the runtime redacts raised errors, so URI branches return text.

    Only the `voidcode://` derived views use this; workspace file errors keep the file's existing
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
    attachment = not is_svg and (mime in RASTER_IMAGE_MIMES or mime == "application/pdf")
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
                if effective_mime == "application/pdf":
                    message = (
                        f"PDF {relative_path} ({effective_mime}, {len(raw)} bytes, sha256:{content_hash}); "
                        f'read the original PDF with read(path="{uri}", view="raw").'
                    )
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


#: Directory-listing shape: depth and per-directory entry caps, matching the
#: reference read tool's tree rendering (sorted by recency, with a truncation
#: notice when either cap bites).
DIRECTORY_MAX_DEPTH = 2
DIRECTORY_PER_DIR_LIMIT = 12
DIRECTORY_ENTRY_LIMIT = 200

#: Archive families the stdlib can decode in-process. `.tar.gz`/`.tgz` etc. are
#: part of the tar family; a bare `.gz` is a single compressed stream (not an
#: archive) and is deliberately unsupported.
_ZIP_SUFFIXES = (".zip", ".jar", ".whl", ".apk", ".vsix", ".nupkg", ".cbz")
_TAR_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".tar")
#: Archive-ish extensions that would need third-party codecs. Reported as an
#: explicit unsupported notice instead of a confusing "does not exist" error.
UNSUPPORTED_ARCHIVE_SUFFIXES = (".rar", ".7z", ".iso", ".cab", ".deb", ".rpm", ".lzh", ".arj", ".asar", ".gz", ".bz2", ".xz")

#: Bounded archive surfaces: an immediate-child listing cap and a cap on the
#: decoded size of a single member (decompression-bomb guard).
ARCHIVE_LIST_LIMIT = 200
MAX_ARCHIVE_MEMBER_BYTES = 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000


@dataclass(frozen=True, slots=True)
class _TreeNode:
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    expanded: bool = False
    omitted: int = 0
    children: tuple[_TreeNode, ...] = ()


#: Directory names pruned from a directory listing: they are almost never the
#: target of a tree read and would otherwise eat the whole entry budget.
_TREE_IGNORE_DIRS = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".ruff_cache", ".pytest_cache"})


def _human_size(byte_count: int) -> str:
    if byte_count < 1024:
        return f"{byte_count} B"
    if byte_count < 1024 * 1024:
        return f"{byte_count / 1024:.1f} KB"
    return f"{byte_count / (1024 * 1024):.1f} MB"


def _relative_age(mtime: float) -> str:
    seconds = max(0.0, time.time() - mtime)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)}d ago"
    if seconds < 86400 * 365:
        return f"{int(seconds // (86400 * 30))}mo ago"
    return f"{int(seconds // (86400 * 365))}y ago"


def _tree_entries(directory: Path, limit: int, *, cancelled: Callable[[], bool]) -> tuple[list[tuple[Path, os.stat_result]], int]:
    count = 0

    def observe(entry: os.DirEntry[str]) -> tuple[Path, os.stat_result]:
        nonlocal count
        info = entry.stat(follow_symlinks=False)
        count += 1
        return Path(entry.path), info

    with os.scandir(directory) as entries:
        selected = heapq.nsmallest(
            limit,
            (observe(entry) for entry in takewhile(lambda _: not cancelled(), entries) if entry.name not in _TREE_IGNORE_DIRS),
            key=lambda item: (-item[1].st_mtime, item[0].name),
        )
    return selected, count


def _materialize_tree(root: Path, *, max_depth: int, per_dir_limit: int, entry_limit: int, cancelled: Callable[[], bool]) -> tuple[_TreeNode, bool]:
    """Build a bounded recency-sorted tree; failed reads are never empty directories."""
    budget = entry_limit
    truncated = False

    def walk(directory: Path, depth: int, mtime: float = 0.0) -> _TreeNode:
        nonlocal budget, truncated
        if budget <= 0 or cancelled():
            truncated = True
            return _TreeNode(directory.name, True, mtime=mtime)
        entries, count = _tree_entries(directory, min(per_dir_limit, budget), cancelled=cancelled)
        children: list[_TreeNode] = []
        for entry, info in entries:
            if budget <= 0:
                break
            budget -= 1
            is_dir = stat.S_ISDIR(info.st_mode)
            if is_dir and depth < max_depth:
                child = walk(entry, depth + 1, info.st_mtime)
            else:
                child = _TreeNode(entry.name, is_dir, size=0 if is_dir else info.st_size, mtime=info.st_mtime)
                truncated |= is_dir  # This directory's contents were not inspected.
            children.append(child)
        omitted = count - len(children)
        truncated |= omitted > 0
        return _TreeNode(directory.name, True, mtime=mtime, expanded=True, omitted=omitted, children=tuple(children))

    return walk(root, 0), truncated


def _render_directory(root: Path, *, label: str, context: ToolContext) -> _ReadOutcome | ToolFailure:
    def cancelled() -> bool:
        return context.abort_signal is not None and context.abort_signal.cancelled

    tree, truncated = _materialize_tree(
        root,
        max_depth=DIRECTORY_MAX_DEPTH,
        per_dir_limit=DIRECTORY_PER_DIR_LIMIT,
        entry_limit=DIRECTORY_ENTRY_LIMIT,
        cancelled=cancelled,
    )
    if cancelled():
        return ToolFailure("read", error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
    lines = [f"{label}/"]
    entry_count = 0

    def _emit(node: _TreeNode, prefix: str) -> None:
        nonlocal entry_count
        children = node.children
        for index, child in enumerate(children):
            last = index == len(children) - 1
            connector = "└── " if last else "├── "
            is_dir = child.is_dir
            name = child.name
            mtime = child.mtime
            if is_dir:
                lines.append(f"{prefix}{connector}{name}/  ({_relative_age(mtime)})")
            else:
                lines.append(f"{prefix}{connector}{name} ({_human_size(child.size)}, {_relative_age(mtime)})")
            entry_count += 1
            if is_dir:
                nested_prefix = f"{prefix}{'    ' if last else '│   '}"
                if child.expanded and not child.children:
                    lines.append(f"{nested_prefix}└── (empty directory)")
                _emit(child, nested_prefix)
            omitted = child.omitted
            if omitted:
                lines.append(f"{prefix}{'    ' if last else '│   '}… {omitted} more")

    _emit(tree, "")
    if tree.omitted:
        lines.append(f"… {tree.omitted} more")
    if truncated:
        lines.append("… listing is incomplete; read a subdirectory to see omitted or unexpanded entries.")
    rendered = "\n".join(lines)
    content = f"Listed {entry_count} entr{'y' if entry_count == 1 else 'ies'} in {label}"
    content += "; listing is truncated." if truncated else "."
    return _ReadOutcome(
        output=_text_output(content, raw_content=rendered, truncated=truncated, partial=truncated),
        body=OpaqueToolBody(
            {
                "path": label,
                "type": "directory",
                "entry_count": entry_count,
                "truncated": truncated,
                "partial": truncated,
                "raw_content": rendered,
            }
        ),
    )


def _normalize_member_path(raw: str) -> str:
    parts = [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"archive member path must not contain '..': {raw}")
    return "/".join(parts)


def _split_archive_path(path: str) -> tuple[str, str, str] | None:
    """Split ``archive.ext[:inner]`` into (archive path, raw inner, family)."""
    lowered = path.lower()
    best: tuple[int, str, str] | None = None
    for family, suffixes in (("zip", _ZIP_SUFFIXES), ("tar", _TAR_SUFFIXES)):
        for suffix in suffixes:
            match = re.search(re.escape(suffix) + r"(?=:|$)", lowered)
            if match is None:
                continue
            index = match.start()
            if best is None or index < best[0] or (index == best[0] and len(suffix) > len(best[1])):
                best = (index, suffix, family)
    if best is None:
        return None
    index, suffix, family = best
    inner = path[index + len(suffix) :]
    return path[: index + len(suffix)], inner[1:] if inner.startswith(":") else inner, family


def _zip_members(archive: zipfile.ZipFile, *, cancelled: Callable[[], bool]) -> list[tuple[str, int, bool]]:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ValueError(f"archive exceeds the maximum supported member count ({MAX_ARCHIVE_MEMBERS})")
    result: list[tuple[str, int, bool]] = []
    for info in members:
        if cancelled():
            raise CancelledError("read cancelled")
        result.append((_normalize_member_path(info.filename), info.file_size, info.is_dir()))
    return result


def _tar_members(archive: tarfile.TarFile, *, cancelled: Callable[[], bool]) -> list[tuple[str, int, bool]]:
    result: list[tuple[str, int, bool]] = []
    for member in archive:
        if cancelled():
            raise CancelledError("read cancelled")
        if len(result) >= MAX_ARCHIVE_MEMBERS:
            raise ValueError(f"archive exceeds the maximum supported member count ({MAX_ARCHIVE_MEMBERS})")
        result.append((_normalize_member_path(member.name), member.size, member.isdir()))
    return result


def _archive_member_bytes(handle: IO[bytes], normalized: str) -> bytes:
    with handle:
        raw = handle.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
    if len(raw) > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError(f"archive member exceeds the maximum supported size ({MAX_ARCHIVE_MEMBER_BYTES} bytes): {normalized}")
    return raw


def _zip_member_bytes(archive: zipfile.ZipFile, normalized: str) -> bytes:
    for info in reversed(archive.infolist()):
        if info.is_dir() or _normalize_member_path(info.filename) != normalized:
            continue
        return _archive_member_bytes(archive.open(info), normalized)
    raise ValueError(f"archive member not found: {normalized}")


def _tar_member_bytes(archive: tarfile.TarFile, normalized: str) -> bytes:
    for member in reversed(archive.getmembers()):
        if _normalize_member_path(member.name) != normalized:
            continue
        if not member.isfile():
            raise ValueError(f"archive member is not a regular file: {normalized}")
        handle = archive.extractfile(member)
        if handle is None:
            raise ValueError(f"archive member cannot be read: {normalized}")
        return _archive_member_bytes(handle, normalized)
    raise ValueError(f"archive member not found: {normalized}")


def _archive_children(members: list[tuple[str, int, bool]], prefix: str) -> list[tuple[str, int, bool]]:
    children: dict[str, tuple[int, bool]] = {}
    for name, size, is_dir in members:
        if not name:
            continue
        if prefix:
            if name == prefix:
                continue
            if not name.startswith(f"{prefix}/"):
                continue
            rest = name[len(prefix) + 1 :]
        else:
            rest = name
        head, _, tail = rest.partition("/")
        if not head:
            continue
        child = (0, True) if tail else (size, is_dir)
        previous = children.get(head)
        if previous is not None and previous[1] != child[1]:
            raise ValueError(f"archive path is both a file and directory: {prefix + '/' if prefix else ''}{head}")
        children[head] = child
    return sorted((name, size, is_dir) for name, (size, is_dir) in children.items())


def _render_archive_lines(text: str, *, label: str, offset: int, limit: int, services: PluginServices) -> _ReadOutcome:
    limit = min(limit, DEFAULT_READ_LIMIT)
    source_lines = text.splitlines()
    total_lines = len(source_lines)
    if total_lines < offset and not (total_lines == 0 and offset == 1):
        raise ValueError(f"Offset {offset} is out of range for this member ({total_lines} lines)")
    rendered_lines: list[str] = []
    bytes_used = 0
    content_truncated = False
    lines_clipped = False
    has_more = False
    for line_index in range(offset - 1, total_lines):
        if len(rendered_lines) >= limit:
            has_more = True
            break
        line_text, line_truncated = _truncate_line(source_lines[line_index])
        encoded_size = len(line_text.encode("utf-8")) + (1 if rendered_lines else 0)
        if bytes_used + encoded_size > MAX_BYTES:
            content_truncated = True
            has_more = True
            break
        rendered_lines.append(line_text)
        bytes_used += encoded_size
        lines_clipped |= line_truncated
        content_truncated = content_truncated or line_truncated
    next_offset = offset + len(rendered_lines)
    content_truncated = content_truncated or has_more
    raw_content = "\n".join(rendered_lines)
    summary = f"Read {len(rendered_lines)} line(s) from {label}" + ("; output is truncated." if content_truncated else ".")
    reference: OutputReference | None = None
    if lines_clipped:
        artifact, _, uri = _spill_artifact(services, kind="archive_text", content=text, mime="text/plain; charset=utf-8", source=label)
        reference = OutputReference(uri, artifact)
        summary += f" Full member text: read(path={json.dumps(uri)})."
    return _ReadOutcome(
        output=_text_output(
            summary,
            raw_content=raw_content,
            next_offset=next_offset if has_more else None,
            bounds=OutputBounds(truncated=content_truncated, partial=content_truncated, reference=reference, source=label),
        ),
        body=OpaqueToolBody(
            {
                "path": label,
                "type": "archive",
                "line_count": total_lines,
                "offset": offset,
                "limit": limit,
                "next_offset": next_offset if has_more else None,
                "truncated": content_truncated,
                "partial": content_truncated,
                "byte_count": bytes_used,
                "lines": [{"line": offset + index, "text": line} for index, line in enumerate(rendered_lines)],
                "raw_content": raw_content,
            }
        ),
    )


def _render_archive(
    archive_path: Path, inner: str, *, family: str, label: str, offset: int, limit: int, services: PluginServices, cancelled: Callable[[], bool]
) -> _ReadOutcome:
    if family == "zip":
        try:
            with zipfile.ZipFile(archive_path) as zip_archive:
                return _render_archive_members(
                    _zip_members(zip_archive, cancelled=cancelled),
                    reader=lambda normalized: _zip_member_bytes(zip_archive, normalized),
                    inner=inner,
                    label=label,
                    offset=offset,
                    limit=limit,
                    services=services,
                )
        except zipfile.BadZipFile as exc:
            raise ValueError(f"not a valid zip archive: {label}") from exc
    try:
        with tarfile.TarFile.open(archive_path) as tar_archive:
            return _render_archive_members(
                _tar_members(tar_archive, cancelled=cancelled),
                reader=lambda normalized: _tar_member_bytes(tar_archive, normalized),
                inner=inner,
                label=label,
                offset=offset,
                limit=limit,
                services=services,
            )
    except tarfile.TarError as exc:
        raise ValueError(f"not a valid tar archive: {label}") from exc


def _render_archive_members(
    members: list[tuple[str, int, bool]],
    *,
    reader: Callable[[str], bytes],
    inner: str,
    label: str,
    offset: int,
    limit: int,
    services: PluginServices,
) -> _ReadOutcome:
    normalized_inner = _normalize_member_path(inner)
    names = {name for name, _, _ in members}
    is_directory = (
        not normalized_inner
        or any(name.startswith(f"{normalized_inner}/") for name in names)
        or any(name == normalized_inner and dir_flag for name, _, dir_flag in members)
    )
    if is_directory:
        if any(name == normalized_inner and not dir_flag for name, _, dir_flag in members):
            raise ValueError(f"archive path is both a file and directory: {normalized_inner}")
        children = _archive_children(members, normalized_inner)
        if offset > max(1, len(children)):
            raise ValueError(f"Offset {offset} is out of range for this archive directory ({len(children)} entries)")
        page_limit = min(limit, ARCHIVE_LIST_LIMIT)
        header = f"{label}:" if not normalized_inner else f"{label}/"
        continuation = f"\n… {len(children)} more; continue with offset={len(children) + 1}."
        budget = MAX_BYTES - len(header.encode("utf-8")) - len(continuation.encode("utf-8"))
        if budget < 0:
            raise ValueError("archive directory path exceeds the read output byte limit")
        lines = [header]
        entry_count = 0
        for name, size, dir_flag in children[offset - 1 : offset - 1 + page_limit]:
            line = f"{name}/" if dir_flag else f"{name} ({_human_size(size)})"
            byte_count = len(line.encode("utf-8")) + 1
            if byte_count > budget:
                if entry_count == 0:
                    raise ValueError("archive member name exceeds the read output byte limit")
                break
            lines.append(line)
            budget -= byte_count
            entry_count += 1
        next_offset = offset + entry_count if offset - 1 + entry_count < len(children) else None
        truncated = next_offset is not None
        if truncated:
            lines.append(f"… {len(children) - offset + 1 - entry_count} more; continue with offset={next_offset}.")
        rendered = "\n".join(lines)
        return _ReadOutcome(
            output=_text_output(
                f"Listed {entry_count} entr{'y' if entry_count == 1 else 'ies'} in {label}" + ("; listing is truncated." if truncated else "."),
                raw_content=rendered,
                next_offset=next_offset,
                truncated=truncated,
                partial=truncated,
            ),
            body=OpaqueToolBody(
                {
                    "path": label,
                    "type": "archive_listing",
                    "entry_count": entry_count,
                    "offset": offset,
                    "limit": page_limit,
                    "next_offset": next_offset,
                    "truncated": truncated,
                    "partial": truncated,
                    "raw_content": rendered,
                }
            ),
        )

    if normalized_inner not in names:
        raise ValueError(f"archive member not found: {normalized_inner}")
    raw = reader(normalized_inner)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _ReadOutcome(
            output=TextOutput(f"{label} is not UTF-8 text ({len(raw)} bytes); binary archive members are not decoded."),
            body=OpaqueToolBody(
                {
                    "path": label,
                    "type": "archive_binary",
                    "byte_count": len(raw),
                    "truncated": False,
                    "partial": False,
                }
            ),
        )
    return _render_archive_lines(text, label=label, offset=offset, limit=limit, services=services)


def _read_success(outcome: _ReadOutcome) -> ToolSuccess[ReadResultBody | OpaqueToolBody]:
    return ToolSuccess("read", output=outcome.output, body=outcome.body, coverage=outcome.coverage)


def _render_format_view(
    candidate: Path,
    *,
    label: str,
    context: ToolContext,
    services: PluginServices,
    view: Literal["sqlite", "notebook", "pdf"],
    offset: int,
    limit: int,
    table: str | None,
    query: str | None,
    cell: int | None,
    page: int | None,
    output_offset: int,
    output_limit: int,
) -> _ReadOutcome:
    if view == "sqlite":
        payload = render_sqlite(
            candidate,
            table=table,
            query=query,
            offset=offset,
            limit=limit,
            cancelled=lambda: context.abort_signal is not None and context.abort_signal.cancelled,
        )
    elif view == "notebook":
        payload = render_notebook(
            candidate,
            offset=offset,
            limit=limit,
            cell=cell,
            output_offset=output_offset,
            output_limit=output_limit,
        )
    else:
        payload = render_pdf_path(
            candidate,
            page=page,
            offset=offset,
            limit=limit,
            cancelled=lambda: context.abort_signal is not None and context.abort_signal.cancelled,
        )
    full_text = payload.pop("full_text", None)
    source_bytes = payload.pop("source_bytes", None)
    artifacts: list[Mapping[str, object]] = []
    if isinstance(full_text, str) and (payload.get("truncated_text") is True or len(full_text.encode("utf-8")) > MAX_BYTES):
        spilled = _spill_artifact(services, kind="pdf_text", content=full_text, mime="text/plain; charset=utf-8", source=label)
        payload["text_reference"] = spilled[2]
        payload["output_artifact"] = spilled[0]
    payload["source"] = label

    images_value = payload.pop("images", [])
    if not isinstance(images_value, list):
        raise ValueError("invalid notebook image collection")
    for image in images_value:
        if not isinstance(image, dict):
            raise ValueError("invalid notebook image record")
        image_bytes = image.pop("data", None)
        if not isinstance(image_bytes, bytes):
            raise ValueError("notebook image data is unavailable")
        spilled = _spill_artifact(
            services,
            kind="notebook_image",
            content=image_bytes,
            mime=str(image["mime"]),
            source=f"{label}#cell={image['cell']}",
        )
        image["reference"] = spilled[2]
        image["artifact"] = spilled[0]
        artifacts.append(spilled[0])
    if view == "pdf" or payload.get("truncated_text") is True or payload.get("raw_reference_required") is True:
        if not isinstance(source_bytes, bytes):
            raise ValueError(f"{view} source snapshot is unavailable")
        spilled = _spill_artifact(
            services,
            kind="pdf_source" if view == "pdf" else "notebook_source",
            content=source_bytes,
            mime="application/pdf" if view == "pdf" else "application/json",
            source=label,
        )
        payload["source_reference"] = spilled[2]
        payload["source_artifact"] = spilled[0]
    if artifacts:
        payload["artifacts"] = artifacts

    raw_content = json.dumps(payload, ensure_ascii=False, indent=2)
    output_reference: str | None = None
    bounds_reference: OutputReference | None = None
    raw_bytes = raw_content.encode("utf-8")
    output_truncated = len(raw_bytes) > MAX_BYTES
    if output_truncated:
        spilled = _spill_artifact(services, kind=f"{view}_view", content=raw_content, mime="application/json", source=label)
        output_reference = spilled[2]
        payload["output_reference"] = output_reference
        payload["output_artifact"] = spilled[0]
        bounds_reference = OutputReference(uri=output_reference, artifact=spilled[0])
        raw_content = raw_bytes[: MAX_BYTES // 2].decode("utf-8", "ignore") + "\n…"
    truncated = bool(payload.get("truncated", False)) or output_truncated
    if output_truncated:
        payload["output_truncated"] = True
    next_offset = payload.get("next_offset")
    source_hash = payload.get("source_sha256")
    return _ReadOutcome(
        output=_text_output(
            f"Read {view} view of {label}"
            + ("; output is truncated." if truncated else ".")
            + (f" Full derived view: read(path={json.dumps(output_reference)})." if output_reference else ""),
            raw_content=raw_content,
            content_hash=source_hash if isinstance(source_hash, str) else None,
            next_offset=next_offset if isinstance(next_offset, int) else None,
            truncated=truncated,
            partial=truncated,
            bounds=OutputBounds(truncated=truncated, partial=truncated, source=label, reference=bounds_reference),
        ),
        body=OpaqueToolBody(payload),
    )


@final
class ReadTool:
    """Read a file or supported attachment from the current workspace."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="read",
        description="Read a file inside the current workspace.",
        input_schema=ReadArgs.model_json_schema(),
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        args = ReadArgs.model_validate(dict(call.arguments))
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(self.definition.name, error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
        if args.path.startswith((RULE_URI_PREFIX, VOIDCODE_TOOL_DOC_PREFIX, VOIDCODE_TRANSCRIPT_PREFIX)) and (
            args.view is not None
            or args.table is not None
            or args.query is not None
            or args.cell is not None
            or args.page is not None
            or args.output_offset is not None
            or args.output_limit is not None
        ):
            raise ValueError("format views and selectors only apply to workspace files and PDF artifact URLs")
        if (args.byte_offset is not None or args.byte_limit is not None) and not args.path.startswith(VOIDCODE_ARTIFACT_PREFIX):
            raise ValueError("byte_offset and byte_limit are only valid for voidcode://artifact URLs")
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

        if args.path.startswith(VOIDCODE_TOOL_DOC_PREFIX):
            outcome = _render_tool_documentation(args.path, context=context, services=services, tool_name_label=self.definition.name)
            return outcome if isinstance(outcome, ToolFailure) else _read_success(outcome)
        if args.path.startswith(VOIDCODE_ARTIFACT_PREFIX):
            if args.view not in (None, "raw", "pdf") or any(
                value is not None for value in (args.table, args.query, args.cell, args.output_offset, args.output_limit)
            ):
                raise ValueError("artifact URLs only support raw or PDF views, without SQLite or notebook selectors")
            if args.page is not None and args.view != "pdf":
                raise ValueError("page requires an explicit PDF artifact view")
            if args.view == "pdf" and args.byte_offset is not None:
                raise ValueError("PDF artifact view does not accept byte arguments")
            try:
                outcome = _render_artifact(
                    args.path,
                    context=context,
                    services=services,
                    offset=args.offset or 1,
                    limit=args.limit or DEFAULT_READ_LIMIT,
                    byte_offset=args.byte_offset,
                    byte_limit=args.byte_limit,
                    view=args.view,
                    page=args.page,
                )
            except CancelledError:
                return ToolFailure(self.definition.name, error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
            return _read_success(outcome)
        if args.path.startswith(VOIDCODE_TRANSCRIPT_PREFIX):
            outcome = _render_transcript(
                args.path,
                services=services,
                limit=args.limit or DEFAULT_TRANSCRIPT_LIMIT,
                tool_name_label=self.definition.name,
            )
            return outcome if isinstance(outcome, ToolFailure) else _read_success(outcome)

        workspace = context.require_workspace()
        workspace_root = workspace.resolve()
        requested_path = Path(args.path)
        candidate = (requested_path if requested_path.is_absolute() else workspace_root / requested_path).resolve()
        is_external = not candidate.is_relative_to(workspace_root)
        relative_path = candidate.as_posix() if is_external else candidate.relative_to(workspace_root).as_posix()
        offset = args.offset or 1
        limit = args.limit or DEFAULT_READ_LIMIT

        format_selectors = (args.table, args.query, args.cell, args.page, args.output_offset, args.output_limit)
        archive = _split_archive_path(args.path) if args.view != "raw" else None
        if archive is not None:
            if args.view is not None or any(value is not None for value in format_selectors):
                raise ValueError("archive views do not accept SQLite, notebook, or PDF selectors")
            archive_path, inner, family = archive
            requested_archive = Path(archive_path)
            resolved_archive = (requested_archive if requested_archive.is_absolute() else workspace_root / requested_archive).resolve()
            if not resolved_archive.is_file():
                raise ValueError(f"read target does not exist: {archive_path}")
            try:
                outcome = _render_archive(
                    resolved_archive,
                    inner,
                    family=family,
                    label=args.path,
                    offset=offset,
                    limit=limit,
                    services=services,
                    cancelled=lambda: context.abort_signal is not None and context.abort_signal.cancelled,
                )
            except CancelledError:
                return ToolFailure(self.definition.name, error="read cancelled", execution=ConfirmedStop(cancellation_signalled=True))
            return _read_success(outcome)

        if args.view != "raw" and args.path.lower().endswith(UNSUPPORTED_ARCHIVE_SUFFIXES):
            suffix = Path(args.path).suffix.lower() or args.path
            raise ValueError(
                f"read does not support {suffix} archives (third-party codec required); "
                f"supported archive families are zip ({', '.join(_ZIP_SUFFIXES)}) and tar ({', '.join(_TAR_SUFFIXES)})."
            )
        if not candidate.exists():
            raise ValueError(f"read target does not exist: {args.path}")
        if candidate.is_dir():
            if args.view not in (None, "raw") or any(value is not None for value in format_selectors):
                raise ValueError("directories do not accept format views or selectors")
            if args.offset is not None or args.limit is not None:
                raise ValueError("directory trees do not support offset or limit")
            outcome = _render_directory(candidate, label=relative_path, context=context)
            return outcome if isinstance(outcome, ToolFailure) else _read_success(outcome)
        if not candidate.is_file():
            raise ValueError(f"read only supports regular files: {args.path}")
        with candidate.open("rb") as source:
            magic = source.read(16)
        detected: Literal["sqlite", "notebook", "pdf"] | None = None
        if candidate.suffix.lower() == ".ipynb":
            detected = "notebook"
        elif candidate.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or magic == b"SQLite format 3\x00":
            detected = "sqlite"
        elif candidate.suffix.lower() == ".pdf" or magic.startswith(b"%PDF-"):
            detected = "pdf"
        selected_view = args.view if args.view not in (None, "raw") else (detected if args.view is None else None)
        if args.table is not None or args.query is not None:
            if selected_view != "sqlite":
                raise ValueError("table and query are only valid for SQLite views")
        if args.cell is not None and selected_view != "notebook":
            raise ValueError("cell is only valid for notebook views")
        if args.page is not None and selected_view != "pdf":
            raise ValueError("page is only valid for PDF views")
        if args.output_offset is not None or args.output_limit is not None:
            if selected_view != "notebook" or args.cell is None:
                raise ValueError("output_offset and output_limit require a selected notebook cell")
        if selected_view is not None:
            return _read_success(
                _render_format_view(
                    candidate,
                    label=relative_path,
                    context=context,
                    services=services,
                    view=selected_view,
                    output_offset=args.output_offset or 1,
                    output_limit=min(args.output_limit or MAX_OUTPUTS, MAX_OUTPUTS),
                    offset=offset,
                    limit=limit,
                    table=args.table,
                    query=args.query,
                    cell=args.cell,
                    page=args.page,
                )
            )

        outcome = _render_file(candidate, relative_path=relative_path, offset=offset, limit=limit, services=services, context=context)
        return outcome if isinstance(outcome, ToolFailure) else _read_success(outcome)
