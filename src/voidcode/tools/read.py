"""Safe read-only file tool for the deterministic slice."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import tarfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, final

from pydantic import BaseModel, field_validator

from ..core.tool_context import RULE_URI_PREFIX, ToolContext
from ..security.path_policy import resolve_workspace_path as resolve_workspace_path_policy
from ._pydantic_args import parse_tool_args, validate_non_empty
from .contracts import ToolCall, ToolDefinition, ToolEffect, ToolResult, is_read_tier
from .guidance import guidance_for_tool
from .output import _ARTIFACT_ID_PATTERN

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


def _render_tool_documentation(path: str, *, context: ToolContext) -> _ReadOutcome:
    tool_name = path[len(VOIDCODE_TOOL_DOC_PREFIX) :].strip()
    if not tool_name:
        raise ValueError("voidcode://tool/<name> requires a tool name")
    context.require_session_id()
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
    effects = sorted(effect.value for effect in definition.effects)
    sections.append("effects: " + ", ".join(effects))
    read_only = is_read_tier(definition.effects)
    sections.append(f"read_only: {str(read_only).lower()}")
    content = "\n".join(sections).strip()
    return _ReadOutcome(
        content=f"Read documentation for tool {tool_name}.",
        data={
            "path": path,
            "type": "tool_documentation",
            "tool_name": definition.name,
            "effects": effects,
            "read_only": read_only,
            "guidance": guidance,
            "input_schema": definition.input_schema,
            "raw_content": content,
        },
    )


def _render_artifact(path: str, *, context: ToolContext, offset: int, limit: int) -> _ReadOutcome:
    """Render a bounded slice of a spilled tool-output artifact by URI.

    The artifact is resolved through the runtime's session-validated reader
    (``ToolContext.artifact``), which applies the session and
    artifact-path guards; the URI never falls through to workspace path
    resolution.
    """

    artifact_id = path[len(VOIDCODE_ARTIFACT_PREFIX) :].strip()
    if not artifact_id:
        raise ValueError("voidcode://artifact/<id> requires an artifact id")
    if _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
        raise ValueError(f"invalid artifact id: {artifact_id}")
    caller_session_id = context.require_session_id()
    facade = context.artifact
    if facade is None:
        raise ValueError("read cannot resolve voidcode://artifact URLs without a runtime artifact reader")
    result = facade.read_artifact(
        caller_session_id=caller_session_id,
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
            "line_count": line_count,
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset,
            "truncated": truncated,
            "partial": truncated,
            "byte_count": len(content.encode("utf-8")),
            "raw_content": content,
        },
    )


def _render_transcript(path: str, *, context: ToolContext, limit: int) -> _ReadOutcome:
    """Render a bounded, payload-stripped transcript of a session by URI.

    The transcript is resolved through the runtime's lineage-guarded reader
    (``ToolContext.transcript``): the caller may read its own
    session or a direct child session, never an unrelated session. Per event
    only ``sequence``, ``event_type``, and ``source`` are returned; raw tool
    output payloads are not included.
    """

    session_id = path[len(VOIDCODE_TRANSCRIPT_PREFIX) :].strip()
    if not session_id:
        raise ValueError("voidcode://transcript/<session_id> requires a session id")
    if "/" in session_id:
        raise ValueError("session_id must not contain '/'")
    caller_session_id = context.require_session_id()
    facade = context.transcript
    if facade is None:
        raise ValueError("read cannot resolve voidcode://transcript URLs without a runtime transcript reader")
    result = facade.read_transcript(caller_session_id=caller_session_id, session_id=session_id, limit=limit)
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
        content=content,
        data={
            "path": label,
            "type": "directory",
            "entry_count": entry_count,
            "truncated": truncated,
            "partial": truncated,
            "raw_content": rendered,
        },
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
    return _ReadOutcome(
        content=f"Read {len(rendered_lines)} line(s) from {label}" + ("; output is truncated." if content_truncated else "."),
        data={
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
            "raw_content": "\n".join(rendered_lines),
        },
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
            content=f"Listed {entry_count} entr{'y' if entry_count == 1 else 'ies'} in {label}" + ("; listing is truncated." if truncated else "."),
            data={
                "path": label,
                "type": "archive_listing",
                "entry_count": entry_count,
                "truncated": truncated,
                "partial": truncated,
                "raw_content": rendered,
            },
        )

    if normalized_inner not in names:
        raise ValueError(f"archive member not found: {normalized_inner}")
    raw = reader(normalized_inner)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _ReadOutcome(
            content=f"{label} is not UTF-8 text ({len(raw)} bytes); binary archive members are not decoded.",
            data={
                "path": label,
                "type": "archive_binary",
                "byte_count": len(raw),
                "truncated": False,
                "partial": False,
                "raw_content": "",
            },
        )
    return _render_archive_lines(text, label=label, offset=offset, limit=limit)


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
                    "Internal URLs: voidcode://tool/<name> reads a tool's guidance and input schema; "
                    "voidcode://rule/<name> reads a bounded workspace rule catalog entry; "
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
                "maximum": DEFAULT_READ_LIMIT,
                "description": "Maximum lines to return; use data.next_offset to continue when truncated.",
            },
            "required": ["path"],
        },
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        args = parse_tool_args(
            ReadArgs,
            {
                "path": call.arguments.get("path"),
                "offset": call.arguments.get("offset"),
                "limit": call.arguments.get("limit"),
            },
            tool_name=self.definition.name,
        )

        if args.path.startswith(RULE_URI_PREFIX):
            reader = context.read_rule
            if reader is None:
                raise RuntimeError("read requires an explicit rule reader for voidcode://rule URLs")
            data = reader(
                args.path,
                workspace=context.require_workspace(),
                offset=args.offset or 1,
                limit=args.limit or DEFAULT_READ_LIMIT,
            )
            truncated = bool(data["truncated"])
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=(f"Read rule {data['rule']} from {args.path}" + ("; output is truncated; continue with next_offset." if truncated else ".")),
                data=data,
                truncated=truncated,
                partial=bool(data["partial"]),
            )
        if args.path.startswith(VOIDCODE_TOOL_DOC_PREFIX):
            outcome = _render_tool_documentation(args.path, context=context)
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
                context=context,
                offset=args.offset or 1,
                limit=args.limit or DEFAULT_READ_LIMIT,
            )
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=outcome.content,
                data=outcome.data,
                truncated=bool(outcome.data["truncated"]),
                partial=bool(outcome.data["partial"]),
            )

        if args.path.startswith(VOIDCODE_TRANSCRIPT_PREFIX):
            outcome = _render_transcript(
                args.path,
                context=context,
                limit=args.limit or DEFAULT_TRANSCRIPT_LIMIT,
            )
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=outcome.content,
                data=outcome.data,
                truncated=bool(outcome.data["transcript_truncated"]),
                partial=bool(outcome.data["transcript_truncated"]),
            )

        workspace = context.require_workspace()
        resolution = resolve_workspace_path_policy(
            workspace=workspace,
            raw_path=args.path,
            allow_outside_workspace=True,
        )
        candidate = resolution.candidate
        relative_path = str(candidate.resolve()) if resolution.is_external else resolution.relative_path

        offset = args.offset or 1
        limit = args.limit or DEFAULT_READ_LIMIT

        archive = _split_archive_path(args.path)
        if archive is not None:
            archive_path, inner, family = archive
            resolved_archive = resolve_workspace_path_policy(
                workspace=workspace,
                raw_path=archive_path,
                allow_outside_workspace=True,
            )
            if not resolved_archive.candidate.is_file():
                raise ValueError(f"read target does not exist: {archive_path}")
            outcome = _render_archive(resolved_archive.candidate, inner, family=family, label=args.path, offset=offset, limit=limit)
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=outcome.content,
                data=outcome.data,
                truncated=bool(outcome.data["truncated"]),
                partial=bool(outcome.data["partial"]),
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
            outcome = _render_directory(candidate, label=relative_path)
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=outcome.content,
                data=outcome.data,
                truncated=bool(outcome.data["truncated"]),
                partial=bool(outcome.data["partial"]),
            )

        if not candidate.is_file():
            raise ValueError(f"read only supports regular files: {args.path}")

        outcome = _render_file(candidate, relative_path=relative_path, offset=offset, limit=limit)

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=outcome.content,
            data=outcome.data,
            truncated=bool(outcome.data["truncated"]),
            partial=bool(outcome.data["partial"]),
        )
