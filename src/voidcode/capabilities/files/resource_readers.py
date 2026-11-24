"""Default ``tool://``, ``artifact://``, and ``transcript://`` readers.

Ordinary bundled capability adapters registered by the files plugin: they
parse their own URI, choose their own defaults, and render bounded pages
through the narrow ``ResourceReaderServices`` surface. The ``read`` tool
never imports this module; registration flows through ``Plugin``.

Each reader declares which generic cursors it honors instead of ignoring
them: ``artifact://`` pages bytes by default and lines with ``offset``
(``offset_unit`` says which), ``transcript://`` pages with the URI's own
UTF-8 byte cursor and rejects ``offset``, and ``tool://`` is a single
document with no paging arguments.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from voidcode.capabilities.resource_pages import format_resource_text, render_stored_artifact_page
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.resources import (
    ResourcePage,
    ResourceReadCaller,
    ResourceReader,
    ResourceReaderServices,
    ResourceReadRequest,
)
from voidcode.sdk.tools import OutputBounds, is_read_tier

_ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_ARTIFACT_LIMIT = 2000
DEFAULT_TRANSCRIPT_LIMIT = 20
MAX_EVENT_PAGE = 200


def _reject_byte_args(request: ResourceReadRequest, *, scheme_label: str) -> None:
    if request.byte_offset is not None or request.byte_limit is not None:
        raise ValueError(f"byte_offset and byte_limit are not supported for {scheme_label} URIs")


def read_tool_documentation(request: ResourceReadRequest, caller: ResourceReadCaller, services: ResourceReaderServices) -> ResourcePage | None:
    del caller
    _reject_byte_args(request, scheme_label="tool://")
    if request.offset != 1:
        raise ValueError("offset is not supported for tool:// URIs")
    if request.limit is not None:
        raise ValueError("limit is not supported for tool:// URIs")
    tool_name = request.uri[len("tool://") :].strip()
    if not tool_name:
        raise ValueError("tool://<name> requires a tool name")
    definition = services.tool_definition(tool_name)
    if definition is None:
        return None
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
    return ResourcePage(
        output=format_resource_text(f"Read documentation for tool {tool_name}.", raw_content=content),
        body={
            "path": request.uri,
            "type": "tool_documentation",
            "tool_name": definition.name,
            "effects": effects,
            "read_only": read_only,
            "guidance": guidance,
            "input_schema": schema,
            "raw_content": content,
        },
    )


def read_artifact_resource(request: ResourceReadRequest, caller: ResourceReadCaller, services: ResourceReaderServices) -> ResourcePage | None:
    artifact_id = request.uri[len("artifact://") :].strip()
    if not artifact_id:
        raise ValueError("artifact://<id> requires an artifact id")
    if _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
        raise ValueError(f"invalid artifact id: {artifact_id}")
    return render_stored_artifact_page(
        uri=request.uri,
        digest=artifact_id,
        services=services,
        caller=caller,
        offset=request.offset,
        limit=request.limit if request.limit is not None else DEFAULT_ARTIFACT_LIMIT,
        byte_offset=request.byte_offset,
        byte_limit=request.byte_limit,
    )


def read_transcript_resource(request: ResourceReadRequest, caller: ResourceReadCaller, services: ResourceReaderServices) -> ResourcePage | None:
    del caller
    _reject_byte_args(request, scheme_label="transcript://")
    if request.offset != 1:
        raise ValueError("offset is not supported for transcript:// URIs; use the URI's ?offset= UTF-8 byte cursor")
    raw_target = request.uri[len("transcript://") :].strip()
    session_id, _, query = raw_target.partition("?")
    session_id = session_id.strip()
    if not session_id:
        raise ValueError("transcript://<session_id> requires a session id")
    if "/" in session_id or "?" in session_id:
        raise ValueError("session_id must not contain '/' or '?'")
    after_sequence = 0
    offset = 0
    if query:
        try:
            params = parse_qs(query, strict_parsing=True, keep_blank_values=True)
        except ValueError as exc:
            raise ValueError(f"invalid transcript query: {exc}") from exc
        unknown = sorted(set(params) - {"after_sequence", "offset"})
        if unknown:
            raise ValueError(f"unsupported transcript query parameter: {unknown[0]}")
        for name in ("after_sequence", "offset"):
            values = params.get(name)
            if values is None:
                continue
            if len(values) != 1 or not values[0].isdecimal():
                raise ValueError(f"transcript {name} must be one non-negative integer")
            try:
                value = int(values[0])
            except ValueError as exc:
                raise ValueError(f"transcript {name} exceeds the supported integer length") from exc
            if name == "after_sequence":
                after_sequence = value
            else:
                offset = value
    limit = request.limit if request.limit is not None else DEFAULT_TRANSCRIPT_LIMIT
    limit = max(1, min(limit, MAX_EVENT_PAGE))
    page = services.read_session_history(session_id, after_sequence=after_sequence, offset=offset, limit=limit)
    truncated = page.truncated
    body_text = page.text if page.text else "(no readable events in this range)"
    lines = [
        f"Transcript for session {session_id} after sequence {after_sequence} (untrusted session data).",
        body_text,
    ]
    next_uri: str | None = None
    if truncated:
        next_uri = f"transcript://{session_id}?after_sequence={page.next_after_sequence}&offset={page.next_offset}"
        lines.append(f"More history: read(path={json.dumps(next_uri)}). The cursor offset counts UTF-8 bytes within one rendered block.")
    return ResourcePage(
        output=format_resource_text(
            "\n".join(lines),
            truncated=truncated,
            partial=truncated,
            bounds=OutputBounds(truncated=truncated, partial=truncated, reference=next_uri),
        ),
        body={
            "path": request.uri,
            "type": "transcript",
            "session_id": session_id,
            "after_sequence": after_sequence,
            "next_after_sequence": page.next_after_sequence,
            "next_offset": page.next_offset,
            "offset_unit": "bytes",
            "truncated": truncated,
        },
    )


def create_resource_readers() -> tuple[ResourceReader, ...]:
    """The default resource namespaces the files plugin serves."""
    return (
        ResourceReader("tool", read_tool_documentation),
        ResourceReader("artifact", read_artifact_resource),
        ResourceReader("transcript", read_transcript_resource),
    )
