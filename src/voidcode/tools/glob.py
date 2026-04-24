from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ._workspace import resolve_workspace_path
from .contracts import ToolCall, ToolDefinition, ToolResult

DEFAULT_IGNORE_PATTERNS = frozenset(
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


class GlobTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="glob",
        description="Find files matching a glob pattern inside the workspace.",
        input_schema={
            "pattern": {"type": "string", "description": "The glob pattern to match files against"},
            "path": {
                "type": "string",
                "description": (
                    "The directory to search in (relative to workspace). "
                    "Defaults to workspace root."
                ),
            },
        },
        read_only=True,
    )

    @staticmethod
    def _find_files(
        workspace_root: Path,
        pattern: str,
        search_path: Path | None = None,
    ) -> tuple[list[Path], bool]:
        search_dir = search_path if search_path else workspace_root

        if not search_dir.is_relative_to(workspace_root):
            raise ValueError("glob search path must be inside the workspace")

        matched: list[Path] = []
        truncated = False

        try:
            for match in search_dir.glob(pattern):
                if match.is_file():
                    relative_parts = match.relative_to(workspace_root).parts
                    if any(ignore in relative_parts for ignore in DEFAULT_IGNORE_PATTERNS):
                        continue

                    matched.append(match)

                    if len(matched) >= LIMIT:
                        truncated = True
                        break
        except Exception:
            pass

        return matched, truncated

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        pattern_value = call.arguments.get("pattern")
        if not isinstance(pattern_value, str):
            raise ValueError("glob requires a string pattern argument")

        if not pattern_value.strip():
            raise ValueError("glob pattern must not be empty")

        path_value = call.arguments.get("path")
        search_path: Path | None = None
        if isinstance(path_value, str):
            search_path, _ = resolve_workspace_path(workspace=workspace, raw_path=path_value)

            if not search_path.exists():
                raise ValueError(f"glob path does not exist: {path_value}")

        workspace_root = workspace.resolve()
        matched, truncated = self._find_files(workspace_root, pattern_value, search_path)

        try:
            matched.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            matched.sort()

        relative_matches = [m.relative_to(workspace_root).as_posix() for m in matched]

        if not relative_matches:
            output = "No files found"
        else:
            output_lines = relative_matches
            if truncated:
                output_lines.append("")
                output_lines.append(
                    "(Results are truncated: showing first "
                    f"{LIMIT} results. Consider using a more specific path or pattern.)"
                )
            output = "\n".join(output_lines)

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=output,
            data={
                "pattern": pattern_value,
                "path": search_path.relative_to(workspace_root).as_posix() if search_path else ".",
                "count": len(relative_matches),
                "truncated": truncated,
                "matches": relative_matches,
            },
            truncated=truncated,
            partial=truncated,
        )
