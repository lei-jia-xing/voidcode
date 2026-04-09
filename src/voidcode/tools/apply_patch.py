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

        diff = subprocess.run(
            ["git", "diff", "--name-status"],
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            text=True,
        )

        changes: list[dict[str, object]] = []
        for line in diff.stdout.splitlines():
            if not line.strip():
                continue
            if line.startswith("R"):
                parts = line.split("\t")
                if len(parts) >= 3:
                    old_path, new_path = parts[1], parts[2]
                    changes.append({"path": new_path, "old_path": old_path, "status": "R"})
            else:
                parts = line.split("\t")
                if len(parts) != 2:
                    continue
                status, path = parts[0], parts[1]
                changes.append({"path": path, "status": status})

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

        try:
            patch_path.unlink(missing_ok=True)
        except Exception:
            pass

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data={"changes": changes, "count": len(changes)},
        )


__all__ = ["ApplyPatchTool"]
