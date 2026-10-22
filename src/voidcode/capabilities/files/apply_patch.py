from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.tools import ResourceChange, TextOutput, ToolSuccess

from .guards import enforce_read_before_write, enforce_seen_lines, enforce_seen_whole_file
from .repair import format_text_repair_hints, raise_tool_diagnostic

_CHANGE_OPERATIONS: dict[str, Literal["create", "update", "delete", "move"]] = {"A": "create", "M": "update", "D": "delete", "R": "move"}


@dataclass(frozen=True)
class _MarkerChunk:
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    change_context: str | None = None
    is_end_of_file: bool = False


@dataclass(frozen=True)
class _MarkerHunk:
    action: str
    path: str
    contents: str = ""
    move_path: str | None = None
    chunks: tuple[_MarkerChunk, ...] = ()


@dataclass(frozen=True)
class _PreparedMarkerChange:
    status: str
    path: str
    content: str = ""
    old_path: str | None = None


@dataclass(frozen=True, slots=True)
class PatchChange:
    path: str
    status: str
    old_path: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"path": self.path, "status": self.status}
        if self.old_path is not None:
            payload["old_path"] = self.old_path
        return payload


@dataclass(frozen=True, slots=True)
class ApplyPatchResultBody:
    changes: tuple[PatchChange, ...]
    count: int
    diagnostics: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(own_json_object(item) for item in self.diagnostics))

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "changes": [change.as_payload() for change in self.changes],
            "count": self.count,
        }
        if self.diagnostics:
            payload["diagnostics"] = [json_wire_object(item) for item in self.diagnostics]
        return payload


def _patch_change_from_payload(payload: Mapping[str, object]) -> PatchChange:
    path = payload.get("path")
    status = payload.get("status")
    old_path = payload.get("old_path")
    if not isinstance(path, str) or not isinstance(status, str) or (old_path is not None and not isinstance(old_path, str)):
        raise TypeError("Invalid internal patch change")
    return PatchChange(path=path, status=status, old_path=old_path)


def _strip_heredoc(input_text: str) -> str:
    heredoc_match = re.match(
        r"^(?:cat\s+)?<<['\"]?(\w+)['\"]?\s*\n([\s\S]*?)\n\1\s*$",
        input_text,
    )
    if heredoc_match is None:
        return input_text
    return heredoc_match.group(2)


def _looks_like_marker_patch(patch_text: str) -> bool:
    lines = _strip_heredoc(patch_text.strip()).split("\n")
    envelope_lines = [line.strip() for line in lines if line.strip()]
    return len(envelope_lines) >= 2 and envelope_lines[0] == "*** Begin Patch" and envelope_lines[-1] == "*** End Patch"


def _parse_marker_header(lines: list[str], index: int) -> tuple[str, str, str | None, int] | None:
    line = lines[index]
    if line.startswith("*** Add File:"):
        path = line[len("*** Add File:") :].strip()
        return ("add", path, None, index + 1) if path else None
    if line.startswith("*** Delete File:"):
        path = line[len("*** Delete File:") :].strip()
        return ("delete", path, None, index + 1) if path else None
    if not line.startswith("*** Update File:"):
        return None

    path = line[len("*** Update File:") :].strip()
    move_path: str | None = None
    next_index = index + 1
    if next_index < len(lines) and lines[next_index].startswith("*** Move to:"):
        move_path = lines[next_index][len("*** Move to:") :].strip()
        next_index += 1
    return ("update", path, move_path, next_index) if path else None


def _parse_marker_add_content(lines: list[str], index: int) -> tuple[str, int]:
    current = index
    content_lines: list[str] = []
    while current < len(lines) and not lines[current].startswith("***"):
        line = lines[current]
        if not line.startswith("+"):
            raise ValueError(f"Add File content lines must start with '+': {line}")
        content_lines.append(line[1:])
        current += 1
    return "\n".join(content_lines), current


def _parse_marker_update_chunks(lines: list[str], index: int) -> tuple[tuple[_MarkerChunk, ...], int]:
    chunks: list[_MarkerChunk] = []
    current = index
    while current < len(lines) and not lines[current].startswith("***"):
        if not lines[current].startswith("@@"):
            current += 1
            continue

        context = lines[current][2:].strip() or None
        current += 1
        old_lines: list[str] = []
        new_lines: list[str] = []
        is_end_of_file = False
        while current < len(lines) and not lines[current].startswith("@@") and not lines[current].startswith("***"):
            change_line = lines[current]
            if change_line == "*** End of File":
                is_end_of_file = True
                current += 1
                break
            if change_line.startswith(" "):
                content = change_line[1:]
                old_lines.append(content)
                new_lines.append(content)
            elif change_line.startswith("-"):
                old_lines.append(change_line[1:])
            elif change_line.startswith("+"):
                new_lines.append(change_line[1:])
            current += 1
        chunks.append(
            _MarkerChunk(
                old_lines=tuple(old_lines),
                new_lines=tuple(new_lines),
                change_context=context,
                is_end_of_file=is_end_of_file,
            )
        )
    return tuple(chunks), current


