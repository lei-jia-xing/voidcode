"""Safe read-only file tool for workspace files and bounded derived views."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import tarfile
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, cast, final
from urllib.parse import parse_qs

from pydantic import BaseModel, StrictInt, field_validator

from voidcode.sdk.history import render_history_page
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import PluginServices, QueryUnavailable
from voidcode.sdk.tools import (
    AttachmentOutput,
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

from .args import parse_tool_args, validate_non_empty
from .artifacts import (
    ARTIFACT_ID_PATTERN as _ARTIFACT_ID_PATTERN,
)
from .artifacts import (
    MAX_ARTIFACT_PAGE_BYTES,
    MAX_BINARY_READ_BYTES,
    MAX_INLINE_ATTACHMENT_BYTES,
    RASTER_IMAGE_MIMES,
    artifact_uri,
    create_artifact,
    read_artifact_page,
)
from .read_formats import MAX_OUTPUTS, render_notebook, render_pdf_bytes, render_pdf_path, render_sqlite

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

#: Internal URL scheme for workspace rule catalogs, shared verbatim with the
#: skills plugin's catalog URIs: ``services.query("workspace.rule_page")`` asks
#: the skills plugin, which owns the rulebook semantics.
RULE_URI_PREFIX = "voidcode://rule/"

#: Named query this tool asks the skills plugin whenever the caller reads a
#: ``voidcode://rule/`` URI: the skills plugin owns the rulebook semantics, so
#: this plugin never parses rule files itself.
RULE_PAGE_QUERY = "workspace.rule_page"

#: Named query this module expects the LSP plugin to answer for document-state
#: guards applied by ``apply_workspace_edit``.
DOCUMENT_VERSION_QUERY = "workspace.document_version"


@dataclass(frozen=True, slots=True)
class _RulePage:
    rule: str
    content_hash: str
    text: str
    next_offset: int | None
    byte_truncated: bool

    @classmethod
    def from_query(cls, value: Mapping[str, object]) -> _RulePage | None:
        rule = value.get("rule")
        content_hash = value.get("content_hash")
        text = value.get("text")
        next_offset = value.get("next_offset")
        truncated = value.get("byte_truncated")
        if not isinstance(rule, str) or not rule:
            return None
        if not isinstance(content_hash, str) or not content_hash:
            return None
        if not isinstance(text, str):
            return None
        if next_offset is not None and (not isinstance(next_offset, int) or isinstance(next_offset, bool) or next_offset < 1):
            return None
        if not isinstance(truncated, bool):
            return None
        return cls(rule=rule, content_hash=content_hash, text=text, next_offset=next_offset, byte_truncated=truncated)

    def as_payload(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "content_hash": self.content_hash,
            "text": self.text,
            "next_offset": self.next_offset,
            "byte_truncated": self.byte_truncated,
        }


def _spill_artifact(
    services: PluginServices,
    *,
    kind: str,
    content: str | bytes,
    mime: str,
    source: str,
) -> tuple[Mapping[str, object], str, str] | None:
    """Spill a payload via ``PluginServices.spill_artifact``; None keeps it inline.

    Returns ``(artifact, artifact_id, uri)``, or ``None`` when the handler's
    services object exposes no spill method: callers then keep their bounded
    inline output and omit the reference.
    """

    artifact = create_artifact(services, kind=kind, content=content, mime=mime, source=source)
    artifact_id = None if artifact is None else artifact.get("artifact_id")
    uri = artifact_uri(artifact)
    if artifact is None or not isinstance(artifact_id, str) or uri is None:
        return None
    return artifact, artifact_id, uri


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
    # No sidecar guidance files in the plugin world: the tool's own admitted
    # definition is the guidance source.
    guidance = definition.description or ""
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
    schema = json_wire_object(definition.input_schema)
    schema_text = json.dumps(schema, indent=2)
    sections.append(schema_text)
    sections.append("")
    effects = sorted(effect.value for effect in definition.effects)
    sections.append("effects: " + ", ".join(effects))
    read_only = is_read_tier(definition.effects)
    sections.append(f"read_only: {str(read_only).lower()}")
    content = "\n".join(sections).strip()
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
    view: Literal["pdf"] | None = None,
    page: int | None = None,
) -> _ReadOutcome:
    """Render an integrity-checked, bounded line or byte page from an artifact.

    The producer's media-type claim, read back with the first page, classifies
    the read: a raster image renders as an attachment (exactly like the
    workspace binary branch), a PDF claim takes the PDF view, and everything
    else pages as text. An artifact stored without a claim falls back to the
    ``%PDF-`` magic; no other sniffing is done, so image bytes without a claim
    still page as text.
    """

    artifact_id = path[len(VOIDCODE_ARTIFACT_PREFIX) :].strip()
    if not artifact_id:
        raise ValueError("voidcode://artifact/<id> requires an artifact id")
    if _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
        raise ValueError(f"invalid artifact id: {artifact_id}")
    if byte_limit is not None and byte_offset is None:
        raise ValueError("byte_limit requires byte_offset")

    def _window(byte_start: int, byte_length: int = MAX_ARTIFACT_PAGE_BYTES) -> Mapping[str, object]:
        window = read_artifact_page(services, artifact_id, byte_offset=byte_start, byte_limit=byte_length)
        if window is None:
            raise ValueError("read cannot resolve voidcode://artifact URLs without a core artifact service")
        return window

    head: Mapping[str, object] | None = None

    def _page(byte_start: int, byte_length: int = MAX_ARTIFACT_PAGE_BYTES) -> Mapping[str, object]:
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
            total_bytes = cast(int, window["total_bytes"])
            content_hash = cast(str, window["sha256"])
            if total_bytes > MAX_BINARY_READ_BYTES:
                raise ValueError(f"{label} exceeds the maximum supported size ({MAX_BINARY_READ_BYTES} bytes): {path}")
            chunk = cast(bytes, window["data"])
            if not chunk:
                return b"".join(chunks), total_bytes, content_hash
            chunks.append(chunk)
            if bool(window["eof"]):
                return b"".join(chunks), total_bytes, content_hash
            byte_start += len(chunk)

    first_page = _page(0)
    media_type = cast(str, first_page["media_type"])

    # An explicit PDF view on a non-PDF artifact still fails below, on the magic.
    if media_type in RASTER_IMAGE_MIMES and view != "pdf":
        raw, total_bytes, content_hash = _full_bytes("Artifact")
        inline = len(raw) <= MAX_INLINE_ATTACHMENT_BYTES
        # A payload above the inline bound re-spills its own (already stored)
        # bytes so the body carries the same reference projection the workspace
        # branch reports; when the spill method is absent the artifact's own URI
        # stands in for it.
        spilled = None if inline else _spill_artifact(services, kind="content", content=raw, mime=media_type, source=path)
        artifact: Mapping[str, object] = (
            spilled[0] if spilled is not None else {"artifact_id": artifact_id, "sha256": content_hash, "byte_count": total_bytes, "uri": path}
        )
        uri = cast(str, artifact["uri"])
        data_uri = f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}" if inline else None
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

    if view == "pdf" or media_type == "application/pdf" or (not media_type and cast(bytes, first_page["data"]).startswith(b"%PDF-")):
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
        if len(full_text.encode("utf-8")) > MAX_BYTES:
            # When the spill method is absent the extracted text beyond this page is
            # dropped; the page's own truncation flags stay the honest signal.
            spilled = _spill_artifact(services, kind="pdf_text", content=full_text, mime="text/plain; charset=utf-8", source=path)
            if spilled is not None:
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
        data = cast(bytes, window["data"])
        total_bytes = cast(int, window["total_bytes"])
        content_hash = cast(str, window["sha256"])
        next_byte_offset = None if bool(window["eof"]) else cast(int, window["offset"]) + len(data)
        truncated = next_byte_offset is not None
        raw_content = data.decode("utf-8", "replace")
        artifact: Mapping[str, object] = {"artifact_id": artifact_id, "sha256": content_hash, "byte_count": total_bytes, "uri": path}
        summary = f"Read {len(data)} artifact byte(s) from byte offset {byte_offset}"
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
                    "byte_count": len(data),
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
        chunk = cast(bytes, window["data"])
        newlines = chunk.count(b"\n")
        if newlines >= remaining:
            index = -1
            for _ in range(remaining):
                index = chunk.index(b"\n", index + 1)
            byte_start += index + 1
            break
        remaining -= newlines
        if not chunk or bool(window["eof"]):
            raise ValueError(f"Offset {offset} is out of range for this artifact")
        byte_start += len(chunk)

    window = _page(byte_start)
    data = cast(bytes, window["data"])
    total_bytes = cast(int, window["total_bytes"])
    content_hash = cast(str, window["sha256"])
    text = data.decode("utf-8", "replace")
    page_lines = text.split("\n")
    if page_lines[-1] == "":
        page_lines.pop()
    page_lines = [line[:-1] if line.endswith("\r") else line for line in page_lines]
    shown = page_lines[:page_limit]
    has_more = len(shown) < len(page_lines) or not bool(window["eof"])
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
    path: str, *, context: ToolContext, services: PluginServices, limit: int, tool_name_label: str = "read"
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
        params = parse_qs(query, strict_parsing=True)
        unknown = sorted(set(params) - {"after_sequence", "offset"})
        if unknown:
            return _uri_failure(tool_name_label, f"unsupported transcript query parameter: {unknown[0]}")
        for name, target in (("after_sequence", "after_sequence"), ("offset", "offset")):
            values = params.get(name)
            if values is None:
                continue
            if len(values) != 1 or not values[0].isdigit():
                return _uri_failure(tool_name_label, f"transcript {name} must be one non-negative integer")
            if target == "after_sequence":
                after_sequence = int(values[0])
            else:
                offset = int(values[0])
    caller_session_id = context.require_session_id()
    limit = max(1, min(limit, MAX_EVENT_PAGE))
    try:
        if session_id == caller_session_id:
            views = services.read_events(after_sequence=after_sequence, limit=limit, include_payload=True)
        else:
            views = services.read_child_events(session_id, after_sequence=after_sequence, limit=limit, include_payload=True)
    except Exception as exc:
        return _uri_failure(tool_name_label, f"transcript not accessible for session {session_id}: {exc}")
    page = render_history_page(views, after_sequence=after_sequence, offset=offset)
    more_rows = len(views) >= limit and page.next_offset == 0
    truncated = page.truncated or more_rows
    if page.text:
        body_text = page.text
    else:
        body_text = "(no readable events in this range)"
    lines = [
        f"Transcript for session {session_id} after sequence {after_sequence} ({len(views)} event row(s) read; untrusted session data).",
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
    path: str
    offset: int | None = None
    limit: int | None = None
    byte_offset: StrictInt | None = None
    byte_limit: StrictInt | None = None
    view: Literal["raw", "sqlite", "notebook", "pdf"] | None = None
    table: str | None = None
    query: str | None = None
    cell: StrictInt | None = None
    page: StrictInt | None = None
    output_offset: StrictInt | None = None
    output_limit: StrictInt | None = None

    _validate_path = field_validator("path", mode="after")(validate_non_empty)

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

    @field_validator("byte_offset", mode="after")
    @classmethod
    def _validate_byte_offset(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("byte_offset must be a non-negative, zero-based UTF-8 byte offset")
        return value

    @field_validator("byte_limit", mode="after")
    @classmethod
    def _validate_byte_limit(cls, value: int | None) -> int | None:
        if value is not None and not 4 <= value <= MAX_ARTIFACT_PAGE_BYTES:
            raise ValueError(f"byte_limit must be between 4 and {MAX_ARTIFACT_PAGE_BYTES}")
        return value

    @field_validator("cell", mode="after")
    @classmethod
    def _validate_cell(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("cell must be a non-negative zero-based index")
        return value

    @field_validator("page", mode="after")
    @classmethod
    def _validate_page(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("page must be a positive one-based page number")
        return value

    @field_validator("output_offset", "output_limit", mode="after")
    @classmethod
    def _validate_output_page(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("output_offset and output_limit must be positive integers")
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


def _render_file(candidate: Path, *, relative_path: str, offset: int, limit: int, services: PluginServices) -> _ReadOutcome:
    mime, _ = mimetypes.guess_type(candidate.name)
    is_svg = candidate.suffix.lower() == ".svg" or mime == "image/svg+xml"

    if not is_svg and (mime in RASTER_IMAGE_MIMES or mime == "application/pdf"):
        attachment_size = candidate.stat().st_size
        if attachment_size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"read attachment exceeds the maximum supported size ({MAX_ATTACHMENT_BYTES} bytes): {relative_path}")
        raw = candidate.read_bytes()
        content_hash = hashlib.sha256(raw).hexdigest()
        # When the spill method is absent the attachment stays inline (bounded
        # by MAX_INLINE_ATTACHMENT_BYTES) and the reference is omitted.
        spilled = _spill_artifact(services, kind="content", content=raw, mime=mime, source=relative_path)
        artifact = None if spilled is None else spilled[0]
        artifact_id = None if spilled is None else spilled[1]
        uri = None if spilled is None else spilled[2]
        bounds = OutputBounds(
            reference=OutputReference(uri=uri, artifact=artifact) if uri is not None else None,
            source=relative_path,
        )
        label = "Image" if mime in RASTER_IMAGE_MIMES else "PDF"
        message = f"{label} read successfully"
        if mime == "application/pdf":
            message = f"PDF {relative_path} ({mime}, {len(raw)} bytes, sha256:{content_hash}); " + (
                f'read the original PDF with read(path="{uri}", view="raw").'
                if uri is not None
                else "the original PDF reference is unavailable without a runtime artifact service."
            )
        data_uri = (
            f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
            if mime in RASTER_IMAGE_MIMES and len(raw) <= MAX_INLINE_ATTACHMENT_BYTES
            else None
        )
        attachment_body: dict[str, object] = {
            "path": relative_path,
            "type": "attachment",
            "content_type": mime,
            "byte_count": len(raw),
            "content_hash": content_hash,
        }
        if uri is not None:
            attachment_body["artifact_id"] = artifact_id
            attachment_body["attachment"] = {"mime": mime, "reference": uri}
            attachment_body["bounds"] = {"reference": uri}
        return _ReadOutcome(
            output=AttachmentOutput(mime, data_uri, presentation=message, bounds=bounds),
            body=OpaqueToolBody(attachment_body),
        )

    if _is_binary_file(candidate) and not is_svg:
        raw = candidate.read_bytes()
        effective_mime = mime or "application/octet-stream"
        content_hash = hashlib.sha256(raw).hexdigest()
        # When the spill method is absent the bytes stay out of the response
        # (they were never inlined) and the reference is omitted.
        spilled = _spill_artifact(services, kind="content", content=raw, mime=effective_mime, source=relative_path)
        artifact = None if spilled is None else spilled[0]
        artifact_id = None if spilled is None else spilled[1]
        uri = None if spilled is None else spilled[2]
        bounds = OutputBounds(
            reference=OutputReference(uri=uri, artifact=artifact) if uri is not None else None,
            source=relative_path,
        )
        presentation = f"Binary file {relative_path} ({effective_mime}, {len(raw)} bytes, sha256:{content_hash})." + (
            f' Read full content with read(path="{uri}").'
            if uri is not None
            else " The full content is unavailable without a runtime artifact service."
        )
        binary_body: dict[str, object] = {
            "path": relative_path,
            "type": "binary",
            "content_type": effective_mime,
            "byte_count": len(raw),
            "content_hash": content_hash,
        }
        if artifact_id is not None:
            binary_body["artifact_id"] = artifact_id
        binary_body["source"] = relative_path
        if uri is not None:
            binary_body["reference"] = uri
        return _ReadOutcome(
            output=TextOutput(presentation, bounds=bounds),
            body=OpaqueToolBody(binary_body),
        )
    digest = hashlib.sha256()
    with candidate.open("rb") as hash_handle:
        for chunk in iter(lambda: hash_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    content_hash = digest.hexdigest()

    limit = min(limit, DEFAULT_READ_LIMIT)
    rendered_lines: list[ReadLine] = []
    raw_content_lines: list[str] = []
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
                content_truncated = content_truncated or line_truncated
    except UnicodeDecodeError as exc:
        raise ValueError("read only supports UTF-8 text files") from exc

    if total_lines < offset and not (total_lines == 0 and offset == 1):
        raise ValueError(f"Offset {offset} is out of range for this file ({total_lines} lines)")

    next_offset = offset + len(rendered_lines)
    # A clipped line or a byte-budget cut mangles visible content, while a plain
    # line window only shortens the page: the visible lines stay truthful.
    lines_clipped = content_truncated
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
            f"Read {len(rendered_lines)} line(s) from {relative_path}" + ("; output is truncated." if content_truncated else "."),
            raw_content=body.raw_content,
            content_hash=body.content_hash,
            next_offset=body.next_offset,
            truncated=body.truncated,
            partial=body.partial,
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


type _TreeNode = dict[str, object]


def _int_field(node: _TreeNode, key: str) -> int:
    value = node.get(key, 0)
    return value if isinstance(value, int) else 0


def _float_field(node: _TreeNode, key: str) -> float:
    value = node.get(key, 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


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


def _tree_entries(directory: Path) -> list[Path]:
    try:
        entries = [entry for entry in directory.iterdir() if entry.name not in _TREE_IGNORE_DIRS]
    except OSError:
        return []

    def _mtime(entry: Path) -> float:
        try:
            return entry.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(entries, key=lambda entry: (-_mtime(entry), entry.name))


def _materialize_tree(root: Path, *, max_depth: int, per_dir_limit: int, entry_limit: int) -> tuple[_TreeNode, bool]:
    """Build a bounded tree node for ``root`` (recency-sorted, depth and entry capped)."""
    budget = entry_limit
    truncated = False

    def _walk(directory: Path, depth: int) -> _TreeNode:
        nonlocal budget, truncated
        entries = _tree_entries(directory)
        omitted = max(0, len(entries) - per_dir_limit)
        node: _TreeNode = {"name": directory.name, "is_dir": True, "expanded": depth < max_depth, "omitted": omitted, "children": []}
        children: list[_TreeNode] = []
        expanded = depth < max_depth
        for entry in entries[:per_dir_limit]:
            if budget <= 0:
                truncated = True
                break
            budget -= 1
            try:
                is_dir = entry.is_dir()
                size = 0 if is_dir else entry.stat().st_size
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            child: _TreeNode = {"name": entry.name, "is_dir": is_dir, "size": size, "mtime": mtime, "expanded": False, "omitted": 0, "children": []}
            if is_dir and expanded:
                child = _walk(entry, depth + 1)
                child["size"] = 0
                child["mtime"] = mtime
            elif not is_dir:
                child["expanded"] = False
            children.append(child)
        node["children"] = children
        return node

    return _walk(root, 0), truncated


def _render_directory(root: Path, *, label: str) -> _ReadOutcome:
    tree, budget_truncated = _materialize_tree(
        root,
        max_depth=DIRECTORY_MAX_DEPTH,
        per_dir_limit=DIRECTORY_PER_DIR_LIMIT,
        entry_limit=DIRECTORY_ENTRY_LIMIT,
    )
    lines = [f"{label}/"]
    entry_count = 0

    def _emit(node: _TreeNode, prefix: str) -> None:
        nonlocal entry_count
        children = node["children"]
        assert isinstance(children, list)
        for index, child in enumerate(children):
            assert isinstance(child, dict)
            last = index == len(children) - 1
            connector = "└── " if last else "├── "
            is_dir = bool(child["is_dir"])
            name = str(child["name"])
            mtime = _float_field(child, "mtime")
            if is_dir:
                lines.append(f"{prefix}{connector}{name}/  ({_relative_age(mtime)})")
            else:
                lines.append(f"{prefix}{connector}{name} ({_human_size(_int_field(child, 'size'))}, {_relative_age(mtime)})")
            entry_count += 1
            if is_dir:
                nested_prefix = f"{prefix}{'    ' if last else '│   '}"
                nested = child["children"]
                assert isinstance(nested, list)
                if child["expanded"] and not nested:
                    lines.append(f"{nested_prefix}└── (empty directory)")
                _emit(child, nested_prefix)
            omitted = _int_field(child, "omitted")
            if omitted:
                lines.append(f"{prefix}{'    ' if last else '│   '}… {omitted} more")

    _emit(tree, "")
    root_omitted = _int_field(tree, "omitted")
    if root_omitted:
        lines.append(f"… {root_omitted} more")
    truncated = budget_truncated or root_omitted > 0
    if truncated:
        lines.append(
            f"… listing truncated at {DIRECTORY_ENTRY_LIMIT} entries / {DIRECTORY_PER_DIR_LIMIT} per directory; read a subdirectory to see the rest."
        )
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
    parts = [part for part in raw.strip().replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"archive member path must not contain '..': {raw}")
    return "/".join(parts)


def _split_archive_path(path: str) -> tuple[str, str, str] | None:
    """Split ``archive.ext[:inner]`` into (archive path, raw inner, family)."""
    lowered = path.lower()
    best: tuple[int, str, str] | None = None
    for family, suffixes in (("zip", _ZIP_SUFFIXES), ("tar", _TAR_SUFFIXES)):
        for suffix in suffixes:
            index = lowered.find(suffix)
            if index < 0:
                continue
            end = index + len(suffix)
            if end < len(lowered) and lowered[end] != ":":
                continue
            if best is None or index < best[0] or (index == best[0] and len(suffix) > len(best[1])):
                best = (index, suffix, family)
    if best is None:
        return None
    index, suffix, family = best
    inner = path[index + len(suffix) :]
    return path[: index + len(suffix)], inner[1:] if inner.startswith(":") else inner, family


def _zip_members(archive: zipfile.ZipFile) -> list[tuple[str, int, bool]]:
    return [(_normalize_member_path(info.filename), info.file_size, info.filename.endswith("/")) for info in archive.infolist()]


def _tar_members(archive: tarfile.TarFile) -> list[tuple[str, int, bool]]:
    return [(_normalize_member_path(member.name), member.size, member.isdir()) for member in archive.getmembers()]


def _zip_member_bytes(archive: zipfile.ZipFile, normalized: str) -> bytes:
    for info in archive.infolist():
        if info.filename.endswith("/") or _normalize_member_path(info.filename) != normalized:
            continue
        if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"archive member exceeds the maximum supported size ({MAX_ARCHIVE_MEMBER_BYTES} bytes): {normalized}")
        return archive.read(info.filename)
    raise ValueError(f"archive member not found: {normalized}")


def _tar_member_bytes(archive: tarfile.TarFile, normalized: str) -> bytes:
    for member in archive.getmembers():
        if not member.isfile() or _normalize_member_path(member.name) != normalized:
            continue
        if member.size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"archive member exceeds the maximum supported size ({MAX_ARCHIVE_MEMBER_BYTES} bytes): {normalized}")
        handle = archive.extractfile(member)
        if handle is None:
            break
        return handle.read()
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
        if tail:
            children.setdefault(head, (0, True))
        else:
            children[head] = (size, is_dir)
    return sorted((name, size, is_dir) for name, (size, is_dir) in children.items())


def _render_archive_lines(text: str, *, label: str, offset: int, limit: int) -> _ReadOutcome:
    limit = min(limit, DEFAULT_READ_LIMIT)
    source_lines = text.splitlines()
    total_lines = len(source_lines)
    if total_lines < offset and not (total_lines == 0 and offset == 1):
        raise ValueError(f"Offset {offset} is out of range for this member ({total_lines} lines)")
    rendered_lines: list[str] = []
    bytes_used = 0
    content_truncated = False
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
        content_truncated = content_truncated or line_truncated
    next_offset = offset + len(rendered_lines)
    content_truncated = content_truncated or has_more
    raw_content = "\n".join(rendered_lines)
    return _ReadOutcome(
        output=_text_output(
            f"Read {len(rendered_lines)} line(s) from {label}" + ("; output is truncated." if content_truncated else "."),
            raw_content=raw_content,
            next_offset=next_offset if has_more else None,
            truncated=content_truncated,
            partial=content_truncated,
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


def _render_archive(archive_path: Path, inner: str, *, family: str, label: str, offset: int, limit: int) -> _ReadOutcome:
    if family == "zip":
        try:
            with zipfile.ZipFile(archive_path) as zip_archive:
                return _render_archive_members(
                    _zip_members(zip_archive),
                    reader=lambda normalized: _zip_member_bytes(zip_archive, normalized),
                    inner=inner,
                    label=label,
                    offset=offset,
                    limit=limit,
                )
        except zipfile.BadZipFile as exc:
            raise ValueError(f"not a valid zip archive: {label}") from exc
    try:
        with tarfile.TarFile.open(archive_path) as tar_archive:
            return _render_archive_members(
                _tar_members(tar_archive),
                reader=lambda normalized: _tar_member_bytes(tar_archive, normalized),
                inner=inner,
                label=label,
                offset=offset,
                limit=limit,
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
) -> _ReadOutcome:
    normalized_inner = _normalize_member_path(inner)
    names = {name for name, _, _ in members}
    is_directory = (
        not normalized_inner
        or any(name.startswith(f"{normalized_inner}/") for name in names)
        or any(name == normalized_inner and dir_flag for name, _, dir_flag in members)
    )
    if not normalized_inner or is_directory:
        children = _archive_children(members, normalized_inner)
        truncated = len(children) > ARCHIVE_LIST_LIMIT
        shown = children[:ARCHIVE_LIST_LIMIT]
        header = f"{label}:" if not normalized_inner else f"{label}/"
        lines = [header]
        for name, size, dir_flag in shown:
            lines.append(f"{name}/" if dir_flag else f"{name} ({_human_size(size)})")
        if truncated:
            lines.append(f"… {len(children) - ARCHIVE_LIST_LIMIT} more; list a subdirectory to see the rest.")
        entry_count = len(shown)
        rendered = "\n".join(lines)
        return _ReadOutcome(
            output=_text_output(
                f"Listed {entry_count} entr{'y' if entry_count == 1 else 'ies'} in {label}" + ("; listing is truncated." if truncated else "."),
                raw_content=rendered,
                truncated=truncated,
                partial=truncated,
            ),
            body=OpaqueToolBody(
                {
                    "path": label,
                    "type": "archive_listing",
                    "entry_count": entry_count,
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
    return _render_archive_lines(text, label=label, offset=offset, limit=limit)


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
    text_reference: str | None = None
    if isinstance(full_text, str) and len(full_text.encode("utf-8")) > MAX_BYTES:
        # When the spill method is absent the extracted text beyond this view is
        # dropped; the view's own truncation flags stay the honest signal.
        spilled = _spill_artifact(services, kind="pdf_text", content=full_text, mime="text/plain; charset=utf-8", source=label)
        if spilled is not None:
            text_reference = spilled[2]
            payload["text_reference"] = text_reference
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
        # When the spill method is absent the image record carries no reference
        # and the bytes were never inlined.
        spilled = _spill_artifact(
            services,
            kind="notebook_image",
            content=image_bytes,
            mime=str(image["mime"]),
            source=f"{label}#cell={image['cell']}",
        )
        if spilled is not None:
            image["reference"] = spilled[2]
            image["artifact"] = spilled[0]
            artifacts.append(spilled[0])
    source_reference: str | None = None
    if view == "pdf" or payload.get("truncated_text") is True or payload.get("raw_reference_required") is True:
        if not isinstance(source_bytes, bytes):
            raise ValueError(f"{view} source snapshot is unavailable")
        # When the spill method is absent the source snapshot reference is omitted.
        spilled = _spill_artifact(
            services,
            kind="pdf_source" if view == "pdf" else "notebook_source",
            content=source_bytes,
            mime="application/pdf" if view == "pdf" else "application/json",
            source=label,
        )
        if spilled is not None:
            source_reference = spilled[2]
            payload["source_reference"] = source_reference
            payload["source_artifact"] = spilled[0]
    if artifacts:
        payload["artifacts"] = artifacts

    raw_content = json.dumps(payload, ensure_ascii=False, indent=2)
    output_reference: str | None = None
    output_truncated = len(raw_content.encode("utf-8")) > MAX_BYTES
    if output_truncated:
        # When the spill method is absent the cut view body is unreachable, so
        # the truncation flags stand alone.
        spilled = _spill_artifact(services, kind=f"{view}_view", content=raw_content, mime="application/json", source=label)
        if spilled is not None:
            output_reference = spilled[2]
            payload["output_reference"] = output_reference
            payload["output_artifact"] = spilled[0]
        raw_content = raw_content.encode("utf-8")[: MAX_BYTES // 2].decode("utf-8", "ignore") + "\n…"
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
            bounds=OutputBounds(truncated=truncated, partial=truncated, source=label),
        ),
        body=OpaqueToolBody(payload),
    )


@final
class ReadTool:
    """Read a file or supported attachment from the current workspace."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="read",
        description="Read a file inside the current workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Internal URLs: voidcode://tool/<name> reads a tool's guidance and input schema; "
                        "voidcode://rule/<name> reads a bounded workspace rule catalog entry; "
                        "voidcode://artifact/<id> reads a bounded slice of the current session's spilled tool-output artifact; "
                        "voidcode://transcript/<session_id> reads a bounded, payload-stripped transcript of the current session "
                        "or of a child session it spawned."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "1-based starting file line, SQLite row, Notebook cell, or PDF text line; "
                        "Notebook cell offsets use `offset` rather than `cell`."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2000,
                    "description": "Maximum file lines, SQLite rows, Notebook cells, or PDF text lines to return.",
                },
                "byte_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Zero-based UTF-8 byte offset for continuing a long artifact line; use the returned next_byte_offset.",
                },
                "byte_limit": {
                    "type": "integer",
                    "minimum": 4,
                    "maximum": 47104,
                    "description": "Maximum UTF-8 bytes in an artifact page; requires byte_offset.",
                },
                "view": {
                    "type": "string",
                    "enum": ["raw", "sqlite", "notebook", "pdf"],
                    "description": (
                        "Select a read-only derived view; raw returns the original file bytes/text and preserves ordinary file-read semantics."
                    ),
                },
                "table": {"type": "string", "description": "SQLite table to inspect; defaults to the first user table."},
                "query": {"type": "string", "description": "One read-only SQLite SELECT/WITH query."},
                "cell": {"type": "integer", "minimum": 0, "description": "Zero-based notebook cell index to inspect alone."},
                "page": {"type": "integer", "minimum": 1, "description": "One-based PDF page to extract."},
                "output_offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based notebook output index within `cell`; requires the zero-based cell selector.",
                },
                "output_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Maximum notebook outputs to return for `cell`; continue with next_output_offset.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
        supports_parallel=True,
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        args = parse_tool_args(
            ReadArgs,
            {
                "path": call.arguments.get("path"),
                "offset": call.arguments.get("offset"),
                "limit": call.arguments.get("limit"),
                "byte_offset": call.arguments.get("byte_offset"),
                "byte_limit": call.arguments.get("byte_limit"),
                "view": call.arguments.get("view"),
                "table": call.arguments.get("table"),
                "query": call.arguments.get("query"),
                "cell": call.arguments.get("cell"),
                "page": call.arguments.get("page"),
                "output_offset": call.arguments.get("output_offset"),
                "output_limit": call.arguments.get("output_limit"),
            },
            tool_name=self.definition.name,
        )
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
                answered = services.query(
                    RULE_PAGE_QUERY,
                    {"uri": args.path, "offset": args.offset or 1, "limit": args.limit or DEFAULT_READ_LIMIT},
                )
            except QueryUnavailable:
                return _uri_failure(self.definition.name, f"rule catalog is unavailable in this runtime generation: {args.path}")
            if answered is None:
                return _uri_failure(self.definition.name, f"unknown rule in this workspace: {args.path}")
            page = _RulePage.from_query(answered)
            if page is None:
                return _uri_failure(self.definition.name, f"unreadable rule page for {args.path}: provider shape violated")
            next_offset = page.next_offset
            truncated = page.byte_truncated or next_offset is not None
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
            if args.view == "pdf":
                if (
                    args.byte_offset is not None
                    or args.byte_limit is not None
                    or args.table is not None
                    or args.query is not None
                    or args.cell is not None
                    or args.output_offset is not None
                    or args.output_limit is not None
                ):
                    raise ValueError("PDF artifact view does not accept byte, SQLite, or notebook arguments")
                return _read_success(
                    _render_artifact(
                        args.path,
                        context=context,
                        services=services,
                        offset=args.offset or 1,
                        limit=args.limit or DEFAULT_READ_LIMIT,
                        byte_offset=args.byte_offset,
                        byte_limit=args.byte_limit,
                        view="pdf",
                        page=args.page,
                    )
                )
            if args.view not in (None, "raw") or any(
                value is not None for value in (args.table, args.query, args.cell, args.page, args.output_offset, args.output_limit)
            ):
                raise ValueError("only raw or PDF views are supported for artifact URLs")
            return _read_success(
                _render_artifact(
                    args.path,
                    context=context,
                    services=services,
                    offset=args.offset or 1,
                    limit=args.limit or DEFAULT_READ_LIMIT,
                    byte_offset=args.byte_offset,
                    byte_limit=args.byte_limit,
                )
            )
        if args.path.startswith(VOIDCODE_TRANSCRIPT_PREFIX):
            outcome = _render_transcript(
                args.path,
                context=context,
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

        archive = _split_archive_path(args.path)
        if archive is not None:
            archive_path, inner, family = archive
            requested_archive = Path(archive_path)
            resolved_archive = (requested_archive if requested_archive.is_absolute() else workspace_root / requested_archive).resolve()
            if not resolved_archive.is_file():
                raise ValueError(f"read target does not exist: {archive_path}")
            return _read_success(
                _render_archive(
                    resolved_archive,
                    inner,
                    family=family,
                    label=args.path,
                    offset=offset,
                    limit=limit,
                )
            )

        if args.path.lower().endswith(UNSUPPORTED_ARCHIVE_SUFFIXES):
            suffix = Path(args.path).suffix.lower() or args.path
            raise ValueError(
                f"read does not support {suffix} archives (third-party codec required); "
                f"supported archive families are zip ({', '.join(_ZIP_SUFFIXES)}) and tar ({', '.join(_TAR_SUFFIXES)})."
            )
        if not candidate.exists():
            raise ValueError(f"read target does not exist: {args.path}")
        if candidate.is_dir():
            return _read_success(_render_directory(candidate, label=relative_path))
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

        return _read_success(_render_file(candidate, relative_path=relative_path, offset=offset, limit=limit, services=services))
