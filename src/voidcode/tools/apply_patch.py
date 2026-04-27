from __future__ import annotations

import re
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from ..hook.config import RuntimeHooksConfig
from ._formatter import FormatterExecutor, formatter_diagnostics, formatter_payload
from .contracts import ToolCall, ToolDefinition, ToolResult


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
    contents: str | None = None
    move_path: str | None = None
    chunks: tuple[_MarkerChunk, ...] = ()


@dataclass(frozen=True)
class _PreparedMarkerChange:
    status: str
    path: str
    content: str | None = None
    old_path: str | None = None


def _assert_within_workspace(workspace: Path, rel_path: Path) -> None:
    root = workspace.resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("patch operation must affect paths inside the workspace")


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
    return (
        len(envelope_lines) >= 2
        and envelope_lines[0] == "*** Begin Patch"
        and envelope_lines[-1] == "*** End Patch"
    )


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
        if line.startswith("+"):
            content_lines.append(line[1:])
        current += 1
    return "\n".join(content_lines), current


def _parse_marker_update_chunks(
    lines: list[str], index: int
) -> tuple[tuple[_MarkerChunk, ...], int]:
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
        while (
            current < len(lines)
            and not lines[current].startswith("@@")
            and not lines[current].startswith("***")
        ):
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
    if (
        lines[begin_index].strip() != "*** Begin Patch"
        or lines[end_index].strip() != "*** End Patch"
    ):
        raise ValueError("Invalid patch format: missing Begin/End markers")

    hunks: list[_MarkerHunk] = []
    current = begin_index + 1
    while current < end_index:
        header = _parse_marker_header(lines, current)
        if header is None:
            current += 1
            continue

        action, path, move_path, next_index = header
        if action == "add":
            contents, current = _parse_marker_add_content(lines, next_index)
            hunks.append(_MarkerHunk(action="add", path=path, contents=contents))
        elif action == "delete":
            hunks.append(_MarkerHunk(action="delete", path=path))
            current = next_index
        else:
            chunks, current = _parse_marker_update_chunks(lines, next_index)
            hunks.append(
                _MarkerHunk(action="update", path=path, move_path=move_path, chunks=chunks)
            )

    if not hunks:
        raise ValueError("patch rejected: empty patch")
    return tuple(hunks)


def _normalize_match_line(line: str) -> str:
    normalized = unicodedata.normalize("NFKC", line.strip())
    return (
        normalized.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2026", "...")
        .replace("\u00a0", " ")
    )


def _seek_sequence(
    lines: list[str],
    pattern: tuple[str, ...],
    start_index: int,
    *,
    eof: bool,
) -> int:
    if not pattern:
        return -1

    def search_with(compare: Callable[[str, str], bool]) -> int:
        if eof:
            from_end = len(lines) - len(pattern)
            if from_end >= start_index and all(
                compare(lines[from_end + offset], expected)
                for offset, expected in enumerate(pattern)
            ):
                return from_end
        for line_index in range(start_index, len(lines) - len(pattern) + 1):
            if all(
                compare(lines[line_index + offset], expected)
                for offset, expected in enumerate(pattern)
            ):
                return line_index
        return -1

    comparators: tuple[Callable[[str, str], bool], ...] = (
        lambda left, right: left == right,
        lambda left, right: left.rstrip() == right.rstrip(),
        lambda left, right: left.strip() == right.strip(),
        lambda left, right: _normalize_match_line(left) == _normalize_match_line(right),
    )
    for comparator in comparators:
        found = search_with(comparator)
        if found != -1:
            return found
    return -1


def _derive_marker_update_content(file_path: Path, chunks: tuple[_MarkerChunk, ...]) -> str:
    try:
        original = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"Failed to read file to update: {file_path}") from exc

    original_lines = original.split("\n")
    if original_lines and original_lines[-1] == "":
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
        if found == -1 and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == "":
                new_slice = new_slice[:-1]
            found = _seek_sequence(original_lines, pattern, line_index, eof=chunk.is_end_of_file)
        if found == -1:
            expected = "\n".join(chunk.old_lines)
            raise ValueError(f"Failed to find expected lines in {file_path}:\n{expected}")
        replacements.append((found, len(pattern), new_slice))
        line_index = found + len(pattern)

    next_lines = list(original_lines)
    for start, old_len, new_segment in sorted(replacements, reverse=True):
        next_lines[start : start + old_len] = list(new_segment)
    if not next_lines or next_lines[-1] != "":
        next_lines.append("")
    return "\n".join(next_lines)


