from __future__ import annotations

import errno
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
from voidcode.sdk.tools import ConfirmedStop, OutputBounds, ResourceChange, TextOutput, ToolFailure, ToolResult, ToolSuccess

from ._replace import replace_file
from .errors import raise_tool_diagnostic

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
    expected_content: bytes | None = None


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

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "changes": [change.as_payload() for change in self.changes],
            "count": self.count,
        }
        return payload


def _patch_result(
    changes: list[PatchChange],
    *,
    tool_name: str,
    error: str | None = None,
    cancelled: bool = False,
) -> ToolResult:
    summary = "\n".join(f"M {change.old_path} -> {change.path}" if change.status == "R" else f"{change.status} {change.path}" for change in changes)
    body = ApplyPatchResultBody(changes=tuple(changes), count=len(changes))
    resources = tuple(ResourceChange(change.path, _CHANGE_OPERATIONS[change.status], change.old_path) for change in changes)
    if error is not None:
        return ToolFailure(
            tool_name=tool_name,
            error=error,
            output=TextOutput(f"{error}\n{summary}" if summary else error, bounds=OutputBounds(partial=True)),
            body=body,
            changes=resources,
            execution=ConfirmedStop(cancellation_signalled=cancelled),
        )
    return ToolSuccess(tool_name=tool_name, output=TextOutput(summary or "patch applied"), body=body, changes=resources)


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
) -> str:
    """Apply exact marker chunks in memory before committing file changes."""
    final_newline = original.endswith("\n")
    original_lines = original.split("\n") if original else []
    if final_newline:
        original_lines.pop()

    replacements: list[tuple[int, int, tuple[str, ...]]] = []
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
            raise_tool_diagnostic(
                message="\n".join(lines),
                error_kind="tool_input_mismatch",
                reason="expected_lines_not_found",
                retry_guidance=("Re-read the target file and rebuild this patch hunk from current content."),
                details={
                    "path": str(file_path),
                    "expected_preview": expected[:500],
                },
            )
        replacements.append((found, len(pattern), new_slice))
        line_index = found + len(pattern)

    next_lines = list(original_lines)
    for start, old_len, new_segment in sorted(replacements, reverse=True):
        next_lines[start : start + old_len] = list(new_segment)
    content = "\n".join(next_lines)
    if final_newline and next_lines:
        content += "\n"
    return content