def _parse_marker_patch(patch_text: str) -> tuple[_MarkerHunk, ...]:
    lines = _strip_heredoc(patch_text.strip()).split("\n")
    begin_index = next((index for index, line in enumerate(lines) if line.strip()), -1)
    end_index = next(
        (index for index in range(len(lines) - 1, -1, -1) if lines[index].strip()),
        -1,
    )
    if begin_index == -1 or end_index == -1 or begin_index >= end_index:
        raise ValueError("Invalid patch format: missing Begin/End markers")
    if lines[begin_index].strip() != "*** Begin Patch" or lines[end_index].strip() != "*** End Patch":
        raise ValueError("Invalid patch format: missing Begin/End markers")

    hunks: list[_MarkerHunk] = []
    current = begin_index + 1
    while current < end_index:
        current_line = lines[current]
        if not current_line.strip():
            current += 1
            continue
        header = _parse_marker_header(lines, current)
        if header is None:
            raise ValueError(f"malformed patch: expected a file operation (*** Add File / *** Update File / *** Delete File), got: {current_line}")

        action, path, move_path, next_index = header
        if action == "add":
            contents, current = _parse_marker_add_content(lines, next_index)
            hunks.append(_MarkerHunk(action="add", path=path, contents=contents))
        elif action == "delete":
            hunks.append(_MarkerHunk(action="delete", path=path))
            current = next_index
        else:
            chunks, current = _parse_marker_update_chunks(lines, next_index)
            hunks.append(_MarkerHunk(action="update", path=path, move_path=move_path, chunks=chunks))

    if not hunks:
        raise ValueError("patch rejected: empty patch")
    return tuple(hunks)


def _seek_sequence(
    lines: list[str],
    pattern: tuple[str, ...],
    start_index: int,
    *,
    eof: bool,
) -> int:
    if not pattern:
        return -1

    if eof:
        from_end = len(lines) - len(pattern)
        if from_end >= start_index and all(lines[from_end + offset] == expected for offset, expected in enumerate(pattern)):
            return from_end
        return -1

    found = -1
    for line_index in range(start_index, len(lines) - len(pattern) + 1):
        if all(lines[line_index + offset] == expected for offset, expected in enumerate(pattern)):
            if found != -1:
                return -2
            found = line_index
    return found


def _derive_marker_update_content_from_text(
    original: str,
    *,
    file_path: Path,
    chunks: tuple[_MarkerChunk, ...],
) -> tuple[str, tuple[tuple[int, int], ...], int]:
    """Return (derived content, matched 1-based line ranges, line-count delta).

    ``matched_ranges`` are 1-based inclusive ranges (in the input content's
    coordinates) of the lines each chunk anchored on: the matched old lines for
    replacement chunks, and the context anchor line for context-anchored
    insertions. ``delta`` is the change in line count the chunks introduce.
    """
    final_newline = original.endswith("\n")
    original_lines = original.split("\n") if original else []
    if final_newline:
        original_lines.pop()

    replacements: list[tuple[int, int, tuple[str, ...]]] = []
    matched_ranges: list[tuple[int, int]] = []
    line_index = 0
    for chunk in chunks:
        if chunk.change_context is not None:
            context_index = _seek_sequence(
                original_lines,
                (chunk.change_context,),
                line_index,
                eof=False,
            )
            if context_index == -2:
                raise_tool_diagnostic(
                    message=f"Found multiple exact matches for context '{chunk.change_context}' in {file_path}.",
                    error_kind="ambiguous_match",
                    reason="ambiguous_marker_context",
                    retry_guidance="Re-read the file and add enough marker context to identify one exact location.",
                    details={"path": str(file_path), "context": chunk.change_context},
                )
            if context_index == -1:
                raise ValueError(f"Failed to find context '{chunk.change_context}' in {file_path}")
            line_index = context_index + 1

        if not chunk.old_lines:
            insert_index = len(original_lines) if chunk.is_end_of_file else line_index
            replacements.append((insert_index, 0, chunk.new_lines))
            if chunk.change_context is not None:
                matched_ranges.append((context_index + 1, context_index + 1))
            line_index = insert_index
            continue

        pattern = chunk.old_lines
        new_slice = chunk.new_lines
        found = _seek_sequence(original_lines, pattern, line_index, eof=chunk.is_end_of_file)
        if found == -2:
            expected = "\n".join(chunk.old_lines)
            raise_tool_diagnostic(
                message=f"Found multiple exact matches for expected lines in {file_path}:\n{expected}",
                error_kind="ambiguous_match",
                reason="ambiguous_marker_match",
                retry_guidance="Re-read the file and add enough marker context to identify one exact location.",
                details={"path": str(file_path), "expected_preview": expected[:500]},
            )
        if found == -1:
            expected = "\n".join(chunk.old_lines)
            lines = [
                f"Failed to find expected lines in {file_path}:",
                expected,
                "Retry guidance: re-read this file and rebuild the patch hunk from current text.",
            ]
            hints = format_text_repair_hints(
                content="\n".join(original_lines),
                expected=expected,
                label="Nearest current file context",
            )
            if hints:
                lines.extend(hints)
            raise_tool_diagnostic(
                message="\n".join(lines),
                error_kind="tool_input_mismatch",
                reason="expected_lines_not_found",
                retry_guidance=("Re-read the target file and rebuild this patch hunk from current content."),
                details={
                    "path": str(file_path),
                    "expected_preview": expected[:500],
                    "has_candidate_matches": bool(hints),
                },
            )
        replacements.append((found, len(pattern), new_slice))
        matched_ranges.append((found + 1, found + len(pattern)))
        line_index = found + len(pattern)

    next_lines = list(original_lines)
    for start, old_len, new_segment in sorted(replacements, reverse=True):
        next_lines[start : start + old_len] = list(new_segment)
    delta = len(next_lines) - len(original_lines)
    content = "\n".join(next_lines)
    if final_newline and next_lines:
        content += "\n"
    return content, tuple(matched_ranges), delta