def _apply_marker_patch(patch_text: str, *, workspace: Path) -> ToolResult:
    hunks = _parse_marker_patch(patch_text)
    prepared: list[_PreparedMarkerChange] = []
    planned_add_paths: set[str] = set()
    for hunk in hunks:
        _assert_within_workspace(workspace, Path(hunk.path))
        if hunk.move_path is not None:
            _assert_within_workspace(workspace, Path(hunk.move_path))

        if hunk.action == "add":
            target = workspace / hunk.path
            if target.exists() or hunk.path in planned_add_paths:
                raise ValueError(f"Add File destination already exists: {hunk.path}")
            planned_add_paths.add(hunk.path)
            prepared.append(
                _PreparedMarkerChange(status="A", path=hunk.path, content=hunk.contents or "")
            )
        elif hunk.action == "delete":
            target = workspace / hunk.path
            if not target.exists():
                raise ValueError(f"Failed to read file for deletion: {target}")
            prepared.append(_PreparedMarkerChange(status="D", path=hunk.path))
        else:
            content = _derive_marker_update_content(workspace / hunk.path, hunk.chunks)
            if hunk.move_path is None:
                prepared.append(_PreparedMarkerChange(status="M", path=hunk.path, content=content))
            else:
                source_path = (workspace / hunk.path).resolve()
                destination_path = (workspace / hunk.move_path).resolve()
                if source_path == destination_path:
                    raise ValueError(f"Move destination must differ from source: {hunk.move_path}")
                if destination_path.exists():
                    raise ValueError(f"Move destination already exists: {hunk.move_path}")
                prepared.append(
                    _PreparedMarkerChange(
                        status="R",
                        path=hunk.move_path,
                        content=content,
                        old_path=hunk.path,
                    )
                )

    for change in prepared:
        target = workspace / change.path
        if change.status in {"A", "M", "R"}:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.content or "", encoding="utf-8", newline="\n")
        if change.status in {"D", "R"}:
            old_target = workspace / (change.old_path or change.path)
            old_target.unlink()

    changes: list[dict[str, object]] = []
    summary_lines: list[str] = []
    for change in prepared:
        if change.status == "R":
            changes.append({"path": change.path, "old_path": change.old_path, "status": "R"})
            summary_lines.append(f"M {change.old_path} -> {change.path}")
        else:
            changes.append({"path": change.path, "status": change.status})
            summary_lines.append(f"{change.status} {change.path}")

    return ToolResult(
        tool_name="apply_patch",
        status="ok",
        content="\n".join(summary_lines),
        data={"changes": changes, "count": len(changes)},
    )


def _strip_diff_prefix(path_text: str) -> str:
    if path_text.startswith('"a/') and path_text.endswith('"'):
        return path_text[3:-1]
    if path_text.startswith('"b/') and path_text.endswith('"'):
        return path_text[3:-1]
    if path_text.startswith("a/") or path_text.startswith("b/"):
        return path_text[2:]
    return path_text


def _parse_diff_git_paths(line: str) -> tuple[str, str] | None:
    quoted_prefix = 'diff --git "a/'
    if line.startswith(quoted_prefix):
        quoted_marker = '" "b/'
        split_index = line.find(quoted_marker, len(quoted_prefix))
        if split_index == -1 or not line.endswith('"'):
            return None
        old_path = line[len(quoted_prefix) : split_index]
        new_path = line[split_index + len(quoted_marker) : -1]
        return old_path, new_path

    plain_prefix = "diff --git a/"
    if not line.startswith(plain_prefix):
        return None

    split_index = line.rfind(" b/")
    if split_index == -1 or split_index < len(plain_prefix):
        return None

    old_path = line[len(plain_prefix) : split_index]
    new_path = line[split_index + len(" b/") :]
    return old_path, new_path


