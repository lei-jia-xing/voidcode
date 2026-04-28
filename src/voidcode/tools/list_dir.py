from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import ClassVar, cast

from ._workspace import resolve_workspace_path
from .contracts import ToolCall, ToolDefinition, ToolResult

IGNORE_PATTERNS = frozenset(
    [
        "node_modules",
        "__pycache__",
        ".git",
        "dist",
        "build",
        "target",
        "vendor",
        ".venv",
        "venv",
        ".idea",
        ".vscode",
        ".coverage",
        "coverage",
        "tmp",
        "temp",
        ".cache",
        "logs",
    ]
)

LIMIT = 100


def _render_tree(
    dirs: set[str],
    files_by_dir: dict[str, list[str]],
    dir_path: str,
    depth: int,
) -> list[str]:
    lines: list[str] = []
    indent = "  " * depth

    if depth > 0:
        lines.append(f"{indent}{Path(dir_path).name}/")

    child_indent = "  " * (depth + 1)
    child_dirs = sorted(d for d in dirs if Path(d).parent.as_posix() == dir_path and d != dir_path)

    for child in child_dirs:
        lines.extend(_render_tree(dirs, files_by_dir, child, depth + 1))

    files = sorted(files_by_dir.get(dir_path, []))
    for file in files:
        lines.append(f"{child_indent}{file}")

    return lines


def _is_ignored(
    *,
    relative_path: Path,
    all_ignore_patterns: set[str],
) -> bool:
    rel_posix = relative_path.as_posix()
    relative_parts = relative_path.parts
    for pattern in all_ignore_patterns:
        p = pattern.strip()
        if not p:
            continue

        if p in relative_parts:
            return True

        if fnmatch.fnmatch(rel_posix, p):
            return True

        # Support directory-style glob ignores like "build/**"
        if p.endswith("/**"):
            prefix = p[:-3].rstrip("/")
            if not prefix:
                continue
            if rel_posix == prefix or rel_posix.startswith(f"{prefix}/"):
                return True
    return False


class ListTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="list",
        description="List files and directories in a workspace path.",
        input_schema={
            "path": {
                "type": "string",
                "description": "The directory to list (relative to workspace). Defaults to root.",
            },
            "ignore": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Additional glob patterns to ignore",
            },
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        path_value = call.arguments.get("path")
        search_path: Path

        if isinstance(path_value, str):
            search_path, _ = resolve_workspace_path(workspace=workspace, raw_path=path_value)

            if not search_path.exists():
                raise ValueError(f"list path does not exist: {path_value}")

            if not search_path.is_dir():
                raise ValueError(f"list path is not a directory: {path_value}")
        else:
            search_path = workspace.resolve()

        workspace_root = workspace.resolve()

        extra_ignore_raw = call.arguments.get("ignore")
        if extra_ignore_raw is not None and not isinstance(extra_ignore_raw, list):
            raise ValueError("list ignore must be an array of strings")

        all_ignore: set[str] = set(IGNORE_PATTERNS)
        if isinstance(extra_ignore_raw, list):
            extra_list = cast(list[str], extra_ignore_raw)
            for item_raw in extra_list:
                all_ignore.add(item_raw)

        dirs: set[str] = {search_path.as_posix()}
        files_by_dir: dict[str, list[str]] = {}

        try:
            for item in search_path.rglob("*"):
                if sum(len(v) for v in files_by_dir.values()) >= LIMIT:
                    break

                try:
                    relative_path = item.relative_to(workspace_root)
                except ValueError:
                    relative_path = item

                if _is_ignored(relative_path=relative_path, all_ignore_patterns=all_ignore):
                    continue

                item_str = item.as_posix()
                parent_str = item.parent.as_posix()

                if parent_str not in dirs:
                    dirs.add(parent_str)

                if item.is_dir():
                    dirs.add(item_str)
                elif item.is_file():
                    if parent_str not in files_by_dir:
                        files_by_dir[parent_str] = []
                    files_by_dir[parent_str].append(item.name)
        except PermissionError:
            pass

        tree_lines = _render_tree(dirs, files_by_dir, search_path.as_posix(), 0)
        file_count = sum(len(v) for v in files_by_dir.values())
        truncated = file_count >= LIMIT

        # Start output with the absolute root path, following with a trailing '/'
        output_lines = [f"{search_path.as_posix()}/"]
        output_lines.extend(tree_lines)

        if truncated:
            output_lines.append("")
            output_lines.append(f"(Results truncated: showing first {LIMIT} files)")

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content="\n".join(output_lines),
            data={
                # Expose absolute path in metadata for consistency with the output root
                "path": str(search_path.as_posix()),
                "count": file_count,
                "truncated": truncated,
            },
            truncated=truncated,
            partial=truncated,
        )