def _resolve_patch_path(workspace: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    return candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()


def _verify_patch_expected_hashes(
    *,
    changes: list[dict[str, object]],
    expected_hashes: dict[str, str] | None,
    workspace: Path,
    tool_name: str = "edit",
) -> None:
    """Verify expected hashes for every existing file the patch modifies, before any write."""
    normalized_hashes: dict[str, str] = {}
    if expected_hashes:
        for key, value in expected_hashes.items():
            normalized_hashes[_resolve_patch_path(workspace, key).as_posix()] = value

    missing_paths: list[str] = []
    affected_paths: list[str] = []
    for change in changes:
        status = change.get("status")
        if status == "A":
            continue
        raw_path = change.get("old_path") if status == "R" else change.get("path")
        if not isinstance(raw_path, str):
            continue
        resolved = _resolve_patch_path(workspace, raw_path)
        if not resolved.is_file():
            continue
        affected_paths.append(raw_path)
        expected_hash = normalized_hashes.get(resolved.as_posix())
        if expected_hash is None:
            missing_paths.append(raw_path)
            continue
        actual_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise_tool_diagnostic(
                message=f"{tool_name} rejected because {raw_path} changed since it was read (stale edit).",
                error_kind="stale_edit",
                reason="content_hash_mismatch",
                retry_guidance=(
                    "Read the affected file(s) again, use the SHA-256 content hashes shown in the read output, "
                    f"then retry {tool_name} with an updated expectedHashes map."
                ),
                details={
                    "path": raw_path,
                    "expected_hash": expected_hash,
                    "actual_hash": actual_hash,
                    "affected_paths": affected_paths,
                },
            )
    if missing_paths:
        raise_tool_diagnostic(
            message=(
                f"{tool_name} requires an expectedHashes entry for every existing file the patch "
                f"modifies; missing: {', '.join(sorted(missing_paths))}."
            ),
            error_kind="tool_input_mismatch",
            reason="missing_expected_hash",
            retry_guidance=(
                "Use read on each affected path, copy its SHA-256 content hash from the output into "
                f"expectedHashes (relative path -> hash), then retry {tool_name}."
            ),
            details={
                "affected_paths": affected_paths,
                "missing_paths": sorted(missing_paths),
            },
        )


def _apply_marker_patch(
    patch_text: str,
    *,
    context: ToolContext,
    workspace: Path,
    expected_hashes: dict[str, str] | None = None,
    tool_name: str = "edit",
) -> ToolSuccess[ApplyPatchResultBody]:
    hunks = _parse_marker_patch(patch_text)
    prepared: list[_PreparedMarkerChange] = []
    planned_add_paths: set[str] = set()
    staged_contents: dict[str, str] = {}
    ranges_by_source: dict[str, list[tuple[int, int]]] = {}
    shift_by_source: dict[str, int] = {}
    for hunk in hunks:
        if hunk.move_path is not None and hunk.move_path == hunk.path:
            raise ValueError(f"Move destination must differ from source: {hunk.move_path}")

        if hunk.action == "add":
            target = workspace / hunk.path
            if target.exists() or hunk.path in planned_add_paths:
                raise ValueError(f"Add File destination already exists: {hunk.path}")
            planned_add_paths.add(hunk.path)
            staged_contents[hunk.path] = hunk.contents
            prepared.append(_PreparedMarkerChange(status="A", path=hunk.path, content=hunk.contents))
        elif hunk.action == "delete":
            target = workspace / hunk.path
            if not target.exists():
                raise ValueError(f"Failed to read file for deletion: {target}")
            staged_contents.pop(hunk.path, None)
            prepared.append(_PreparedMarkerChange(status="D", path=hunk.path))
        else:
            source_file = workspace / hunk.path
            source_content = staged_contents.get(hunk.path)
            if source_content is None:
                try:
                    source_content = source_file.read_text(encoding="utf-8")
                except FileNotFoundError as exc:
                    raise ValueError(f"Failed to read file to update: {source_file}") from exc
            content, matched_ranges, delta = _derive_marker_update_content_from_text(
                source_content,
                file_path=source_file,
                chunks=hunk.chunks,
            )
            shift = shift_by_source.get(hunk.path, 0)
            original_ranges = [(start - shift, end - shift) for start, end in matched_ranges if end - shift >= 1]
            ranges_by_source.setdefault(hunk.path, []).extend(original_ranges)
            shift_by_source[hunk.path] = shift + delta
            if hunk.move_path is None:
                staged_contents[hunk.path] = content
                prepared.append(_PreparedMarkerChange(status="M", path=hunk.path, content=content))
            else:
                source_path = (workspace / hunk.path).resolve()
                destination_path = (workspace / hunk.move_path).resolve()
                if source_path == destination_path:
                    raise ValueError(f"Move destination must differ from source: {hunk.move_path}")
                if destination_path.exists():
                    raise ValueError(f"Move destination already exists: {hunk.move_path}")
                staged_contents.pop(hunk.path, None)
                staged_contents[hunk.move_path] = content
                prepared.append(
                    _PreparedMarkerChange(
                        status="R",
                        path=hunk.move_path,
                        content=content,
                        old_path=hunk.path,
                    )
                )

    for change in prepared:
        if change.status == "A":
            continue
        guard_path = change.old_path or change.path
        target = _resolve_patch_path(workspace, guard_path)
        is_external = not target.is_relative_to(workspace.resolve())
        display_path = target.as_posix() if is_external else target.relative_to(workspace.resolve()).as_posix()
        enforce_read_before_write(
            context=context,
            tool_name=tool_name,
            raw_path=guard_path,
            candidate=target,
            display_path=display_path,
        )

    _verify_patch_expected_hashes(
        changes=[
            {
                "path": change.path,
                "status": change.status,
                **({"old_path": change.old_path} if change.old_path is not None else {}),
            }
            for change in prepared
        ],
        expected_hashes=expected_hashes,
        workspace=workspace,
        tool_name=tool_name,
    )

    for change in prepared:
        if change.status == "A":
            continue
        guard_path = change.old_path or change.path
        target = _resolve_patch_path(workspace, guard_path)
        is_external = not target.is_relative_to(workspace.resolve())
        display_path = target.as_posix() if is_external else target.relative_to(workspace.resolve()).as_posix()
        if change.status == "D":
            enforce_seen_whole_file(
                context=context,
                tool_name=tool_name,
                raw_path=guard_path,
                candidate=target,
                display_path=display_path,
            )
            continue
        for start_line, end_line in ranges_by_source.get(guard_path, ()):
            enforce_seen_lines(
                context=context,
                tool_name=tool_name,
                raw_path=guard_path,
                candidate=target,
                display_path=display_path,
                start_line=start_line,
                end_line=end_line,
            )

    for change in prepared:
        target = workspace / change.path
        if change.status in {"A", "M", "R"}:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.content, encoding="utf-8", newline="\n")
        if change.status in {"D", "R"}:
            old_target = workspace / (change.old_path or change.path)
            old_target.unlink()

    changes: list[PatchChange] = []
    summary_lines: list[str] = []
    for change in prepared:
        target = _resolve_patch_path(workspace, change.path)
        display_path = target.as_posix() if not target.is_relative_to(workspace.resolve()) else target.relative_to(workspace.resolve()).as_posix()
        old_target = _resolve_patch_path(workspace, change.old_path) if change.old_path is not None else None
        display_old_path = (
            old_target.as_posix()
            if old_target is not None and not old_target.is_relative_to(workspace.resolve())
            else old_target.relative_to(workspace.resolve()).as_posix()
            if old_target is not None
            else None
        )
        changes.append(PatchChange(path=display_path, status=change.status, old_path=display_old_path))
        if change.status == "R":
            summary_lines.append(f"M {display_old_path} -> {display_path}")
        else:
            summary_lines.append(f"{change.status} {display_path}")

    return ToolSuccess(
        tool_name=tool_name,
        output=TextOutput("\n".join(summary_lines)),
        body=ApplyPatchResultBody(changes=tuple(changes), count=len(changes)),
        changes=tuple(ResourceChange(c.path, _CHANGE_OPERATIONS[c.status], c.old_path) for c in changes),
    )


