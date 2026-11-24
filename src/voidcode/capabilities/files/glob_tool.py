from __future__ import annotations

import heapq
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    ConfirmedStop,
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolDiagnostics,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from ..tool_errors import expected_failures, raise_tool_diagnostic
from ._common import MAX_RESULT_LIMIT, full_match_path, has_hidden_component, is_default_ignored, resolve_workspace_path
from .gitignore import GitIgnoreMatcher

LIMIT = 100


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
        if info.field_name == "pattern" and (Path(value).is_absolute() or ".." in Path(value).parts):
            raise ValueError("glob pattern must be relative to the search directory; use path to choose the directory")
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
        icon=Icon(glyph="❋"),
        description=(
            "Find files matching a glob pattern, newest modified first. Standard dependency, VCS, and build directories "
            "(.git, node_modules, __pycache__, dist, build, target, vendor, .venv, ...) are skipped "
            "at any depth; pass one explicitly as path to search inside it. Hidden entries are included and "
            ".gitignore is not applied unless you ask for it, matching grep's defaults."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The glob pattern to match files against"},
                "path": {"type": "string", "description": "Directory to search; relative paths are based at the workspace root."},
                "include_hidden": {
                    "type": "boolean",
                    "description": "Include hidden files and directories (name starts with '.'); defaults to true, matching grep.",
                },
                "respect_gitignore": {
                    "type": "boolean",
                    "description": (
                        "Also skip paths matched by the search root's .gitignore; defaults to false. "
                        "The matcher only reads the root .gitignore (no nested/global excludes)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "description": "Maximum number of files to return after sorting newest first; defaults to 100.",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
    )

    @staticmethod
    def _find_files(
        search_dir: Path,
        pattern: str,
        *,
        project_root: Path,
        context: ToolContext,
        include_hidden: bool = True,
        gitignore: GitIgnoreMatcher | None = None,
        limit: int = LIMIT,
    ) -> tuple[list[Path], bool, str | None]:
        """Collect up to ``limit`` matches, newest first, with a deterministic tie-break.

        The limit is applied after ordering, so truncation never depends on walk
        order: the returned page is the newest ``limit`` matches (ties by path).
        """
        selected: list[tuple[float, str, Path]] = []
        matched_count = 0

        def fail_walk(error: OSError) -> None:
            raise error

        def excluded(path: Path, *, is_directory: bool) -> bool:
            relative = path.relative_to(project_root)
            scanned = path.relative_to(search_dir)
            return (
                is_default_ignored(scanned, is_directory=is_directory)
                or (not include_hidden and has_hidden_component(relative))
                or (gitignore is not None and gitignore.is_ignored(relative.as_posix(), is_directory=is_directory))
            )

        def consider(match: Path) -> None:
            nonlocal matched_count
            key_path = match.relative_to(search_dir).as_posix()
            try:
                metadata = match.stat()
            except OSError:
                return
            if not stat.S_ISREG(metadata.st_mode):
                return
            matched_count += 1
            entry = (metadata.st_mtime, key_path, match)
            if len(selected) < limit:
                heapq.heappush(selected, entry)
            elif entry > selected[0]:
                heapq.heapreplace(selected, entry)

        def collected() -> list[Path]:
            selected.sort(key=lambda item: (-item[0], item[1]))
            return [item[2] for item in selected]

        try:
            if context.abort_signal is not None and context.abort_signal.cancelled:
                return [], False, None
            for directory, dirnames, filenames in search_dir.walk(on_error=fail_walk):
                if context.abort_signal is not None and context.abort_signal.cancelled:
                    return collected(), False, None
                dirnames[:] = sorted(name for name in dirnames if not excluded(directory / name, is_directory=True))
                for name in sorted(filenames):
                    if context.abort_signal is not None and context.abort_signal.cancelled:
                        return collected(), False, None
                    match = directory / name
                    if not full_match_path(match.relative_to(search_dir).as_posix(), pattern) or excluded(match, is_directory=False):
                        continue
                    consider(match)
        except OSError as exc:
            return collected(), matched_count > limit, str(exc)
        return collected(), matched_count > limit, None

    @expected_failures
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
        try:
            search_path, _ = resolve_workspace_path(workspace, args.path)
        except ValueError as exc:
            raise_tool_diagnostic(
                message=f"glob could not resolve the search path {args.path!r}: {exc}",
                error_kind="tool_input_mismatch",
                reason="invalid_path",
                retry_guidance="Retry with a valid directory path.",
                details={"path": args.path},
            )
        if not search_path.exists():
            raise_tool_diagnostic(
                message=f"glob path does not exist: {args.path}",
                error_kind="tool_input_mismatch",
                reason="search_path_not_found",
                retry_guidance="Retry with an existing directory path.",
                details={"path": args.path},
            )
        if not search_path.is_dir():
            raise_tool_diagnostic(
                message=f"glob path must be a directory: {args.path}",
                error_kind="tool_input_mismatch",
                reason="search_path_not_directory",
                retry_guidance="Retry with a directory path; glob searches directories, not single files.",
                details={"path": args.path},
            )
        effective_root = search_path if not search_path.is_relative_to(workspace_root) else workspace_root
        gitignore = GitIgnoreMatcher.load(effective_root) if respect_gitignore else None
        matched, truncated, search_error = self._find_files(
            search_path,
            pattern_value,
            project_root=effective_root,
            context=context,
            include_hidden=include_hidden,
            gitignore=gitignore,
            limit=limit,
        )

        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                tool_name=self.definition.name,
                error="glob search cancelled",
                output=TextOutput("glob search cancelled", bounds=OutputBounds(partial=True)),
                execution=ConfirmedStop(cancellation_signalled=True),
            )

        if effective_root == workspace_root:
            relative_matches = [m.relative_to(workspace_root).as_posix() for m in matched]
            path_display = search_path.relative_to(workspace_root).as_posix()
        else:
            relative_matches = [str(m.resolve()) for m in matched]
            path_display = str(search_path)

        output = "\n".join(relative_matches) or "No files found."

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
                diagnostics=ToolDiagnostics(
                    kind="tool_execution_error",
                    summary=error_message,
                    details={"reason": "search_io_error", "path": path_display},
                    guidance="Retry the search; if the failure persists, check the directory's permissions.",
                ),
            )

        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(output, bounds=OutputBounds(truncated=truncated, partial=truncated)),
            body=body,
        )