def _resolve_patch_path(workspace: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    return candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()


def _verify_patch_expected_hashes(
    *,
    changes: list[PatchChange],
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
        status = change.status
        if status == "A":
            continue
        raw_path = change.old_path or change.path
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
    expected_hashes: dict[str, str] | None = None,
    tool_name: str = "edit",
) -> ToolResult:
    workspace = context.require_workspace().resolve()
    hunks = _parse_marker_patch(patch_text)
    prepared: list[_PreparedMarkerChange] = []
    staged_contents: dict[Path, bytes | None] = {}

    def current_content(path: Path) -> bytes | None:
        if path not in staged_contents:
            try:
                staged_contents[path] = path.read_bytes()
            except FileNotFoundError:
                staged_contents[path] = None
        return staged_contents[path]

    for hunk in hunks:
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return _patch_result([], tool_name=tool_name, error="patch cancelled before committing files", cancelled=True)
        source = _resolve_patch_path(workspace, hunk.path)
        expected_content = current_content(source)
        if hunk.action == "add":
            if expected_content is not None:
                raise ValueError(f"Add File destination already exists: {hunk.path}")
            staged_contents[source] = hunk.contents.encode("utf-8")
            prepared.append(_PreparedMarkerChange(status="A", path=source.as_posix(), content=hunk.contents))
        elif hunk.action == "delete":
            if expected_content is None:
                raise ValueError(f"Failed to read file for deletion: {source}")
            staged_contents[source] = None
            prepared.append(_PreparedMarkerChange(status="D", path=source.as_posix(), expected_content=expected_content))
        else:
            if expected_content is None:
                raise ValueError(f"Failed to read file to update: {source}")
            source_content = expected_content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            content = _derive_marker_update_content_from_text(source_content, file_path=source, chunks=hunk.chunks)
            if hunk.move_path is None:
                staged_contents[source] = content.encode("utf-8")
                prepared.append(_PreparedMarkerChange(status="M", path=source.as_posix(), content=content, expected_content=expected_content))
            else:
                destination = _resolve_patch_path(workspace, hunk.move_path)
                if source == destination:
                    raise ValueError(f"Move destination must differ from source: {hunk.move_path}")
                if current_content(destination) is not None:
                    raise ValueError(f"Move destination already exists: {hunk.move_path}")
                staged_contents[source] = None
                staged_contents[destination] = content.encode("utf-8")
                prepared.append(
                    _PreparedMarkerChange(
                        status="R",
                        path=destination.as_posix(),
                        content=content,
                        old_path=source.as_posix(),
                        expected_content=expected_content,
                    )
                )

    _verify_patch_expected_hashes(
        changes=[PatchChange(change.path, change.status, change.old_path) for change in prepared],
        expected_hashes=expected_hashes,
        workspace=workspace,
        tool_name=tool_name,
    )

    changes: list[PatchChange] = []
    for change in prepared:
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return _patch_result(changes, tool_name=tool_name, error="patch cancelled before committing the next operation", cancelled=True)
        target = Path(change.path)
        old_target = Path(change.old_path or change.path)
        display = target.relative_to(workspace).as_posix() if target.is_relative_to(workspace) else target.as_posix()
        old_display = old_target.relative_to(workspace).as_posix() if old_target.is_relative_to(workspace) else old_target.as_posix()
        try:
            if change.status in {"D", "R"} and (old_target.is_symlink() or old_target.read_bytes() != change.expected_content):
                raise OSError(errno.ESTALE, "File changed since patch validation", str(old_target))
            if change.status in {"A", "M", "R"}:
                target.parent.mkdir(parents=True, exist_ok=True)
                replace_file(target, change.content.encode("utf-8"), expected_content=change.expected_content if change.status == "M" else None)
                changes.append(PatchChange(display, "A" if change.status == "R" else change.status))
            if change.status in {"D", "R"}:
                old_target.unlink()
                if change.status == "R":
                    changes.pop()  # The created destination is now a completed move.
                changes.append(PatchChange(display, change.status, old_display if change.status == "R" else None))
        except OSError as exc:
            return _patch_result(changes, tool_name=tool_name, error=f"patch failed while committing {change.path}: {exc}")

    return _patch_result(changes, tool_name=tool_name)


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


def _run_git_command(args: list[str], cwd: Path, *, patch: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            input=patch,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("git is required to apply an edit patch") from exc


def _changes_from_unified_diff(patch_text: str) -> list[PatchChange]:
    try:
        patch_set = PatchSet(patch_text)
    except UnidiffParseError, ValueError:
        return []

    changes: list[PatchChange] = []
    for patched_file in patch_set:
        old_path = None if patched_file.source_file == "/dev/null" else _strip_diff_prefix(patched_file.source_file)
        new_path = None if patched_file.target_file == "/dev/null" else _strip_diff_prefix(patched_file.target_file)

        if old_path is not None and '"' in old_path:
            old_path = None
        if new_path is not None and '"' in new_path:
            new_path = None

        if old_path is None and new_path is not None:
            changes.append(PatchChange(path=new_path, status="A"))
        elif old_path is not None and new_path is None:
            changes.append(PatchChange(path=old_path, status="D"))
        elif old_path is not None and new_path is not None and old_path != new_path:
            changes.append(PatchChange(path=new_path, old_path=old_path, status="R"))
        elif new_path is not None:
            changes.append(PatchChange(path=new_path, status="M"))

    return changes


def _changes_from_patch_metadata(patch_text: str) -> list[PatchChange]:
    changes: list[PatchChange] = []
    block_old_path: str | None = None
    block_new_path: str | None = None
    patch_old_path: str | None = None

    def flush_block() -> None:
        if block_new_path is None:
            if block_old_path is not None:
                changes.append(PatchChange(path=block_old_path, status="D"))
        elif block_old_path is None:
            changes.append(PatchChange(path=block_new_path, status="A"))
        elif block_old_path == block_new_path:
            changes.append(PatchChange(path=block_new_path, status="M"))
        else:
            changes.append(PatchChange(path=block_new_path, old_path=block_old_path, status="R"))

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


def _changes_from_patch(patch_text: str) -> list[PatchChange]:
    changes = _changes_from_unified_diff(_normalize_patch_text(patch_text))
    changes.extend(_changes_from_patch_metadata(patch_text))
    return list(dict.fromkeys(changes))


def execute_patch(
    arguments: Mapping[str, object],
    *,
    context: ToolContext,
    tool_name: str = "edit",
) -> ToolResult:
    workspace = context.require_workspace()
    if context.abort_signal is not None and context.abort_signal.cancelled:
        return _patch_result([], tool_name=tool_name, error="patch cancelled before committing files", cancelled=True)
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
            message=f"{tool_name} received empty patch text.",
            error_kind="parse_error",
            reason="empty_patch",
            retry_guidance=(
                "Provide a non-empty patch: a structured *** Begin Patch envelope with at least "
                "one file operation, or a unified diff that git apply can parse."
            ),
            details={"patch_empty": True},
        )

    if _looks_like_marker_patch(patch_text):
        return _apply_marker_patch(patch_text, context=context, expected_hashes=expected_hashes, tool_name=tool_name)

    normalized_patch = _normalize_patch_text(patch_text)
    changes = _changes_from_patch(patch_text)
    if not changes:
        raise_tool_diagnostic(
            message=f"{tool_name} received malformed patch text: no valid file operations were found.",
            error_kind="parse_error",
            reason="invalid_patch_text",
            retry_guidance=(
                "Provide a structured *** Begin Patch envelope with at least one file operation, or a valid unified diff that git apply can parse."
            ),
            details={"affected_paths": [], "patch_invalid": True},
        )
    check = _run_git_command(["git", "apply", "--check", "-"], workspace, patch=normalized_patch)
    if check.returncode != 0:
        error = check.stdout or "Patch check failed"
        raise_tool_diagnostic(
            message=error,
            error_kind="parse_error",
            reason="patch_apply_failed",
            retry_guidance=(
                "Re-read the affected file(s) with read and rebuild the patch hunks from "
                f"the current content, then retry {tool_name}. Prefer the structured "
                "*** Begin Patch envelope to avoid manual hunk counts."
            ),
            details={
                "affected_paths": [c.path for c in changes],
            },
        )

    # Verify expected hashes for every existing file the patch modifies, before writing.
    _verify_patch_expected_hashes(
        changes=changes,
        expected_hashes=expected_hashes,
        workspace=workspace,
        tool_name=tool_name,
    )

    if context.abort_signal is not None and context.abort_signal.cancelled:
        return _patch_result([], tool_name=tool_name, error="patch cancelled before committing files", cancelled=True)
    apply = _run_git_command(["git", "apply", "-"], workspace, patch=normalized_patch)
    if apply.returncode != 0:
        error = apply.stdout or "Patch apply failed"
        raise_tool_diagnostic(
            message=error,
            error_kind="parse_error",
            reason="patch_apply_failed",
            retry_guidance=(
                "Re-read the affected file(s) with read and rebuild the patch hunks from "
                f"the current content, then retry {tool_name}. Prefer the structured "
                "*** Begin Patch envelope to avoid manual hunk counts."
            ),
            details={
                "affected_paths": [c.path for c in changes],
            },
        )

    return _patch_result(changes, tool_name=tool_name)


__all__ = ["ApplyPatchResultBody", "PatchChange", "execute_patch"]
