"""Shared bounded renderers for content-addressed resource pages.

One paging implementation serves every digest-addressed namespace: the
``artifact://`` reader and the web ``web://`` snapshot reader both parse
their own URI, then render through :func:`render_stored_artifact_page`.
Capabilities import only the SDK, so neither reader imports a concrete
plugin or the ``read`` tool.
"""

from __future__ import annotations

import codecs
from concurrent.futures import CancelledError
from typing import Protocol

from voidcode.sdk.resources import ResourcePage, ResourceReadCaller
from voidcode.sdk.services import ArtifactPage
from voidcode.sdk.tools import RASTER_IMAGE_MIMES, TOOL_OUTPUT_BYTES, AttachmentOutput, OutputBounds, TextOutput

#: One bounded page stays below the model projection so result metadata fits.
MAX_RESOURCE_PAGE_BYTES = TOOL_OUTPUT_BYTES - 4096


class ArtifactPageReader(Protocol):
    """The shared artifact pager's only service dependency."""

    def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage: ...


def format_resource_text(
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


def _check_cancelled(caller: ResourceReadCaller) -> None:
    if caller.abort_signal is not None and caller.abort_signal.cancelled:
        raise CancelledError("read cancelled")


def render_stored_artifact_page(
    *,
    uri: str,
    digest: str,
    services: ArtifactPageReader,
    caller: ResourceReadCaller,
    offset: int = 1,
    limit: int = 2000,
    byte_offset: int | None = None,
    byte_limit: int | None = None,
) -> ResourcePage | None:
    """Render one bounded page of stored artifact bytes addressed by digest.

    ``None`` means the digest is not stored. Callers parse their own URI.
    Only genuine absence maps to ``None``; permission, storage, bound, and
    contract errors propagate so the runtime reports them visibly.
    """
    _check_cancelled(caller)

    def _window(byte_start: int, byte_length: int = MAX_RESOURCE_PAGE_BYTES) -> ArtifactPage:
        _check_cancelled(caller)
        return services.read_artifact(digest, offset=byte_start, limit=byte_length)

    head: ArtifactPage | None = None

    def _page(byte_start: int, byte_length: int = MAX_RESOURCE_PAGE_BYTES) -> ArtifactPage:
        nonlocal head
        if byte_start == 0 and byte_length == MAX_RESOURCE_PAGE_BYTES:
            if head is None:
                head = _window(0, byte_length)
            return head
        return _window(byte_start, byte_length)

    try:
        first_page = _page(0)
    except FileNotFoundError:
        return None
    artifact = first_page.artifact
    media_type = artifact.media_type

    if byte_offset is None and media_type in RASTER_IMAGE_MIMES:
        return ResourcePage(
            output=AttachmentOutput(
                media_type,
                None,
                presentation=f"Image artifact read successfully; full image: {uri}.",
                bounds=OutputBounds(reference=artifact, source=uri),
            ),
            body={
                "path": uri,
                "type": "attachment",
                "artifact": artifact.as_payload(),
            },
        )

    if byte_offset is not None:
        window_limit = min(byte_limit if byte_limit is not None else MAX_RESOURCE_PAGE_BYTES, MAX_RESOURCE_PAGE_BYTES)
        window = _page(byte_offset, window_limit)
        data = window.data
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        raw_content = decoder.decode(data, final=window.eof)
        if len(raw_content.encode("utf-8")) > MAX_RESOURCE_PAGE_BYTES:
            data = data[: MAX_RESOURCE_PAGE_BYTES // 3]
            decoder.reset()
            raw_content = decoder.decode(data, final=False)
        consumed = len(data) - len(decoder.getstate()[0])
        next_byte_offset = None if window.eof and consumed == len(window.data) else window.offset + consumed
        truncated = next_byte_offset is not None
        summary = f"Read {consumed} artifact byte(s) from byte offset {byte_offset}"
        summary += f"; continue with byte_offset={next_byte_offset}." if next_byte_offset is not None else "."
        return ResourcePage(
            output=format_resource_text(
                summary,
                raw_content=raw_content,
                content_hash=artifact.sha256,
                next_byte_offset=next_byte_offset,
                truncated=truncated,
                partial=truncated,
                bounds=OutputBounds(truncated=truncated, partial=truncated, reference=artifact),
            ),
            body={
                "path": uri,
                "type": "artifact",
                "artifact": artifact.as_payload(),
                "offset": byte_offset,
                "offset_unit": "bytes",
                "byte_limit": window_limit,
                "next_byte_offset": next_byte_offset,
                "truncated": truncated,
                "partial": truncated,
                "byte_count": consumed,
                "raw_content": raw_content,
            },
        )

    page_limit = min(limit, 2000)
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
    if not window.eof:
        last_newline = data.rfind(b"\n")
        if last_newline == -1:
            return render_stored_artifact_page(
                uri=uri,
                digest=digest,
                services=services,
                caller=caller,
                offset=offset,
                limit=limit,
                byte_offset=byte_start,
            )
        data = data[: last_newline + 1]
    text = data.decode("utf-8", "replace")
    if len(text.encode("utf-8")) > MAX_RESOURCE_PAGE_BYTES:
        return render_stored_artifact_page(
            uri=uri,
            digest=digest,
            services=services,
            caller=caller,
            offset=offset,
            limit=limit,
            byte_offset=byte_start,
        )
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
    summary = f"Read {len(shown)} line(s) from {uri}"
    if has_more:
        summary += f"; continue with offset={next_offset}." if next_offset is not None else "; output is truncated."
    else:
        summary += "."
    return ResourcePage(
        output=format_resource_text(
            summary,
            raw_content=raw_content,
            content_hash=artifact.sha256,
            next_offset=next_offset,
            truncated=has_more,
            partial=has_more,
            bounds=OutputBounds(truncated=has_more, partial=has_more, reference=artifact),
        ),
        body={
            "path": uri,
            "type": "artifact",
            "artifact": artifact.as_payload(),
            "offset": offset,
            "offset_unit": "lines",
            "limit": page_limit,
            "next_offset": next_offset,
            "truncated": has_more,
            "partial": has_more,
            "byte_count": len(raw_content.encode("utf-8")),
            "raw_content": raw_content,
            "line_count": len(shown),
        },
    )
