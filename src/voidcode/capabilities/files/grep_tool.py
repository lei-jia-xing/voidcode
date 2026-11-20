from __future__ import annotations

import fnmatch
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    ConfirmedStop,
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from ._common import MAX_RESULT_LIMIT, is_default_ignored, resolve_workspace_path, validate_glob_patterns, validate_non_empty
from .errors import raise_tool_diagnostic
from .gitignore import GitIgnoreMatcher

MAX_MATCHES = 200
_MAX_LINE_CHARS = 64 * 1024


class GrepArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pattern: str
    path: str = "."
    regex: bool = False
    ignore_case: bool = False
    context: int = Field(default=0, ge=0, le=100)
    include: list[str] | None = None
    exclude: list[str] | None = None
    respect_gitignore: bool = False
    limit: int = Field(default=MAX_MATCHES, ge=1, le=MAX_RESULT_LIMIT)

    _non_empty = field_validator("pattern", "path", mode="after")(validate_non_empty)
    _glob_patterns = field_validator("include", "exclude", mode="after")(validate_glob_patterns)


@dataclass(frozen=True, slots=True)
class GrepResultMatch:
    file: str
    line: int
    text: str
    columns: tuple[int, ...]
    before: tuple[tuple[int, str], ...]
    after: tuple[tuple[int, str], ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "text": self.text,
            "columns": list(self.columns),
            "before": [{"line": number, "text": text} for number, text in self.before],
            "after": [{"line": number, "text": text} for number, text in self.after],
        }


@dataclass(frozen=True, slots=True)
class GrepResultBody:
    path: str
    pattern: str
    regex: bool
    ignore_case: bool
    context: int
    match_count: int
    truncated: bool
    matches: tuple[GrepResultMatch, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "pattern": self.pattern,
            "regex": self.regex,
            "ignore_case": self.ignore_case,
            "context": self.context,
            "match_count": self.match_count,
            "truncated": self.truncated,
            "partial": self.truncated,
            "matches": [match.as_payload() for match in self.matches],
        }


class GrepTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="grep",
        icon=Icon(glyph="⌕"),
        description=(
            "Search workspace files. Pattern is literal by default; set regex=true to use regular expressions, "
            "and ignore_case=true for case-insensitive matching. Standard dependency, VCS, and build directories "
            "(.git, node_modules, __pycache__, dist, build, target, vendor, .venv, ...) are skipped at any depth; "
            "pass one explicitly as path to search inside it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text to search for; treated literally unless regex=true."},
                "path": {"type": "string", "description": "File or directory to search, relative to the workspace; defaults to workspace root."},
                "regex": {"type": "boolean", "description": "Treat pattern as a regular expression; defaults to false."},
                "ignore_case": {"type": "boolean", "description": "Match case-insensitively; defaults to false."},
                "context": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Number of surrounding lines per match (max 100)."},
                "include": {"type": "array", "items": {"type": "string"}, "description": "Optional glob filters for files to include."},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Optional glob filters for files to exclude."},
                "respect_gitignore": {
                    "type": "boolean",
                    "description": (
                        "Also skip paths matched by the search root's .gitignore; defaults to false. "
                        "The matcher only reads the root .gitignore (no nested/global excludes)."
                    ),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "description": "Maximum matching lines to return; defaults to 200."},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
    )

    @staticmethod
    def _matches_glob(path: str, pattern: str) -> bool:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.startswith("**/"):
            return fnmatch.fnmatch(path, pattern[3:])
        return False

    @staticmethod
    def _iter_targets(
        root: Path,
        *,
        project_root: Path,
        context: ToolContext,
        include: list[str] | None,
        exclude: list[str] | None,
        gitignore: GitIgnoreMatcher | None = None,
    ) -> Iterator[Path]:
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return
        include_patterns = include or []
        exclude_patterns = exclude or []
        if root.is_file():
            yield root
            return

        def fail_walk(error: OSError) -> None:
            raise error

        for directory, dirnames, filenames in root.walk(top_down=True, on_error=fail_walk):
            if context.abort_signal is not None and context.abort_signal.cancelled:
                return
            directory_relative = directory.relative_to(project_root)
            # Default-ignore names are judged below the requested root, so an explicit
            # ``path`` naming one is honored; gitignore rules stay project-relative.
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not is_default_ignored((directory / name).relative_to(root), is_directory=True)
                and (gitignore is None or not gitignore.is_ignored((directory_relative / name).as_posix(), is_directory=True))
            )
            for name in sorted(filenames):
                if context.abort_signal is not None and context.abort_signal.cancelled:
                    return
                candidate = directory / name
                if not candidate.is_file():
                    continue
                rel = candidate.relative_to(project_root).as_posix()
                if gitignore is not None and gitignore.is_ignored(rel, is_directory=False):
                    continue
                if include_patterns and not any(GrepTool._matches_glob(rel, pat) for pat in include_patterns):
                    continue
                if any(GrepTool._matches_glob(rel, pat) for pat in exclude_patterns):
                    continue
                yield candidate

    @staticmethod
    def _iter_matches(
        path: Path,
        *,
        relative_path: str,
        pattern: re.Pattern[str],
        context: int,
        invocation: ToolContext,
    ) -> Iterator[GrepResultMatch]:
        before: deque[tuple[int, str]] = deque(maxlen=context)

        def lines() -> Iterator[tuple[int, str]]:
            if invocation.abort_signal is not None and invocation.abort_signal.cancelled:
                return
            with path.open("r", encoding="utf-8", newline="") as stream:
                for line_number, raw_line in enumerate(iter(lambda: stream.readline(_MAX_LINE_CHARS + 1), ""), 1):
                    if invocation.abort_signal is not None and invocation.abort_signal.cancelled:
                        return
                    if len(raw_line) > _MAX_LINE_CHARS:
                        raise_tool_diagnostic(
                            message=(
                                f"grep line {line_number} in {relative_path} exceeds {_MAX_LINE_CHARS} characters; "
                                "use a bounded file read to inspect it."
                            ),
                            error_kind="tool_execution_error",
                            reason="line_too_long",
                            details={"path": relative_path, "line": line_number},
                        )
                    yield line_number, raw_line.rstrip("\r\n")

        try:
            remaining = iter(lines())
            following = deque(islice(remaining, context + 1))
            while following:
                if invocation.abort_signal is not None and invocation.abort_signal.cancelled:
                    return
                current = following.popleft()
                columns = tuple(match.start() + 1 for match in pattern.finditer(current[1]))
                if columns:
                    yield GrepResultMatch(
                        relative_path,
                        current[0],
                        current[1],
                        columns,
                        tuple(before),
                        tuple(following),
                    )
                before.append(current)
                try:
                    following.append(next(remaining))
                except StopIteration:
                    pass
        except UnicodeDecodeError:
            return

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,  # noqa: ARG002 - required by the frozen Tool ABI
    ) -> ToolResult:
        workspace = context.require_workspace()
        args = GrepArgs.model_validate(json_wire_object(call.arguments))

        workspace_root = workspace.resolve()
        candidate, relative_path = resolve_workspace_path(workspace, args.path)
        is_external = not candidate.is_relative_to(workspace_root)

        if not candidate.exists():
            raise ValueError(f"grep target does not exist: {args.path}")

        effective_root = candidate if is_external and candidate.is_dir() else workspace_root

        try:
            pattern = re.compile(
                args.pattern if args.regex else re.escape(args.pattern),
                re.IGNORECASE if args.ignore_case else 0,
            )
        except re.error as exc:
            raise_tool_diagnostic(
                message=(
                    "grep Validation error: pattern: invalid regex pattern "
                    f"({exc.msg}) (received str). "
                    "Please retry with corrected arguments that satisfy the tool schema."
                ),
                error_kind="tool_input_validation",
                reason="invalid_regex",
                retry_guidance=("Retry with a valid regex pattern, or set regex=false for a literal search."),
                details={"pattern": args.pattern, "regex_error": exc.msg},
            )
        gitignore = GitIgnoreMatcher.load(effective_root) if args.respect_gitignore else None
        targets = self._iter_targets(
            candidate,
            project_root=effective_root,
            context=context,
            include=args.include,
            exclude=args.exclude,
            gitignore=gitignore,
        )

        matches: list[GrepResultMatch] = []
        truncated = False
        try:
            for target in targets:
                file_display = str(target.resolve()) if is_external else target.relative_to(workspace_root).as_posix()
                for match in self._iter_matches(target, relative_path=file_display, pattern=pattern, context=args.context, invocation=context):
                    if len(matches) == args.limit:
                        truncated = True
                        break
                    matches.append(match)
                if truncated:
                    break
        except OSError as exc:
            raise_tool_diagnostic(
                message=f"grep search failed: {exc}",
                error_kind="tool_execution_error",
                reason="search_io_error",
                details={"path": args.path},
            )

        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                tool_name=self.definition.name,
                error="grep search cancelled",
                output=TextOutput("grep search cancelled", bounds=OutputBounds(partial=True)),
                execution=ConfirmedStop(cancellation_signalled=True),
            )

        total_occurrences = sum(len(match.columns) for match in matches)
        path_display = str(candidate.resolve()) if is_external else relative_path
        preview_lines: dict[tuple[str, int], tuple[str, tuple[int, ...]]] = {}
        for match in matches:
            for line_number, line_text in (*match.before, (match.line, match.text), *match.after):
                columns = match.columns if line_number == match.line else ()
                key = (match.file, line_number)
                if key not in preview_lines or columns:
                    preview_lines[key] = (line_text, columns)
        rendered_lines = [f"{file}:{line}:{','.join(map(str, columns)) or '-'}: {text}" for (file, line), (text, columns) in preview_lines.items()]
        summary = f"Found {total_occurrences} match(es) for {args.pattern!r} in {path_display}"
        if rendered_lines:
            summary += "\n" + "\n".join(rendered_lines)

        body = GrepResultBody(
            path=path_display,
            pattern=args.pattern,
            regex=args.regex,
            ignore_case=args.ignore_case,
            context=args.context,
            match_count=total_occurrences,
            truncated=truncated,
            matches=tuple(matches),
        )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(summary, bounds=OutputBounds(truncated=truncated, partial=truncated)),
            body=body,
        )
