from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

from ..security.path_policy import resolve_workspace_path
from ._repair import raise_tool_diagnostic
from .contracts import ToolResult
from .runtime_context import current_runtime_tool_context


@dataclass(frozen=True, slots=True)
class ReadTracking:
    """Paths revealed by read_file plus the exact 1-based line numbers seen."""

    read_paths: frozenset[str]
    read_lines: Mapping[str, frozenset[int]]


def read_tracking_for_tool_results(
    *,
    tool_results: tuple[ToolResult, ...],
    workspace: Path,
) -> ReadTracking:
    resolved_paths: set[str] = set()
    lines_by_path: dict[str, set[int]] = {}
    for result in tool_results:
        if result.tool_name != "read_file" or result.status != "ok":
            continue
        candidate = _resolve_internal_workspace_path(
            workspace=workspace,
            raw_path=_read_result_path(result),
        )
        if candidate is None:
            continue
        resolved = candidate.as_posix()
        resolved_paths.add(resolved)
        seen_lines = _read_result_lines(result)
        if seen_lines is not None:
            lines_by_path.setdefault(resolved, set()).update(seen_lines)
    return ReadTracking(
        read_paths=frozenset(resolved_paths),
        read_lines={path: frozenset(lines) for path, lines in lines_by_path.items()},
    )


def read_paths_for_tool_results(
    *,
    tool_results: tuple[ToolResult, ...],
    workspace: Path,
) -> frozenset[str]:
    return read_tracking_for_tool_results(
        tool_results=tool_results,
        workspace=workspace,
    ).read_paths


def _read_result_lines(result: ToolResult) -> frozenset[int] | None:
    """Extract the 1-based line numbers revealed by a read_file result.

    Returns ``None`` when the result carried no line data at all (e.g. an
    image/pdf attachment read), and an (possibly empty) frozenset when a text
    read revealed zero or more lines.
    """
    raw_lines = result.data.get("lines")
    if not isinstance(raw_lines, list):
        return None
    line_numbers: set[int] = set()
    for item in raw_lines:
        if isinstance(item, dict):
            line = item.get("line")
            if isinstance(line, int):
                line_numbers.add(line)
    return frozenset(line_numbers)


def enforce_read_before_write(
    *,
    tool_name: str,
    workspace: Path,
    raw_path: str,
    candidate: Path,
    display_path: str,
    is_external: bool,
) -> None:
    if is_external or not candidate.exists() or not candidate.is_file():
        return
    context = current_runtime_tool_context()
    if context is None:
        return
    if candidate.resolve().as_posix() in context.read_paths:
        return
    raise_tool_diagnostic(
        message=(f"{tool_name} requires reading the current file before modifying it: {display_path}"),
        error_kind="tool_input_mismatch",
        reason="write_without_read",
        retry_guidance=("Use read_file on the target path first, review the current content, then retry the change."),
        details={"path": display_path, "raw_path": raw_path},
    )


def enforce_seen_lines(
    *,
    tool_name: str,
    workspace: Path,
    raw_path: str,
    candidate: Path,
    display_path: str,
    is_external: bool,
    start_line: int,
    end_line: int,
) -> None:
    """Require every line in ``[start_line, end_line]`` (1-based, inclusive) to
    have been revealed by a prior read_file result.

    Fails closed: a file with no recorded line data rejects every change.
    """
    if is_external or not candidate.exists() or not candidate.is_file():
        return
    context = current_runtime_tool_context()
    if context is None:
        return
    resolved = candidate.resolve().as_posix()
    if resolved not in context.read_paths:
        raise_tool_diagnostic(
            message=(f"{tool_name} requires reading the current file before modifying it: {display_path}"),
            error_kind="tool_input_mismatch",
            reason="write_without_read",
            retry_guidance=("Use read_file on the target path first, review the current content, then retry the change."),
            details={"path": display_path, "raw_path": raw_path},
        )
    seen = context.read_lines.get(resolved)
    if seen is None:
        _raise_unseen_range(
            tool_name=tool_name,
            display_path=display_path,
            raw_path=raw_path,
            unseen_ranges=[(start_line, max(start_line, end_line))],
        )
    if start_line > end_line:
        return
    unseen_ranges = _unseen_ranges(seen, start_line, end_line)
    if unseen_ranges:
        _raise_unseen_range(
            tool_name=tool_name,
            display_path=display_path,
            raw_path=raw_path,
            unseen_ranges=unseen_ranges,
        )


def enforce_seen_whole_file(
    *,
    tool_name: str,
    workspace: Path,
    raw_path: str,
    candidate: Path,
    display_path: str,
    is_external: bool,
) -> None:
    """Require every line of an existing file to have been revealed by read_file."""
    if is_external or not candidate.exists() or not candidate.is_file():
        return
    content = candidate.read_text(encoding="utf-8")
    total_lines = len(content.splitlines())
    enforce_seen_lines(
        tool_name=tool_name,
        workspace=workspace,
        raw_path=raw_path,
        candidate=candidate,
        display_path=display_path,
        is_external=is_external,
        start_line=1,
        end_line=total_lines,
    )


def _unseen_ranges(seen: frozenset[int], start_line: int, end_line: int) -> list[tuple[int, int]]:
    missing = sorted(line for line in range(start_line, end_line + 1) if line not in seen)
    ranges: list[tuple[int, int]] = []
    for line in missing:
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], line)
        else:
            ranges.append((line, line))
    return ranges


def _raise_unseen_range(
    *,
    tool_name: str,
    display_path: str,
    raw_path: str,
    unseen_ranges: list[tuple[int, int]],
) -> NoReturn:
    def _format_range(start: int, end: int) -> str:
        if start > end:
            return f"line {start}"
        return f"line {start}" if start == end else f"lines {start}-{end}"

    rendered = ", ".join(_format_range(start, end) for start, end in unseen_ranges)
    was_were = "was" if len(unseen_ranges) == 1 else "were"
    raise_tool_diagnostic(
        message=(f"{tool_name} cannot modify {display_path}: {rendered} {was_were} never revealed by read_file."),
        error_kind="tool_input_mismatch",
        reason="unseen_range",
        retry_guidance=(
            "Use read_file on the target path to reveal the missing lines first "
            "(continue reading with data.next_offset until data.next_offset is None), "
            "then retry the change against the current content."
        ),
        details={
            "path": display_path,
            "raw_path": raw_path,
            "unseen_line_ranges": [{"start": start, "end": end} for start, end in unseen_ranges],
        },
    )


def _read_result_path(result: ToolResult) -> str | None:
    raw_arguments = result.data.get("arguments")
    if isinstance(raw_arguments, dict):
        arguments = cast(dict[str, object], raw_arguments)
        raw_path = arguments.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            return raw_path
    raw_path = result.data.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        return raw_path
    return None


def _resolve_internal_workspace_path(*, workspace: Path, raw_path: str | None) -> Path | None:
    if raw_path is None or not raw_path.strip():
        return None
    resolution = resolve_workspace_path(
        workspace=workspace,
        raw_path=raw_path,
        allow_outside_workspace=True,
    )
    if resolution.is_external:
        return None
    return resolution.candidate.resolve()


__all__ = [
    "ReadTracking",
    "enforce_read_before_write",
    "enforce_seen_lines",
    "enforce_seen_whole_file",
    "read_paths_for_tool_results",
    "read_tracking_for_tool_results",
]
