"""Shared bounds, path utilities, diff helpers, and argument validators for file capabilities."""

from __future__ import annotations

import difflib
import re
from io import StringIO
from pathlib import Path, PurePath
from typing import Literal

from pydantic import ValidationInfo

from voidcode.sdk.tools import Change

#: Upper bound for an explicit ``limit`` so one call cannot materialize an
#: unbounded match list.
MAX_RESULT_LIMIT = 5000

#: Maximum UTF-8 bytes of caller-supplied replacement text in one mutation call.
#:
#: The bound is on what the caller sends (``write.content``, the text edits'
#: ``newString``, or the range edits' ``newText``), never on the size of the
#: resulting file: editing a large file stays possible, while one call cannot
#: inject an unbounded payload.
MAX_MUTATION_CONTENT_BYTES = 64 * 1024

#: Directory names file-discovery tools never descend into by default.
#:
#: Entries are exact directory names matched at any depth, never globs or path
#: fragments, and they apply only while walking below the requested search
#: root: an explicit ``path`` that names one of these directories is searched,
#: because the caller asked for it directly.
DEFAULT_IGNORE_PATTERNS = frozenset(
    {
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
    }
)


def is_default_ignored(relative: PurePath, *, is_directory: bool) -> bool:
    """True when a directory component of ``relative`` is default-ignored.

    Only directory components count: a regular file named like an entry (for
    example ``build``) is not hidden from directory listings.
    """
    parts = relative.parts if is_directory else relative.parts[:-1]
    return any(part in DEFAULT_IGNORE_PATTERNS for part in parts)


def has_hidden_component(relative: PurePath) -> bool:
    """True when any component of ``relative`` starts with ``.``."""
    return any(part.startswith(".") for part in relative.parts)


def full_match_path(relative_path: str, pattern: str, *, basename_any_depth: bool = False) -> bool:
    """One glob dialect for every file-discovery tool.

    ``pathlib`` ``full_match`` semantics. With ``basename_any_depth`` a pattern
    without a separator is anchored to the basename at any depth (ripgrep-like
    filters: ``*.py`` selects every Python file); otherwise the pattern is
    anchored to the given path, which is what ``glob``'s search-relative
    patterns mean. Callers choose which path to match (the search-relative path
    for ``glob``, the workspace-relative path for ``grep``'s filters).
    """
    if basename_any_depth and "/" not in pattern and not pattern.startswith("**/"):
        pattern = f"**/{pattern}"
    return PurePath(relative_path).full_match(pattern)


def validate_non_empty(value: str, info: ValidationInfo) -> str:
    if not value.strip():
        raise ValueError(f"{info.field_name} must not be empty")
    return value


def validate_glob_patterns(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if not all(item.strip() for item in value):
        raise ValueError("glob patterns must be non-empty strings")
    return value


def to_display_path(path: Path, workspace_root: Path) -> str:
    """Format path as relative to workspace root when inside, or as_posix() when external."""
    resolved_workspace = workspace_root.resolve()
    resolved_path = path.resolve()
    if resolved_path.is_relative_to(resolved_workspace):
        return resolved_path.relative_to(resolved_workspace).as_posix()
    return resolved_path.as_posix()


class PathEscapesWorkspaceError(ValueError):
    """A resolved path lies outside the workspace and the caller forbade it."""


def resolve_workspace_path(
    workspace: Path,
    raw_path: str | Path,
    *,
    allow_outside_workspace: bool = True,
) -> tuple[Path, str]:
    """Resolve candidate path against workspace root and format display path.

    Returns:
        tuple[Path, str]: The fully resolved absolute target Path and its display string
        (relative to workspace root if inside workspace, else absolute posix path).
    """
    workspace_root = workspace.resolve()
    requested = Path(raw_path)
    candidate = (requested if requested.is_absolute() else workspace_root / requested).resolve()
    is_external = not candidate.is_relative_to(workspace_root)
    if not allow_outside_workspace and is_external:
        raise PathEscapesWorkspaceError(f"Path escapes workspace: {raw_path}")
    display_path = candidate.as_posix() if is_external else candidate.relative_to(workspace_root).as_posix()
    return candidate, display_path


def make_file_change(
    path: str,
    operation: Literal["create", "update", "delete", "move"] = "update",
    *,
    old_path: str | None = None,
    content_hash: str | None = None,
    scratch: bool = False,
) -> Change:
    """Create a typed filesystem Change fact."""
    return Change(
        path=path,
        operation=operation,
        old_path=old_path,
        content_hash=content_hash,
        scratch=scratch,
    )


def summarize_diff(*, path: Path | str, before: str, after: str) -> tuple[str, int, int]:
    """Calculate unified diff and line addition/deletion counts between two text states."""
    lines = list(
        difflib.unified_diff(
            StringIO(before, newline="").readlines(),
            StringIO(after, newline="").readlines(),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    additions = sum(line.startswith("+") for line in lines[2:])
    deletions = sum(line.startswith("-") for line in lines[2:])
    diff = "".join(line if line.endswith(("\r", "\n")) else line + "\n\\ No newline at end of file\n" for line in lines)
    return diff, additions, deletions


_LINE_ENDING_PATTERN = re.compile(r"\r\n|\r|\n")


def normalize_line_endings(text: str) -> str:
    """Normalize CRLF and CR to standard LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def line_ending_kinds(text: str) -> frozenset[str]:
    """The distinct line-ending tokens present: LF, CRLF, and/or lone CR."""
    return frozenset(_LINE_ENDING_PATTERN.findall(text))


def encode_line_endings(text: str, ending: str | None) -> str:
    """Re-encode LF text with one uniform file ending; ``None`` keeps LF."""
    if ending is None or ending == "\n":
        return text
    return text.replace("\n", ending)


__all__ = [
    "DEFAULT_IGNORE_PATTERNS",
    "MAX_MUTATION_CONTENT_BYTES",
    "MAX_RESULT_LIMIT",
    "PathEscapesWorkspaceError",
    "encode_line_endings",
    "full_match_path",
    "has_hidden_component",
    "is_default_ignored",
    "line_ending_kinds",
    "make_file_change",
    "normalize_line_endings",
    "resolve_workspace_path",
    "summarize_diff",
    "to_display_path",
    "validate_glob_patterns",
    "validate_non_empty",
]