def _decode_c_quoted_path(token: str) -> str | None:
    if not token.startswith('"'):
        return token if '"' not in token else None
    if len(token) < 2 or not token.endswith('"'):
        return None
    raw = token[1:-1]
    decoded: bytearray = bytearray()
    index = 0
    escapes = {
        "a": b"\a",
        "b": b"\b",
        "f": b"\f",
        "n": b"\n",
        "r": b"\r",
        "t": b"\t",
        "v": b"\v",
        "\\": b"\\",
        '"': b'"',
    }
    while index < len(raw):
        char = raw[index]
        if char != "\\":
            decoded.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(raw):
            return None
        escaped = raw[index]
        if escaped in escapes:
            decoded.extend(escapes[escaped])
            index += 1
            continue
        if escaped not in "01234567":
            return None
        end = index + 1
        while end < len(raw) and end < index + 3 and raw[end] in "01234567":
            end += 1
        decoded.append(int(raw[index:end], 8))
        index = end
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _consume_path_token(text: str) -> tuple[str, int] | None:
    if not text:
        return None
    if text.startswith('"'):
        escaped = False
        index = 1
        while index < len(text):
            char = text[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                decoded = _decode_c_quoted_path(text[: index + 1])
                return (decoded, index + 1) if decoded is not None else None
            index += 1
        return None
    index = 0
    while index < len(text) and not text[index].isspace():
        if text[index] == '"':
            return None
        index += 1
    return (text[:index], index) if index else None


def _strip_diff_prefix(path_text: str) -> str:
    path = path_text.strip()
    if path.startswith('"'):
        decoded = _decode_c_quoted_path(path)
        if decoded is not None:
            path = decoded
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _parse_diff_git_paths(line: str) -> tuple[str, str] | None:
    prefix = "diff --git "
    if not line.startswith(prefix):
        return None
    remainder = line[len(prefix) :]
    if remainder.startswith('"'):
        first = _consume_path_token(remainder)
        if first is None:
            return None
        old_token, index = first
        while index < len(remainder) and remainder[index].isspace():
            index += 1
        second = _consume_path_token(remainder[index:])
        if second is None:
            return None
        new_token, consumed = second
        if remainder[index + consumed :].strip():
            return None
    else:
        split_index = remainder.rfind(" b/")
        if split_index <= 0:
            return None
        old_token = remainder[:split_index]
        new_token = remainder[split_index + 1 :]
        if not old_token.startswith("a/") or not new_token or '"' in old_token or '"' in new_token:
            return None
    if not old_token.startswith("a/") or not new_token.startswith("b/"):
        return None
    return old_token[2:], new_token[2:]


def _parse_patch_file_path(raw_value: str) -> str | None:
    value = raw_value.strip()
    if not value:
        return None
    if value.startswith('"'):
        token = _consume_path_token(value)
        if token is None:
            return None
        path, consumed = token
        remainder = value[consumed:]
        if remainder and not remainder.startswith("\t"):
            return None
        return path
    if '"' in value:
        return None
    return value.split("\t", 1)[0].rstrip()


def _quote_git_path(prefix: str, path: str) -> str:
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{prefix}/{escaped}"'


def _format_diff_git_line(old_path: str, new_path: str) -> str:
    quote_paths = any(ch in path for ch in (" ", "\t", '"', "\\") for path in (old_path, new_path))
    old_token = _quote_git_path("a", old_path) if quote_paths else f"a/{old_path}"
    new_token = _quote_git_path("b", new_path) if quote_paths else f"b/{new_path}"
    return f"diff --git {old_token} {new_token}"


def _format_patch_marker_path(prefix: str, path: str) -> str:
    marker = f"{prefix}/{path}"
    if any(ch in path for ch in (" ", "\t", '"', "\\")):
        return _quote_git_path(prefix, path)
    return marker


def _normalize_diff_block(header: str, block_lines: list[str]) -> str:
    diff_paths = _parse_diff_git_paths(header)
    header_line = header
    if diff_paths is not None:
        old_path, new_path = diff_paths
        header_line = _format_diff_git_line(old_path, new_path)

    has_mode = any(line.startswith("old mode ") for line in block_lines) and any(line.startswith("new mode ") for line in block_lines)
    has_markers = any(line.startswith("--- ") or line.startswith("+++ ") for line in block_lines)
    has_hunks = any(line.startswith("@@ ") for line in block_lines)

    if has_mode and not has_markers and not has_hunks and diff_paths is not None:
        old_path, new_path = diff_paths
        old_marker = _format_patch_marker_path("a", old_path)
        new_marker = _format_patch_marker_path("b", new_path)
        inserted_block: list[str] = []
        inserted = False
        for line in block_lines:
            inserted_block.append(line)
            if not inserted and line.startswith("new mode "):
                inserted_block.append(f"--- {old_marker}")
                inserted_block.append(f"+++ {new_marker}")
                inserted = True
        if not inserted:
            inserted_block.extend([f"--- {old_marker}", f"+++ {new_marker}"])
        block_lines = inserted_block

    return "\n".join([header_line] + block_lines)


def _normalize_patch_text(patch_text: str) -> str:
    lines = patch_text.splitlines()
    normalized: list[str] = []
    current_header: str | None = None
    current_block: list[str] | None = None

    def flush_block() -> None:
        nonlocal current_header, current_block
        if current_header is None or current_block is None:
            return
        normalized.append(_normalize_diff_block(current_header, current_block))
        current_header = None
        current_block = None

    for line in lines:
        if line.startswith("diff --git "):
            flush_block()
            current_header = line
            current_block = []
            continue
        if current_block is None:
            normalized.append(line)
        else:
            current_block.append(line)

    flush_block()
    result = "\n".join(normalized)
    if patch_text.endswith("\n"):
        result += "\n"
    return result


def _run_git_command(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("git is required to apply an edit patch") from exc


def _format_patch_error(error: str, patch_text: str) -> str:
    if "No valid patches in input" in error or "patch with only garbage" in error:
        return _invalid_patch_text_error(patch_text, detail=error)
    match = re.search(r"(?:line\s+|:)(\d+)(?:\n|$)", error)
    if match is None:
        return error
    line_number = int(match.group(1))
    lines = patch_text.splitlines()
    start = max(1, line_number - 4)
    end = min(len(lines), line_number + 4)
    context = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1))
    hint = ""
    if 1 <= line_number <= len(lines) and lines[line_number - 1].startswith("diff --git "):
        hint = (
            "\nHint: a new file diff began where git was still parsing the previous hunk. "
            "Check the previous @@ header line counts, or use the structured "
            "*** Begin Patch / *** Add File envelope to avoid manual hunk counts."
        )
    return f"{error}\nPatch context near line {line_number}:\n{context}{hint}"