def _format_diff_git_line(old_path: str, new_path: str) -> str:
    quote_paths = any(ch in path for ch in (" ", "\t") for path in (old_path, new_path))
    old_token = f'"a/{old_path}"' if quote_paths else f"a/{old_path}"
    new_token = f'"b/{new_path}"' if quote_paths else f"b/{new_path}"
    return f"diff --git {old_token} {new_token}"


def _format_patch_marker_path(prefix: str, path: str) -> str:
    marker = f"{prefix}/{path}"
    if " " in path or "\t" in path:
        return f'"{marker}"'
    return marker


def _normalize_diff_block(header: str, block_lines: list[str]) -> str:
    diff_paths = _parse_diff_git_paths(header)
    header_line = header
    if diff_paths is not None:
        old_path, new_path = diff_paths
        header_line = _format_diff_git_line(old_path, new_path)

    has_mode = any(line.startswith("old mode ") for line in block_lines) and any(
        line.startswith("new mode ") for line in block_lines
    )
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
        raise ValueError("git is required for apply_patch") from exc


def _format_patch_error(error: str, patch_text: str) -> str:
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


def _formatter_feedback_for_changes(
    changes: list[dict[str, object]],
    *,
    workspace: Path,
    hooks_config: RuntimeHooksConfig | None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if hooks_config is None:
        return [], []

    executor = FormatterExecutor(hooks_config, workspace)
    formatter_results: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    for change in changes:
        if change.get("status") not in {"A", "M", "R"}:
            continue
        path_value = change.get("path")
        if not isinstance(path_value, str):
            continue
        candidate = (workspace / path_value).resolve()
        if not candidate.is_relative_to(workspace) or not candidate.is_file():
            continue
        result = executor.run(candidate)
        if result.status != "not_configured":
            payload = formatter_payload(result)
            payload["path"] = path_value
            formatter_results.append(payload)
        for diagnostic in formatter_diagnostics(result):
            diagnostic["path"] = path_value
            diagnostics.append(diagnostic)
    return formatter_results, diagnostics


def _with_formatter_feedback(
    result: ToolResult,
    *,
    workspace: Path,
    hooks_config: RuntimeHooksConfig | None,
) -> ToolResult:
    raw_changes = result.data.get("changes")
    if not isinstance(raw_changes, list):
        return result
    changes: list[dict[str, object]] = []
    for item in cast(list[object], raw_changes):
        if isinstance(item, dict):
            changes.append(cast(dict[str, object], item))
    formatter_results, diagnostics = _formatter_feedback_for_changes(
        changes,
        workspace=workspace,
        hooks_config=hooks_config,
    )
    if not formatter_results and not diagnostics:
        return result

    data = dict(result.data)
    if formatter_results:
        data["formatters"] = formatter_results
    if diagnostics:
        data["diagnostics"] = diagnostics

    content = result.content
    if content is not None and diagnostics:
        content += f"\nFormatter warning: {diagnostics[0]['message']}"

    return ToolResult(
        tool_name=result.tool_name,
        status=result.status,
        content=content,
        data=data,
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
    except (UnidiffParseError, ValueError):
        return []

    changes: list[dict[str, object]] = []
    for patched_file in patch_set:
        old_path = (
            None
            if patched_file.source_file == "/dev/null"
            else _strip_diff_prefix(patched_file.source_file)
        )
        new_path = (
            None
            if patched_file.target_file == "/dev/null"
            else _strip_diff_prefix(patched_file.target_file)
        )

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
            block_old_path = line[len("rename from ") :].strip()
            continue

        if line.startswith("rename to "):
            block_new_path = line[len("rename to ") :].strip()
            continue

        if line.startswith("--- "):
            old_marker = line[4:].strip()
            patch_old_path = None if old_marker == "/dev/null" else _strip_diff_prefix(old_marker)
            continue

        if not line.startswith("+++ "):
            continue

        new_marker = line[4:].strip()
        patch_new_path = None if new_marker == "/dev/null" else _strip_diff_prefix(new_marker)
        block_old_path = patch_old_path
        block_new_path = patch_new_path
        patch_old_path = None

    flush_block()

    return changes


def _changes_from_patch(patch_text: str) -> list[dict[str, object]]:
    changes = _changes_from_unified_diff(_normalize_patch_text(patch_text))
    changes.extend(_changes_from_patch_metadata(patch_text))
    return _dedupe_changes(changes)


_APPLY_PATCH_DESCRIPTION = (
    "Apply structured file patches or unified diff patches inside the current workspace."
)


class ApplyPatchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="apply_patch",
        description=_APPLY_PATCH_DESCRIPTION,
        input_schema={"patch": {"type": "string"}},
        read_only=False,
    )

    def __init__(self, *, hooks_config: RuntimeHooksConfig | None = None) -> None:
        self._hooks_config = hooks_config

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        patch_text = call.arguments.get("patch")
        if not isinstance(patch_text, str):
            raise ValueError("apply_patch requires a string 'patch' argument")

        if _looks_like_marker_patch(patch_text):
            result = _apply_marker_patch(patch_text, workspace=workspace)
            return _with_formatter_feedback(
                result,
                workspace=workspace.resolve(),
                hooks_config=self._hooks_config,
            )

        normalized_patch = _normalize_patch_text(patch_text)
        patch_path = workspace / ".voidcode_apply_patch.patch"
        patch_path.write_text(normalized_patch, encoding="utf-8", newline="\n")
        try:
            check = _run_git_command(["git", "apply", "--check", str(patch_path)], workspace)
            if check.returncode != 0:
                error = check.stdout or "Patch check failed"
                if _looks_like_mode_only_patch(patch_text):
                    changes = _changes_from_patch(patch_text)
                    content = "\n".join(
                        f"M {c['path']}"
                        if c.get("status") != "R"
                        else f"M {c['old_path']} -> {c['path']}"
                        for c in changes
                    )
                    return ToolResult(
                        tool_name=self.definition.name,
                        status="ok",
                        content=content,
                        data={"changes": changes, "count": len(changes)},
                    )
                raise ValueError(_format_patch_error(error, normalized_patch))

            # Apply patch
            apply = _run_git_command(["git", "apply", str(patch_path)], workspace)
            if apply.returncode != 0:
                error = apply.stdout or "Patch apply failed"
                if _looks_like_mode_only_patch(patch_text):
                    changes = _changes_from_patch(patch_text)
                    content = "\n".join(
                        f"M {c['path']}"
                        if c.get("status") != "R"
                        else f"M {c['old_path']} -> {c['path']}"
                        for c in changes
                    )
                    return ToolResult(
                        tool_name=self.definition.name,
                        status="ok",
                        content=content,
                        data={"changes": changes, "count": len(changes)},
                    )
                raise ValueError(_format_patch_error(error, normalized_patch))

            changes = _changes_from_patch(patch_text)

            summary_lines: list[str] = []
            for c in changes:
                if c.get("status") == "R":
                    summary_lines.append(f"M {c['old_path']} -> {c['path']}")
                else:
                    summary_lines.append(f"{c['status']} {c['path']}")

            content = "\n".join(summary_lines) if summary_lines else "patch applied"

            # Validate that all affected paths are inside the workspace
            for c in changes:
                path_value = c.get("path")
                if isinstance(path_value, str):
                    _assert_within_workspace(workspace, Path(path_value))
                old_path_value = c.get("old_path")
                if isinstance(old_path_value, str):
                    _assert_within_workspace(workspace, Path(old_path_value))

            result = ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=content,
                data={"changes": changes, "count": len(changes)},
            )
            return _with_formatter_feedback(
                result,
                workspace=workspace.resolve(),
                hooks_config=self._hooks_config,
            )
        finally:
            try:
                patch_path.unlink(missing_ok=True)
            except Exception:
                pass


__all__ = ["ApplyPatchTool"]
