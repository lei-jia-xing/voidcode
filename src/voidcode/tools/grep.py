from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast, final

from pydantic import ValidationError

from ..security.path_policy import resolve_workspace_path as resolve_workspace_path_policy
from ._pydantic_args import GrepArgs, format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult

MAX_MATCHES = 200
DEFAULT_IGNORE_PATTERNS = frozenset(
    (
        ".git",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
    )
)


@dataclass(frozen=True, slots=True)
class _GrepMatch:
    file: str
    line: int
    text: str
    columns: list[int]
    before: list[dict[str, object]]
    after: list[dict[str, object]]


@final
class GrepTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="grep",
        description=(
            "Search for a literal or regex pattern in files inside the current workspace."
        ),
        input_schema={
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "regex": {"type": "boolean"},
            "context": {"type": "integer"},
            "include": {"type": "array", "items": {"type": "string"}},
            "exclude": {"type": "array", "items": {"type": "string"}},
        },
        read_only=True,
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
            if any(
                part in DEFAULT_IGNORE_PATTERNS
                for part in candidate.relative_to(project_root).parts
            ):
                continue
            if include_patterns and not any(
                GrepTool._matches_glob(rel, pat) for pat in include_patterns
            ):
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
        except (UnicodeDecodeError, OSError):
            return None

    @staticmethod
    def _context_lines(
        lines: list[str], start: int, end: int, *, context: int
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        context = max(0, context)
        before = [
            cast(dict[str, object], {"line": line_no + 1, "text": lines[line_no]})
            for line_no in range(max(0, start - context), start)
        ]
        after = [
            cast(dict[str, object], {"line": line_no + 1, "text": lines[line_no]})
            for line_no in range(end + 1, min(len(lines), end + 1 + context))
        ]
        return before, after

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = GrepArgs.model_validate(
                {
                    "pattern": call.arguments.get("pattern"),
                    "path": call.arguments.get("path"),
                    "regex": call.arguments.get("regex", False),
                    "context": call.arguments.get("context", 0),
                    "include": call.arguments.get("include"),
                    "exclude": call.arguments.get("exclude"),
                }
            )
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

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
        effective_root = (
            candidate if resolution.is_external and candidate.is_dir() else workspace_root
        )

        try:
            pattern = re.compile(args.pattern if args.regex else re.escape(args.pattern))
        except re.error as exc:
            raise ValueError(
                "grep Validation error: pattern: invalid regex pattern "
                f"({exc.msg}) (received str). "
                "Please retry with corrected arguments that satisfy the tool schema."
            ) from exc
        targets = self._collect_targets(
            candidate,
            project_root=effective_root,
            include=args.include,
            exclude=args.exclude,
        )

        matches: list[_GrepMatch] = []
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
                    _GrepMatch(
                        file=(
                            str(target.resolve())
                            if resolution.is_external
                            else target.relative_to(workspace_root).as_posix()
                        ),
                        line=line_index + 1,
                        text=line_text,
                        columns=columns,
                        before=before,
                        after=after,
                    )
                )
                if len(matches) >= MAX_MATCHES:
                    break
            if len(matches) >= MAX_MATCHES:
                break

        total_occurrences = sum(len(match.columns) for match in matches)
        preview_lines: list[str] = []
        for match in matches[:10]:
            preview_lines.append(f"{match.file}:{match.line}: {match.text}")

        path_display = str(candidate.resolve()) if resolution.is_external else relative_path
        summary = f"Found {total_occurrences} match(es) for {args.pattern!r} in {path_display}"
        if preview_lines:
            summary = summary + "\n" + "\n".join(preview_lines)

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=summary,
            data={
                "path": path_display,
                "pattern": args.pattern,
                "regex": args.regex,
                "context": args.context,
                "match_count": total_occurrences,
                "truncated": len(matches) >= MAX_MATCHES,
                "partial": len(matches) >= MAX_MATCHES,
                "matches": [
                    {
                        "file": match.file,
                        "line": match.line,
                        "text": match.text,
                        "columns": match.columns,
                        "before": match.before,
                        "after": match.after,
                    }
                    for match in matches
                ],
            },
            truncated=len(matches) >= MAX_MATCHES,
            partial=len(matches) >= MAX_MATCHES,
        )