def _invalid_patch_text_error(patch_text: str, *, detail: str | None = None) -> str:
    stripped = _strip_heredoc(patch_text).strip()
    if not stripped:
        base = "edit received empty patch text."
    else:
        base = "edit received malformed patch text: no valid file operations were found."
    guidance = (
        "\nProvide exactly one structured patch envelope with at least one file operation, "
        "or a valid unified diff that git apply can parse."
        "\nExample:\n*** Begin Patch\n*** Add File: hello.txt\n+hello\n*** End Patch"
    )
    if detail:
        return f"{base}\nUnderlying error: {detail}{guidance}"
    return f"{base}{guidance}"


def _guard_changes_before_write(
    changes: list[dict[str, object]],
    *,
    context: ToolContext,
    workspace: Path,
    tool_name: str,
) -> None:
    for change in changes:
        status = change.get("status")
        guard_path = change.get("old_path") if status == "R" else change.get("path")
        if not isinstance(guard_path, str):
            continue
        requested_path = Path(guard_path)
        candidate = (requested_path if requested_path.is_absolute() else workspace / requested_path).resolve()
        is_external = not candidate.is_relative_to(workspace.resolve())
        if status == "A":
            continue
        enforce_read_before_write(
            context=context,
            tool_name=tool_name,
            raw_path=guard_path,
            candidate=candidate,
            display_path=candidate.as_posix() if is_external else candidate.relative_to(workspace.resolve()).as_posix(),
        )


