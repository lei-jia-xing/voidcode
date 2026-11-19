"""Shared bounds, path utilities, diff helpers, and argument validators for file capabilities."""

from __future__ import annotations

import difflib
from io import StringIO
from pathlib import Path
from typing import Literal

from pydantic import ValidationInfo

from voidcode.sdk.tools import Change

#: Upper bound for an explicit ``limit`` so one call cannot materialize an
#: unbounded match list.
MAX_RESULT_LIMIT = 5000


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
        raise ValueError(f"Path escapes workspace: {raw_path}")
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


def normalize_line_endings(text: str) -> str:
    """Normalize CRLF and CR to standard LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def detect_line_ending(text: str) -> str:
    """Detect whether text uses CRLF or LF line endings."""
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def convert_line_endings(text: str, ending: str) -> str:
    """Convert text to the given line ending (CRLF or LF)."""
    normalized = normalize_line_endings(text)
    return normalized if ending == "\n" else normalized.replace("\n", "\r\n")


def read_utf8_text(path: Path, *, tool_name: str = "edit") -> str:
    """Read a UTF-8 text file or raise ValueError."""
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise ValueError(f"{tool_name} only supports UTF-8 text files") from exc


__all__ = [
    "MAX_RESULT_LIMIT",
    "convert_line_endings",
    "detect_line_ending",
    "make_file_change",
    "normalize_line_endings",
    "read_utf8_text",
    "resolve_workspace_path",
    "summarize_diff",
    "to_display_path",
    "validate_glob_patterns",
    "validate_non_empty",
]
