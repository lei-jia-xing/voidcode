from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from ..core.tool_context import ToolContext
from ..core.turns import ReportedCall
from ..security.path_policy import resolve_workspace_path
from ._repair import raise_tool_diagnostic
from .read import ReadResultBody


@dataclass(frozen=True, slots=True)
class ReadTracking:
    """Read observations grouped by canonical file path and source content hash."""

    read_paths: frozenset[str]
    read_lines: Mapping[tuple[str, str], frozenset[int]]
    read_whole_files: frozenset[tuple[str, str]]


def read_tracking_for_tool_results(
    *,
    tool_results: tuple[ReportedCall, ...],
    workspace: Path,
) -> ReadTracking:
    resolved_paths: set[str] = set()
    lines_by_hash: dict[tuple[str, str], set[int]] = {}
    line_counts: dict[tuple[str, str], int] = {}
    inconsistent_counts: set[tuple[str, str]] = set()
    clipped_files: set[tuple[str, str]] = set()
    whole_file_observations: set[tuple[str, str]] = set()
    for report in tool_results:
        if report.final_tool_name != "read" or report.result.status != "ok":
            continue
        candidate = _resolve_internal_workspace_path(
            workspace=workspace,
            raw_path=_read_result_path(report),
        )
        if candidate is None:
            continue
        resolved = candidate.as_posix()
        resolved_paths.add(resolved)
        body = report.result.body
        if not isinstance(body, ReadResultBody):
            continue
        key = (resolved, body.content_hash)
        prior_count = line_counts.setdefault(key, body.line_count)
        if prior_count != body.line_count:
            inconsistent_counts.add(key)
        lines_by_hash.setdefault(key, set()).update(line.line for line in body.lines if not line.truncated)
        if any(line.truncated for line in body.lines):
            clipped_files.add(key)
        if body.whole_file:
            whole_file_observations.add(key)

    whole_files = set(whole_file_observations)
    for key, seen in lines_by_hash.items():
        if key in inconsistent_counts or key in clipped_files:
            whole_files.discard(key)
            continue
        line_count = line_counts[key]
        covers_file = not seen if line_count == 0 else len(seen) == line_count and min(seen) == 1 and max(seen) == line_count
        if covers_file:
            whole_files.add(key)
    return ReadTracking(
        read_paths=frozenset(resolved_paths),
        read_lines={key: frozenset(lines) for key, lines in lines_by_hash.items()},
        read_whole_files=frozenset(whole_files),
    )


def enforce_read_before_write(
    *,
    context: ToolContext,
    tool_name: str,
    workspace: Path,
    raw_path: str,
    candidate: Path,
    display_path: str,
    is_external: bool,
) -> None:
    _ = workspace
    if context.session_id is None:
        return
    context.require_session_id()
    if is_external or not candidate.exists() or not candidate.is_file():
        return
    if candidate.resolve().as_posix() in context.read_paths:
        return
    raise_tool_diagnostic(
        message=(f"{tool_name} requires reading the current file before modifying it: {display_path}"),
        error_kind="tool_input_mismatch",
        reason="write_without_read",
        retry_guidance=("Use read on the target path first, review the current content, then retry the change."),
        details={"path": display_path, "raw_path": raw_path},
    )


def enforce_seen_lines(
    *,
    context: ToolContext,
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
    have been revealed by a prior read result.

    Fails closed: a file with no recorded line data rejects every change.
    """
    _ = workspace
    if context.session_id is None:
        return
    context.require_session_id()
    if is_external or not candidate.exists() or not candidate.is_file():
        return
    resolved = candidate.resolve().as_posix()
    if resolved not in context.read_paths:
        raise_tool_diagnostic(
            message=(f"{tool_name} requires reading the current file before modifying it: {display_path}"),
            error_kind="tool_input_mismatch",
            reason="write_without_read",
            retry_guidance=("Use read on the target path first, review the current content, then retry the change."),
            details={"path": display_path, "raw_path": raw_path},
        )
    seen = _seen_lines_for_context(context, resolved)
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
    context: ToolContext,
    tool_name: str,
    workspace: Path,
    raw_path: str,
    candidate: Path,
    display_path: str,
    is_external: bool,
) -> None:
    """Require a complete, non-truncated file observation before overwrite."""
    if context.session_id is None or is_external or not candidate.exists() or not candidate.is_file():
        return
    context.require_session_id()
    content = candidate.read_text(encoding="utf-8")
    total_lines = len(content.splitlines())
    enforce_seen_lines(
        context=context,
        tool_name=tool_name,
        workspace=workspace,
        raw_path=raw_path,
        candidate=candidate,
        display_path=display_path,
        is_external=is_external,
        start_line=1,
        end_line=total_lines,
    )
    if not _has_whole_file_for_context(context, candidate.resolve().as_posix()):
        raise_tool_diagnostic(
            message=f"{tool_name} requires a complete, non-truncated read of {display_path} before replacing it.",
            error_kind="tool_input_mismatch",
            reason="incomplete_read",
            retry_guidance=(
                "Read the file from its first line until no next offset remains; "
                "if a line is clipped, use a line-scoped edit instead of replacing the whole file."
            ),
            details={"path": display_path, "raw_path": raw_path},
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
        message=(f"{tool_name} cannot modify {display_path}: {rendered} {was_were} never revealed by read."),
        error_kind="tool_input_mismatch",
        reason="unseen_range",
        retry_guidance=(
            "Use read on the target path to reveal the missing lines first "
            "(continue with the next offset shown in the read output until no next offset is returned), "
            "then retry the change against the current content."
        ),
        details={
            "path": display_path,
            "raw_path": raw_path,
            "unseen_line_ranges": [{"start": start, "end": end} for start, end in unseen_ranges],
        },
    )


def _seen_lines_for_context(context: ToolContext, path: str) -> frozenset[int] | None:
    if context.read_hash is not None:
        return context.read_lines.get((path, context.read_hash))
    observations = [lines for (observed_path, _), lines in context.read_lines.items() if observed_path == path]
    return observations[0] if len(observations) == 1 else None


def _has_whole_file_for_context(context: ToolContext, path: str) -> bool:
    if context.read_hash is not None:
        return (path, context.read_hash) in context.read_whole_files
    observations = [key for key in context.read_whole_files if key[0] == path]
    return len(observations) == 1


def _read_result_path(report: ReportedCall) -> str | None:
    raw_path = report.authorized_arguments.get("path")
    return raw_path if isinstance(raw_path, str) and raw_path.strip() else None


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
    "read_tracking_for_tool_results",
]