def _enforce_patch_seen_ranges(
    *,
    context: ToolContext,
    patch_text: str,
    workspace: Path,
    tool_name: str,
) -> None:
    """Require every source line each unified-diff hunk touches to be seen."""
    patch_set = PatchSet(patch_text)

    for patched_file in patch_set:
        old_path = None if patched_file.source_file == "/dev/null" else _strip_diff_prefix(patched_file.source_file)
        new_path = None if patched_file.target_file == "/dev/null" else _strip_diff_prefix(patched_file.target_file)

        if old_path is not None and '"' in old_path:
            old_path = None
        if new_path is not None and '"' in new_path:
            new_path = None

        if old_path is None and new_path is not None:
            continue
        guard_path = old_path if old_path is not None else new_path
        if guard_path is None:
            continue
        candidate = _resolve_patch_path(workspace, guard_path)
        if not candidate.is_file():
            continue
        for hunk in patched_file:
            length = hunk.source_length
            if length <= 0:
                continue
            enforce_seen_lines(
                context=context,
                tool_name=tool_name,
                raw_path=guard_path,
                candidate=candidate,
                display_path=guard_path,
                start_line=hunk.source_start,
                end_line=hunk.source_start + length - 1,
            )


def _looks_like_mode_only_patch(patch_text: str) -> bool:
    inside_diff = False
    current_block_is_mode_only = False
    blocks: list[bool] = []

    def flush_block() -> None:
        nonlocal current_block_is_mode_only
        if inside_diff:
            blocks.append(current_block_is_mode_only)

    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            if inside_diff:
                flush_block()
            inside_diff = True
            current_block_is_mode_only = True
            continue
        if not inside_diff:
            continue
        if line.startswith("old mode ") or line.startswith("new mode "):
            continue
        if line.startswith("--- ") or line.startswith("+++ ") or line.startswith("@@ "):
            current_block_is_mode_only = False
            continue
        if line.strip() == "":
            continue
        current_block_is_mode_only = False

    if inside_diff:
        flush_block()

    return bool(blocks) and all(blocks)


def _mode_only_expected_modes(patch_text: str) -> dict[str, int]:
    """Return the target modes declared by each mode-only diff block."""
    expected_modes: dict[str, int] = {}
    current_paths: tuple[str, str] | None = None
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            current_paths = _parse_diff_git_paths(line)
            continue
        if current_paths is None or not line.startswith("new mode "):
            continue
        try:
            mode = int(line[len("new mode ") :].strip(), 8)
        except ValueError:
            continue
        expected_modes[current_paths[1]] = mode & 0o7777
    return expected_modes


def _capture_mode_only_state(
    *,
    changes: list[dict[str, object]],
    workspace: Path,
) -> dict[str, tuple[bool, int | None]]:
    state: dict[str, tuple[bool, int | None]] = {}
    for change in changes:
        path_value = change.get("path")
        if not isinstance(path_value, str):
            continue
        candidate = _resolve_patch_path(workspace, path_value)
        try:
            state[path_value] = (True, candidate.stat().st_mode & 0o7777)
        except FileNotFoundError:
            state[path_value] = (False, None)
    return state


