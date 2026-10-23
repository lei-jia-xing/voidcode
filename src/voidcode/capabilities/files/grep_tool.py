from __future__ import annotations

import fnmatch
import re
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import OutputBounds, TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolResult, ToolSuccess

from .gitignore import GitIgnoreMatcher
from .repair import raise_tool_diagnostic

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
    model_config = ConfigDict(extra="forbid", strict=True)

    pattern: str
    path: str = "."
    regex: bool = False
    ignore_case: bool = False
    context: int = Field(default=0, ge=0)
    include: list[str] | None = None
    exclude: list[str] | None = None
    respect_gitignore: bool = False
    limit: int = Field(default=MAX_MATCHES, ge=1, le=MAX_RESULT_LIMIT)

    @field_validator("pattern", "path", mode="after")
    @classmethod
    def _validate_non_empty(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value

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
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text to search for; treated literally unless regex=true."},
                "path": {"type": "string", "description": "File or directory to search, relative to the workspace; defaults to workspace root."},
                "regex": {"type": "boolean", "description": "Treat pattern as a regular expression; defaults to false."},
                "ignore_case": {"type": "boolean", "description": "Match case-insensitively; defaults to false."},
                "context": {"type": "integer", "minimum": 0, "description": "Number of surrounding lines to include for each match."},
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
        supports_parallel=True,
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
        include: list[str] | None,
        exclude: list[str] | None,
        gitignore: GitIgnoreMatcher | None = None,
    ) -> Iterator[Path]:
        include_patterns = include or []
        exclude_patterns = exclude or []
        if root.is_file():
            yield root
            return

        for directory, dirnames, filenames in root.walk(top_down=True):
            directory_relative = directory.relative_to(project_root)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in DEFAULT_IGNORE_PATTERNS
                and (gitignore is None or not gitignore.is_ignored((directory_relative / name).as_posix(), is_directory=True))
            )
            for name in sorted(filenames):
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
    ) -> Iterator[GrepResultMatch]:
        before: deque[tuple[int, str]] = deque(maxlen=context)
        line_number = 0

        def lines() -> Iterator[tuple[int, str]]:
            nonlocal line_number
            with path.open("r", encoding="utf-8", newline="") as stream:
                for raw_line in stream:
                    line_number += 1
                    yield line_number, raw_line.rstrip("\r\n")

        try:
            remaining = iter(lines())
            following = deque(islice(remaining, context + 1))
            while following:
                current = following.popleft()
                columns = tuple(match.start() + 1 for match in pattern.finditer(current[1]))
                if columns:
                    yield GrepResultMatch(
                        relative_path,
                        current[0],
                        current[1],
                        columns,
                        tuple({"line": number, "text": text} for number, text in before),
                        tuple({"line": number, "text": text} for number, text in following),
                    )
                before.append(current)
                try:
                    following.append(next(remaining))
                except StopIteration:
                    pass
        except UnicodeDecodeError, OSError:
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
        requested_path = Path(args.path)
        candidate = (requested_path if requested_path.is_absolute() else workspace_root / requested_path).resolve()
        is_external = not candidate.is_relative_to(workspace_root)
        relative_path = candidate.as_posix() if is_external else candidate.relative_to(workspace_root).as_posix()

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
            include=args.include,
            exclude=args.exclude,
            gitignore=gitignore,
        )

        matches: list[GrepResultMatch] = []
        truncated = False
        for target in targets:
            file_display = str(target.resolve()) if is_external else target.relative_to(workspace_root).as_posix()
            for match in self._iter_matches(target, relative_path=file_display, pattern=pattern, context=args.context):
                if len(matches) == args.limit:
                    truncated = True
                    break
                matches.append(match)
            if truncated:
                break

        total_occurrences = sum(len(match.columns) for match in matches)
        path_display = str(candidate.resolve()) if is_external else relative_path
        preview_lines: dict[tuple[str, int], tuple[str, tuple[int, ...]]] = {}
        for match in matches:
            for line in (*match.before, {"line": match.line, "text": match.text}, *match.after):
                line_number = line["line"]
                line_text = line["text"]
                if not isinstance(line_number, int) or not isinstance(line_text, str):
                    continue
                columns = match.columns if line_number == match.line else ()
                key = (match.file, line_number)
                if key not in preview_lines or columns:
                    preview_lines[key] = (line_text, columns)
        rendered_lines = [f"{file}:{line}:{','.join(map(str, columns)) or '-'}: {text}" for (file, line), (text, columns) in preview_lines.items()]
        summary = f"Found {total_occurrences} match(es) for {args.pattern!r} in {path_display}"
        if rendered_lines:
            summary += "\n" + "\n".join(rendered_lines)
        if truncated:
            summary += f"\n[TRUNCATED] Results truncated at {args.limit} matching lines."

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
