"""Apply marker patches through exact preflight and checked file commits.

Supports add/update/delete/move operations without external commands.
"""

from __future__ import annotations

import errno
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.tools import Change, ConfirmedStop, OutputBounds, TextOutput, ToolFailure, ToolResult, ToolSuccess

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
    resources = tuple(Change(change.path, _CHANGE_OPERATIONS[change.status], change.old_path) for change in changes)
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
        if not move_path:
            raise ValueError("malformed patch: Move to requires a destination path")
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
        if not lines[current].strip():
            current += 1
            continue
        if not lines[current].startswith("@@"):
            raise ValueError(f"malformed patch: expected an @@ update chunk, got: {lines[current]}")

        context = lines[current][2:].strip() or None
        current += 1
        old_lines: list[str] = []
        new_lines: list[str] = []
        is_end_of_file = False
        while current < len(lines) and not lines[current].startswith("@@"):
            change_line = lines[current]
            if change_line == "*** End of File":
                is_end_of_file = True
                current += 1
                break
            if change_line.startswith("***"):
                break
            if change_line.startswith(" "):
                content = change_line[1:]
                old_lines.append(content)
                new_lines.append(content)
            elif change_line.startswith("-"):
                old_lines.append(change_line[1:])
            elif change_line.startswith("+"):
                new_lines.append(change_line[1:])
            else:
                raise ValueError(f"malformed patch: update lines must start with space, + or -, got: {change_line}")
            current += 1
        if not old_lines and not new_lines:
            raise ValueError("malformed patch: empty update chunk")
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
    lines = patch_text.strip().replace("\r\n", "\n").split("\n")
    if len(lines) < 2 or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise ValueError("Invalid patch format: missing Begin/End markers")
    end_index = len(lines) - 1

    hunks: list[_MarkerHunk] = []
    current = 1
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
            if not chunks and move_path is None:
                raise ValueError("malformed patch: Update File requires update chunks or a move destination")
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
    for start, old_len, new_segment in reversed(replacements):
        next_lines[start : start + old_len] = list(new_segment)
    content = "\n".join(next_lines)
    if final_newline and next_lines:
        content += "\n"
    return content


def _resolve_patch_path(workspace: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    return candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()


def _apply_marker_patch(
    patch_text: str,
    *,
    context: ToolContext,
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
            content = expected_content.decode("utf-8")
            if hunk.chunks:
                content = _derive_marker_update_content_from_text(
                    content.replace("\r\n", "\n").replace("\r", "\n"), file_path=source, chunks=hunk.chunks
                )
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


def execute_patch(
    arguments: Mapping[str, object],
    *,
    context: ToolContext,
    tool_name: str = "edit",
) -> ToolResult:
    context.require_workspace()
    if context.abort_signal is not None and context.abort_signal.cancelled:
        return _patch_result([], tool_name=tool_name, error="patch cancelled before committing files", cancelled=True)
    patch_text = arguments.get("patch")
    if not isinstance(patch_text, str):
        raise ValueError(f"{tool_name} requires a string 'patch' argument")

    return _apply_marker_patch(patch_text, context=context, tool_name=tool_name)


__all__ = ["ApplyPatchResultBody", "PatchChange", "execute_patch"]
