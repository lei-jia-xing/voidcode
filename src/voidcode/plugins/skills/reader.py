"""The ``skill://`` resource reader and bounded page rendering."""

from __future__ import annotations

import codecs

from voidcode.capabilities.resource_pages import MAX_RESOURCE_PAGE_BYTES, format_resource_text, raise_if_cancelled
from voidcode.sdk.resources import ResourcePage, ResourceReadCaller, ResourceReaderServices, ResourceReadRequest
from voidcode.sdk.tools import OutputBounds

from .config import SkillsConfig
from .manifest import SKILL_URI_PREFIX, SkillMetadata
from .registry import build_registry

DEFAULT_SKILL_READ_LIMIT = 2000


def _skill_rendered_content(skill: SkillMetadata) -> str:
    return (
        f"## Skill: {skill.name}\n"
        f"**Description**: {skill.description}\n"
        f"sourcePath: {skill.entry_path}\n"
        f"baseDirectory: {skill.directory}\n\n"
        f"{skill.content}"
    )


def _skill_byte_page(
    rendered: str,
    *,
    uri: str,
    skill: SkillMetadata,
    byte_offset: int,
    caller: ResourceReadCaller,
    byte_limit: int | None = None,
    reason: str = "",
) -> ResourcePage:
    """One byte-bounded page, mirroring the shared artifact pager for oversized lines."""
    raise_if_cancelled(caller)
    data = rendered.encode("utf-8")
    if byte_offset > len(data):
        raise ValueError(f"byte offset {byte_offset} is out of range for this skill ({len(data)} bytes)")
    limit = min(byte_limit if byte_limit is not None else MAX_RESOURCE_PAGE_BYTES, MAX_RESOURCE_PAGE_BYTES)
    end = min(len(data), byte_offset + limit)
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        raw_content = decoder.decode(data[byte_offset:end], final=end == len(data))
    except UnicodeDecodeError as exc:
        raise ValueError("byte_offset is not a UTF-8 character boundary for this skill") from exc
    consumed = end - byte_offset - len(decoder.getstate()[0])
    if consumed <= 0 and byte_offset < len(data):
        raise ValueError("byte limit is too small to decode one UTF-8 character")
    next_byte_offset = None if end == len(data) else byte_offset + consumed
    truncated = next_byte_offset is not None
    summary = f"Read {consumed} byte(s) from {uri} at byte offset {byte_offset}"
    if reason:
        summary += f" ({reason})"
    summary += f"; continue with byte_offset={next_byte_offset}." if next_byte_offset is not None else "."
    return ResourcePage(
        output=format_resource_text(
            summary,
            raw_content=raw_content,
            next_byte_offset=next_byte_offset,
            truncated=truncated,
            partial=truncated,
            bounds=OutputBounds(truncated=truncated, partial=truncated),
        ),
        body={
            "path": uri,
            "type": "skill",
            "name": skill.name,
            "description": skill.description,
            "sourcePath": str(skill.entry_path),
            "baseDirectory": str(skill.directory),
            "byte_offset": byte_offset,
            "offset_unit": "bytes",
            "byte_limit": limit,
            "next_byte_offset": next_byte_offset,
            "truncated": truncated,
            "partial": truncated,
            "byte_count": consumed,
            "raw_content": raw_content,
        },
    )


def _skill_page(
    rendered: str,
    *,
    uri: str,
    skill: SkillMetadata,
    offset: int,
    limit: int,
    caller: ResourceReadCaller,
) -> ResourcePage:
    raise_if_cancelled(caller)
    lines = rendered.split("\n")
    total = len(lines)
    if offset > total:
        raise ValueError(f"Offset {offset} is out of range for this skill ({total} lines)")
    start = offset - 1
    shown = lines[start : start + limit]

    def render_shown() -> str:
        raw_content = "\n".join(shown)
        if shown and (start + len(shown) < total or rendered.endswith("\n")):
            raw_content += "\n"
        return raw_content

    raw_content = render_shown()
    byte_trimmed = False
    while shown and len(raw_content.encode("utf-8")) > MAX_RESOURCE_PAGE_BYTES:
        byte_trimmed = True
        shown.pop()
        raw_content = render_shown()
    if not shown and start < total:
        line_start = len("\n".join(lines[:start]).encode("utf-8")) + (1 if start else 0)
        return _skill_byte_page(
            rendered,
            uri=uri,
            skill=skill,
            byte_offset=line_start,
            caller=caller,
            reason=f"line {offset} exceeds the {MAX_RESOURCE_PAGE_BYTES}-byte page cap",
        )
    next_offset = offset + len(shown) if start + len(shown) < total else None
    truncated = next_offset is not None
    summary = f"Read {len(shown)} line(s) from {uri}"
    if byte_trimmed:
        summary += f" (page byte cap {MAX_RESOURCE_PAGE_BYTES} reached)"
    summary += f"; continue with offset={next_offset}." if next_offset is not None else "."
    return ResourcePage(
        output=format_resource_text(
            summary,
            raw_content=raw_content,
            next_offset=next_offset,
            truncated=truncated,
            partial=truncated,
            bounds=OutputBounds(truncated=truncated, partial=truncated),
        ),
        body={
            "path": uri,
            "type": "skill",
            "name": skill.name,
            "description": skill.description,
            "sourcePath": str(skill.entry_path),
            "baseDirectory": str(skill.directory),
            "offset": offset,
            "offset_unit": "lines",
            "limit": limit,
            "next_offset": next_offset,
            "truncated": truncated,
            "partial": truncated,
            "byte_count": len(raw_content.encode("utf-8")),
            "byte_cap": MAX_RESOURCE_PAGE_BYTES,
            "raw_content": raw_content,
            "line_count": len(shown),
        },
    )


class SkillReader:
    """The ``skill://`` namespace: one discovered skill's rendered SKILL.md per URI."""

    def __init__(self, config: SkillsConfig) -> None:
        self._config = config

    def read(self, request: ResourceReadRequest, caller: ResourceReadCaller, services: ResourceReaderServices) -> ResourcePage | None:
        _ = services
        raise_if_cancelled(caller)
        name = request.uri[len(SKILL_URI_PREFIX) :].strip()
        if not name:
            raise ValueError("skill://<name> requires a skill name")
        registry = build_registry(workspace=caller.workspace, config=self._config)
        skill = registry.skills.get(name)
        if skill is None:
            failure = registry.failure(name)
            if failure is not None:
                raise ValueError(f"skill '{name}' is unavailable: {failure.message} ({failure.path})")
            return None
        if not skill.enabled:
            raise ValueError(f"skill '{name}' is disabled")
        rendered = _skill_rendered_content(skill)
        if request.byte_offset is not None:
            return _skill_byte_page(
                rendered,
                uri=request.uri,
                skill=skill,
                byte_offset=request.byte_offset,
                byte_limit=request.byte_limit,
                caller=caller,
            )
        limit = min(request.limit if request.limit is not None else DEFAULT_SKILL_READ_LIMIT, DEFAULT_SKILL_READ_LIMIT)
        return _skill_page(rendered, uri=request.uri, skill=skill, offset=request.offset, limit=limit, caller=caller)
