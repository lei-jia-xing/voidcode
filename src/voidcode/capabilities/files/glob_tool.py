from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import OutputBounds, TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult, ToolSuccess

from .gitignore import GitIgnoreMatcher

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
#: Upper bound for an explicit ``limit`` so one call cannot materialize an
#: unbounded match list.
MAX_RESULT_LIMIT = 5000


class GlobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pattern: str
    path: str = "."
    include_hidden: bool = True
    respect_gitignore: bool = False
    limit: int = Field(default=LIMIT, ge=1, le=MAX_RESULT_LIMIT)

    @field_validator("pattern", "path")
    @classmethod
    def _non_empty(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"glob {info.field_name} must not be empty")
        return value


@dataclass(frozen=True, slots=True)
class GlobResultBody:
    pattern: str
    path: str
    count: int
    truncated: bool
    matches: tuple[str, ...]
    search_error: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "pattern": self.pattern,
            "path": self.path,
            "count": self.count,
            "truncated": self.truncated,
            "matches": list(self.matches),
        }
        if self.search_error is not None:
            payload["search_error"] = self.search_error
        return payload


class GlobTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="glob",
        description="Find files matching a glob pattern.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The glob pattern to match files against"},
                "path": {"type": "string", "description": "Directory to search; relative paths are based at the workspace root."},
                "include_hidden": {
                    "type": "boolean",
                    "description": (
                        "Include hidden files and directories (name starts with '.'); defaults to true to preserve existing behaviour. "
                        "Set false to skip them."
                    ),
                },
                "respect_gitignore": {
                    "type": "boolean",
                    "description": (
                        "Also skip paths matched by the search root's .gitignore; defaults to false. "
                        "The matcher only reads the root .gitignore (no nested/global excludes)."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "description": "Maximum number of files to return; defaults to 100."},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
        supports_parallel=True,
    )

    @staticmethod
    def _find_files(
        workspace_root: Path,
        pattern: str,
        search_path: Path | None = None,
        project_root: Path | None = None,
        *,
        include_hidden: bool = True,
        gitignore: GitIgnoreMatcher | None = None,
        limit: int = LIMIT,
    ) -> tuple[list[Path], bool, str | None]:
        search_dir = search_path if search_path else workspace_root
        root = project_root or workspace_root

        if not search_dir.is_relative_to(root):
            root = search_dir

        matched: list[Path] = []
        truncated = False
        error_message: str | None = None

        try:
            for match in search_dir.glob(pattern):
                if not match.is_file():
                    continue
                relative_parts = match.relative_to(root).parts
                if any(ignore in relative_parts for ignore in DEFAULT_IGNORE_PATTERNS):
                    continue
                if not include_hidden and any(part.startswith(".") for part in relative_parts):
                    continue
                if gitignore is not None and gitignore.is_ignored(match.relative_to(root).as_posix(), is_directory=False):
                    continue
                if len(matched) == limit:
                    truncated = True
                    break
                matched.append(match)
        except OSError as exc:
            error_message = str(exc)

        return matched, truncated, error_message

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,  # noqa: ARG002 - required by the frozen Tool ABI
    ) -> ToolResult:
        workspace = context.require_workspace()
        args = GlobArgs.model_validate(dict(call.arguments))
        pattern_value = args.pattern
        include_hidden, respect_gitignore, limit = args.include_hidden, args.respect_gitignore, args.limit
        workspace_root = workspace.resolve()
        requested_path = Path(args.path)
        search_path = (requested_path if requested_path.is_absolute() else workspace_root / requested_path).resolve()
        if not search_path.exists():
            raise ValueError(f"glob path does not exist: {args.path}")
        if not search_path.is_dir():
            raise ValueError(f"glob path must be a directory: {args.path}")
        effective_root = search_path if not search_path.is_relative_to(workspace_root) else workspace_root
        gitignore = GitIgnoreMatcher.load(effective_root) if respect_gitignore else None
        matched, truncated, search_error = self._find_files(
            workspace_root,
            pattern_value,
            search_path,
            project_root=effective_root,
            include_hidden=include_hidden,
            gitignore=gitignore,
            limit=limit,
        )

        try:
            matched.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            matched.sort()

        if effective_root == workspace_root:
            relative_matches = [m.relative_to(workspace_root).as_posix() for m in matched]
            path_display = search_path.relative_to(workspace_root).as_posix() if search_path else "."
        else:
            relative_matches = [str(m.resolve()) for m in matched]
            path_display = str((search_path or effective_root).resolve())

        output = "\n".join(relative_matches) or "No files found."
        if truncated:
            output += f"\n[TRUNCATED] Showing first {limit} files."

        body = GlobResultBody(
            pattern=pattern_value,
            path=path_display,
            count=len(relative_matches),
            truncated=truncated,
            matches=tuple(relative_matches),
            search_error=search_error,
        )
        if search_error is not None:
            error_message = f"glob search failed: {search_error}"
            return ToolFailure(
                tool_name=self.definition.name,
                error=error_message,
                output=TextOutput(error_message, bounds=OutputBounds(truncated=truncated, partial=True)),
                body=body,
            )

        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(output, bounds=OutputBounds(truncated=truncated, partial=truncated)),
            body=body,
        )
