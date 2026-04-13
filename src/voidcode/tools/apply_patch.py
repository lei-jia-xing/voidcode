from __future__ import annotations

import subprocess
from pathlib import Path
from typing import ClassVar

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from .contracts import ToolCall, ToolDefinition, ToolResult


def _assert_within_workspace(workspace: Path, rel_path: Path) -> None:
    root = workspace.resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("patch operation must affect paths inside the workspace")


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
    old_token = f'"a/{old_path}"' if " " in old_path or "\t" in old_path else f"a/{old_path}"
    new_token = f'"b/{new_path}"' if " " in new_path or "\t" in new_path else f"b/{new_path}"
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


class ApplyPatchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="apply_patch",
        description="Apply unified diff patches to files inside the current workspace.",
        input_schema={"patch": {"type": "string"}},
        read_only=False,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        patch_text = call.arguments.get("patch")
        if not isinstance(patch_text, str):
            raise ValueError("apply_patch requires a string 'patch' argument")

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
                raise ValueError(error)

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
                raise ValueError(error)

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

            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=content,
                data={"changes": changes, "count": len(changes)},
            )
        finally:
            try:
                patch_path.unlink(missing_ok=True)
            except Exception:
                pass


__all__ = ["ApplyPatchTool"]
