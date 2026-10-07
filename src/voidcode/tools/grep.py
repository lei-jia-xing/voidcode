from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, field_validator

from ..core.tool_context import ToolContext
from ..security.json_values import json_wire_object, own_json_object
from ..security.path_policy import resolve_workspace_path as resolve_workspace_path_policy
from ._gitignore import GitIgnoreMatcher
from ._pydantic_args import parse_tool_args, validate_non_empty
from ._repair import raise_tool_diagnostic
from .contracts import OutputBounds, TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolResult, ToolSuccess

MAX_MATCHES = 200
#: Upper bound for an explicit ``limit`` so one call cannot materialize an
#: unbounded match list.
MAX_RESULT_LIMIT = 5000
DEFAULT_IGNORE_PATTERNS = frozenset(
    (
        ".git",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
    )
)


class GrepArgs(BaseModel):
    pattern: str
    path: str
    regex: bool = False
    ignore_case: bool = False
    context: int = 0
    include: list[str] | None = None
    exclude: list[str] | None = None
    respect_gitignore: bool = False
    limit: int = MAX_MATCHES

    _validate_pattern = field_validator("pattern", mode="after")(validate_non_empty)
    _validate_path = field_validator("path", mode="after")(validate_non_empty)

    @field_validator("context", mode="after")
    @classmethod
    def _validate_context(cls, value: int) -> int:
        if value < 0:
            raise ValueError("context must be greater than or equal to 0")
        return value

    @field_validator("limit", mode="after")
    @classmethod
    def _validate_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("limit must be greater than or equal to 1")
        return min(value, MAX_RESULT_LIMIT)

    @field_validator("include", "exclude", mode="after")
    @classmethod
    def _validate_glob_patterns(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not all(item.strip() for item in value):
            raise ValueError("glob patterns must be non-empty strings")
        return value


@dataclass(frozen=True, slots=True)
class GrepResultMatch:
    file: str
    line: int
    text: str
    columns: tuple[int, ...]
    before: tuple[Mapping[str, object], ...]
    after: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "before", tuple(own_json_object(item) for item in self.before))
        object.__setattr__(self, "after", tuple(own_json_object(item) for item in self.after))

    def as_payload(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "text": self.text,
            "columns": list(self.columns),
            "before": [json_wire_object(item) for item in self.before],
            "after": [json_wire_object(item) for item in self.after],
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
    diagnostics: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(own_json_object(item) for item in self.diagnostics))

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
            "diagnostics": [json_wire_object(item) for item in self.diagnostics],
        }


class GrepTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="grep",
        description=(
            "Search workspace files. Pattern is literal by default; set regex=true to use regular expressions, "
            "and ignore_case=true for case-insensitive matching."
        ),
        input_schema={
            "pattern": {"type": "string", "description": "Text to search for; treated literally unless regex=true."},
            "path": {"type": "string", "description": "File or directory to search, relative to the workspace; defaults to workspace root."},
            "regex": {
                "type": "boolean",
                "description": "Treat pattern as a regular expression; defaults to false.",
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Match case-insensitively; defaults to false.",
            },
            "context": {"type": "integer", "minimum": 0, "description": "Number of surrounding lines to include for each match."},
            "include": {"type": "array", "items": {"type": "string"}, "description": "Optional glob filters for files to include."},
            "exclude": {"type": "array", "items": {"type": "string"}, "description": "Optional glob filters for files to exclude."},
            "respect_gitignore": {
                "type": "boolean",
                "description": (
                    "Also skip paths matched by the search root's .gitignore; defaults to false. The matcher only "
                    "reads the root .gitignore (no nested/global excludes)."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULT_LIMIT,
                "description": f"Maximum matching lines to return; defaults to {MAX_MATCHES}.",
            },
            "required": ["pattern", "path"],
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
    def _collect_targets(
        root: Path,
        *,
        project_root: Path,
        include: list[str] | None,
        exclude: list[str] | None,
        gitignore: GitIgnoreMatcher | None = None,
    ) -> list[Path]:
        targets: list[Path] = []
        include_patterns = include or []
        exclude_patterns = exclude or []
        if root.is_file():
            return [root]

        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(project_root).as_posix()
            if any(part in DEFAULT_IGNORE_PATTERNS for part in candidate.relative_to(project_root).parts):
                continue
            if gitignore is not None and gitignore.is_ignored(rel, is_directory=False):
                continue
            if include_patterns and not any(GrepTool._matches_glob(rel, pat) for pat in include_patterns):
                continue
            if any(GrepTool._matches_glob(rel, pat) for pat in exclude_patterns):
                continue
            targets.append(candidate)
        targets.sort(key=lambda path: path.relative_to(project_root).as_posix())
        return targets

    @staticmethod
    def _read_lines(path: Path) -> list[str] | None:
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                return [line.rstrip("\r\n") for line in fh]
        except UnicodeDecodeError, OSError:
            return None

    @staticmethod
    def _context_lines(lines: list[str], start: int, end: int, *, context: int) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        context = max(0, context)
        before: list[dict[str, object]] = [{"line": line_no + 1, "text": lines[line_no]} for line_no in range(max(0, start - context), start)]
        after: list[dict[str, object]] = [
            {"line": line_no + 1, "text": lines[line_no]} for line_no in range(end + 1, min(len(lines), end + 1 + context))
        ]
        return before, after

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        args = parse_tool_args(
            GrepArgs,
            {
                "pattern": call.arguments.get("pattern"),
                "path": call.arguments.get("path"),
                "regex": call.arguments.get("regex", False),
                "ignore_case": call.arguments.get("ignore_case", False),
                "context": call.arguments.get("context", 0),
                "include": call.arguments.get("include"),
                "exclude": call.arguments.get("exclude"),
                "respect_gitignore": call.arguments.get("respect_gitignore", False),
                "limit": call.arguments.get("limit", MAX_MATCHES),
            },
            tool_name=self.definition.name,
        )

        resolution = resolve_workspace_path_policy(
            workspace=workspace,
            raw_path=args.path,
            containment_error="grep path must resolve to a valid path",
            allow_outside_workspace=True,
        )
        candidate = resolution.candidate
        relative_path = resolution.relative_path

        if not candidate.exists():
            raise ValueError(f"grep target does not exist: {args.path}")

        workspace_root = workspace.resolve()
        effective_root = candidate if resolution.is_external and candidate.is_dir() else workspace_root

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
        targets = self._collect_targets(
            candidate,
            project_root=effective_root,
            include=args.include,
            exclude=args.exclude,
            gitignore=gitignore,
        )

        matches: list[GrepResultMatch] = []
        for target in targets:
            lines = self._read_lines(target)
            if lines is None:
                continue
            for line_index, line_text in enumerate(lines):
                columns = [match.start() + 1 for match in pattern.finditer(line_text)]
                if not columns:
                    continue
                before, after = self._context_lines(
                    lines,
                    line_index,
                    line_index,
                    context=args.context,
                )
                matches.append(
                    GrepResultMatch(
                        file=(str(target.resolve()) if resolution.is_external else target.relative_to(workspace_root).as_posix()),
                        line=line_index + 1,
                        text=line_text,
                        columns=tuple(columns),
                        before=tuple(before),
                        after=tuple(after),
                    )
                )
                if len(matches) >= args.limit:
                    break
            if len(matches) >= args.limit:
                break

        total_occurrences = sum(len(match.columns) for match in matches)
        preview_lines: list[str] = []
        for match in matches[:10]:
            preview_lines.append(f"{match.file}:{match.line}: {match.text}")

        path_display = str(candidate.resolve()) if resolution.is_external else relative_path
        summary = f"Found {total_occurrences} match(es) for {args.pattern!r} in {path_display}"
        if preview_lines:
            summary = summary + "\n" + "\n".join(preview_lines)
        truncated = len(matches) >= args.limit
        if truncated:
            summary += (
                "\n[TRUNCATED] Results truncated at "
                f"{args.limit} matching lines. Refine the path, include/exclude filters, "
                "or pattern before relying on this as complete."
            )
        diagnostics: list[dict[str, object]] = []
        if total_occurrences == 0:
            diagnostics.append(
                {
                    "source": self.definition.name,
                    "severity": "info",
                    "reason": "no_matches",
                    "message": (
                        "No matches found. Broaden the path/include filter, verify the search text with read, or use a plain string for literal text."
                    ),
                }
            )
        if truncated:
            diagnostics.append(
                {
                    "source": self.definition.name,
                    "severity": "warning",
                    "reason": "results_truncated",
                    "message": f"grep stopped after {args.limit} matching lines.",
                    "retry_guidance": (
                        "Refine path/include/exclude filters or use a more specific pattern before treating these results as complete."
                    ),
                }
            )

        body = GrepResultBody(
            path=path_display,
            pattern=args.pattern,
            regex=args.regex,
            ignore_case=args.ignore_case,
            context=args.context,
            match_count=total_occurrences,
            truncated=truncated,
            matches=tuple(matches),
            diagnostics=tuple(diagnostics),
        )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(summary, bounds=OutputBounds(truncated=truncated, partial=truncated)),
            body=body,
        )