def _verify_mode_only_change(
    *,
    changes: list[dict[str, object]],
    expected_modes: dict[str, int],
    before: dict[str, tuple[bool, int | None]],
    workspace: Path,
) -> None:
    """Reject a mode-only success unless every declared mode changed on disk."""
    unchanged_paths: list[str] = []
    invalid_paths: list[str] = []
    for change in changes:
        path_value = change.get("path")
        if not isinstance(path_value, str):
            continue
        candidate = _resolve_patch_path(workspace, path_value)
        try:
            after = (True, candidate.stat().st_mode & 0o7777)
        except FileNotFoundError:
            after = (False, None)
        if before.get(path_value) == after:
            unchanged_paths.append(path_value)
        expected_mode = expected_modes.get(path_value)
        if expected_mode is not None and after != (True, expected_mode):
            invalid_paths.append(path_value)

    if unchanged_paths or invalid_paths:
        detail_paths = sorted(set(unchanged_paths + invalid_paths))
        raise_tool_diagnostic(
            message=(f"edit patch did not make the requested mode-only filesystem change for: {', '.join(detail_paths)}."),
            error_kind="no_op",
            reason="mode_only_change_not_verified",
            retry_guidance=("Confirm the current file mode and rebuild the mode-only patch against the current file state."),
            details={
                "affected_paths": [path_value for path_value in (change.get("path") for change in changes) if isinstance(path_value, str)],
                "unchanged_paths": unchanged_paths,
                "invalid_mode_paths": invalid_paths,
            },
        )


