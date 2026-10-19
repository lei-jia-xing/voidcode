from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import OutputBounds, TextOutput, ToolCall, ToolDefinition, ToolFailure, ToolResult, ToolSuccess

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
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

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
        pattern_value = call.arguments.get("pattern")
        if not isinstance(pattern_value, str):
            raise ValueError("glob requires a string pattern argument")

        if not pattern_value.strip():
            raise ValueError("glob pattern must not be empty")

        include_hidden = _bool_argument(call, "include_hidden", default=True)
        respect_gitignore = _bool_argument(call, "respect_gitignore", default=False)
        limit = _int_argument(call, "limit", default=LIMIT, maximum=MAX_RESULT_LIMIT)

        path_value = call.arguments.get("path")
        search_path: Path | None = None
        resolved_candidate: Path | None = None
        is_external = False
        if isinstance(path_value, str):
            workspace_root = workspace.resolve()
            requested_path = Path(path_value)
            resolved_candidate = (requested_path if requested_path.is_absolute() else workspace_root / requested_path).resolve()
            is_external = not resolved_candidate.is_relative_to(workspace_root)
            search_path = resolved_candidate

            if not search_path.exists():
                raise ValueError(f"glob path does not exist: {path_value}")

        workspace_root = workspace.resolve()
        effective_root = resolved_candidate if is_external and resolved_candidate is not None and resolved_candidate.is_dir() else workspace_root
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


def _bool_argument(call: ToolCall, key: str, *, default: bool) -> bool:
    value = call.arguments.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"glob requires a boolean {key} argument")
    return value


def _int_argument(call: ToolCall, key: str, *, default: int, maximum: int) -> int:
    value = call.arguments.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"glob requires an integer {key} argument")
    if value < 1:
        raise ValueError(f"glob {key} must be greater than or equal to 1")
    return min(value, maximum)
