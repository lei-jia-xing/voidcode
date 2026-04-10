from __future__ import annotations

import subprocess
from pathlib import Path
from typing import ClassVar

from .contracts import ToolCall, ToolDefinition, ToolResult


def _assert_within_workspace(workspace: Path, rel_path: Path) -> None:
    root = workspace.resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("patch operation must affect paths inside the workspace")


def _strip_diff_prefix(path_text: str) -> str:
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


def _changes_from_patch(patch_text: str) -> list[dict[str, object]]:
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

    deduped: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for change in changes:
        key = (change.get("status"), change.get("old_path"), change.get("path"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(change)
    return deduped


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

        patch_path = workspace / ".voidcode_apply_patch.patch"
        patch_path.write_text(patch_text, encoding="utf-8")
        try:
            check = subprocess.run(
                ["git", "apply", "--check", str(patch_path)],
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if check.returncode != 0:
                error = check.stdout or "Patch check failed"
                raise ValueError(error)

            # Apply patch
            apply = subprocess.run(
                ["git", "apply", str(patch_path)],
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if apply.returncode != 0:
                error = apply.stdout or "Patch apply failed"
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