def _dedupe_changes(changes: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for change in changes:
        key = (change.get("status"), change.get("old_path"), change.get("path"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(change)
    return deduped


def _changes_from_unified_diff(patch_text: str) -> list[dict[str, object]]:
    try:
        patch_set = PatchSet(patch_text)
    except UnidiffParseError, ValueError:
        return []

    changes: list[dict[str, object]] = []
    for patched_file in patch_set:
        old_path = None if patched_file.source_file == "/dev/null" else _strip_diff_prefix(patched_file.source_file)
        new_path = None if patched_file.target_file == "/dev/null" else _strip_diff_prefix(patched_file.target_file)

        if old_path is not None and '"' in old_path:
            old_path = None
        if new_path is not None and '"' in new_path:
            new_path = None

        if old_path is None and new_path is not None:
            changes.append({"path": new_path, "status": "A"})
        elif old_path is not None and new_path is None:
            changes.append({"path": old_path, "status": "D"})
        elif old_path is not None and new_path is not None and old_path != new_path:
            changes.append({"path": new_path, "old_path": old_path, "status": "R"})
        elif new_path is not None:
            changes.append({"path": new_path, "status": "M"})

    return changes


def _changes_from_patch_metadata(patch_text: str) -> list[dict[str, object]]:
    changes: list[dict[str, object]] = []
    block_old_path: str | None = None
    block_new_path: str | None = None
    patch_old_path: str | None = None

    def flush_block() -> None:
        if block_old_path is None and block_new_path is None:
            return
        if block_old_path is None and block_new_path is not None:
            changes.append({"path": block_new_path, "status": "A"})
        elif block_old_path is not None and block_new_path is None:
            changes.append({"path": block_old_path, "status": "D"})
        elif block_old_path == block_new_path:
            changes.append({"path": block_new_path, "status": "M"})
        else:
            changes.append({"path": block_new_path, "old_path": block_old_path, "status": "R"})

    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            flush_block()
            diff_paths = _parse_diff_git_paths(line)
            if diff_paths is None:
                block_old_path = None
                block_new_path = None
            else:
                block_old_path, block_new_path = diff_paths
            patch_old_path = None
            continue

        if line.startswith("rename from "):
            block_old_path = _parse_patch_file_path(line[len("rename from ") :])
            continue

        if line.startswith("rename to "):
            block_new_path = _parse_patch_file_path(line[len("rename to ") :])
            continue

        if line.startswith("--- "):
            old_marker = _parse_patch_file_path(line[4:])
            patch_old_path = None if old_marker in {None, "/dev/null"} else _strip_diff_prefix(old_marker)
            continue

        if not line.startswith("+++ "):
            continue

        new_marker = _parse_patch_file_path(line[4:])
        patch_new_path = None if new_marker in {None, "/dev/null"} else _strip_diff_prefix(new_marker)
        block_old_path = patch_old_path
        block_new_path = patch_new_path
        patch_old_path = None

    flush_block()

    return changes


def _changes_from_patch(patch_text: str) -> list[dict[str, object]]:
    changes = _changes_from_unified_diff(_normalize_patch_text(patch_text))
    changes.extend(_changes_from_patch_metadata(patch_text))
    return _dedupe_changes(changes)


def execute_patch(
    arguments: Mapping[str, object],
    *,
    context: ToolContext,
    tool_name: str = "edit",
) -> ToolSuccess[ApplyPatchResultBody]:
    workspace = context.require_workspace()
    patch_text = arguments.get("patch")
    if not isinstance(patch_text, str):
        raise ValueError(f"{tool_name} requires a string 'patch' argument")

    raw_expected_hashes = arguments.get("expectedHashes")
    if raw_expected_hashes is None:
        expected_hashes: dict[str, str] | None = None
    elif isinstance(raw_expected_hashes, Mapping) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_expected_hashes.items()
    ):
        expected_hashes = dict(raw_expected_hashes)
    else:
        raise ValueError(f"{tool_name} expectedHashes must be an object mapping path strings to SHA-256 hash strings")

    stripped_patch = _strip_heredoc(patch_text).strip()
    if not stripped_patch:
        raise_tool_diagnostic(
            message=_invalid_patch_text_error(patch_text),
            error_kind="parse_error",
            reason="empty_patch",
            retry_guidance=(
                "Provide a non-empty patch: a structured *** Begin Patch envelope with at least "
                "one file operation, or a unified diff that git apply can parse."
            ),
            details={"patch_empty": True},
        )

    if _looks_like_marker_patch(patch_text):
        return _apply_marker_patch(patch_text, workspace=workspace, expected_hashes=expected_hashes, context=context, tool_name=tool_name)

    normalized_patch = _normalize_patch_text(patch_text)
    changes = _changes_from_patch(patch_text)
    if not changes and not _looks_like_mode_only_patch(patch_text):
        raise_tool_diagnostic(
            message=_invalid_patch_text_error(patch_text),
            error_kind="parse_error",
            reason="invalid_patch_text",
            retry_guidance=(
                "Provide a structured *** Begin Patch envelope with at least one file operation, or a valid unified diff that git apply can parse."
            ),
            details={"affected_paths": [], "patch_invalid": True},
        )
    _guard_changes_before_write(changes, workspace=workspace, tool_name=tool_name, context=context)
    patch_path = workspace / ".voidcode_edit.patch"
    patch_path.write_text(normalized_patch, encoding="utf-8", newline="\n")
    try:
        check = _run_git_command(["git", "apply", "--check", str(patch_path)], workspace)
        if check.returncode != 0:
            error = check.stdout or "Patch check failed"
            raise_tool_diagnostic(
                message=_format_patch_error(error, normalized_patch),
                error_kind="parse_error",
                reason="patch_apply_failed",
                retry_guidance=(
                    "Re-read the affected file(s) with read and rebuild the patch hunks from "
                    f"the current content, then retry {tool_name}. Prefer the structured "
                    "*** Begin Patch envelope to avoid manual hunk counts."
                ),
                details={
                    "git_error": error,
                    "affected_paths": [c.get("path") for c in changes if isinstance(c.get("path"), str)],
                },
            )

        # Verify expected hashes for every existing file the patch modifies, before writing.
        _verify_patch_expected_hashes(
            changes=changes,
            expected_hashes=expected_hashes,
            workspace=workspace,
            tool_name=tool_name,
        )

        # Require every source line the hunks touch to have been revealed by read.
        _enforce_patch_seen_ranges(patch_text=normalized_patch, workspace=workspace, tool_name=tool_name, context=context)

        mode_only = _looks_like_mode_only_patch(patch_text)
        mode_only_before = _capture_mode_only_state(changes=changes, workspace=workspace) if mode_only else {}

        apply = _run_git_command(["git", "apply", str(patch_path)], workspace)
        if apply.returncode != 0:
            error = apply.stdout or "Patch apply failed"
            raise_tool_diagnostic(
                message=_format_patch_error(error, normalized_patch),
                error_kind="parse_error",
                reason="patch_apply_failed",
                retry_guidance=(
                    "Re-read the affected file(s) with read and rebuild the patch hunks from "
                    f"the current content, then retry {tool_name}. Prefer the structured "
                    "*** Begin Patch envelope to avoid manual hunk counts."
                ),
                details={
                    "git_error": error,
                    "affected_paths": [c.get("path") for c in changes if isinstance(c.get("path"), str)],
                },
            )

        if mode_only:
            _verify_mode_only_change(
                changes=changes,
                expected_modes=_mode_only_expected_modes(patch_text),
                before=mode_only_before,
                workspace=workspace,
            )

        summary_lines: list[str] = []
        for c in changes:
            if c.get("status") == "R":
                summary_lines.append(f"M {c['old_path']} -> {c['path']}")
            else:
                summary_lines.append(f"{c['status']} {c['path']}")

        content = "\n".join(summary_lines) if summary_lines else "patch applied"

        return ToolSuccess(
            tool_name=tool_name,
            output=TextOutput(content),
            changes=tuple(
                ResourceChange(c.path, _CHANGE_OPERATIONS[c.status], c.old_path) for c in (_patch_change_from_payload(change) for change in changes)
            ),
            body=ApplyPatchResultBody(
                changes=tuple(_patch_change_from_payload(change) for change in changes),
                count=len(changes),
            ),
        )
    finally:
        try:
            patch_path.unlink(missing_ok=True)
        except Exception:
            pass


__all__ = ["ApplyPatchResultBody", "PatchChange", "execute_patch"]
