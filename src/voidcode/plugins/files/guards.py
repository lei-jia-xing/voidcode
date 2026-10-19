from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from voidcode.sdk.invocation import ToolContext

from .repair import raise_tool_diagnostic


def _coverage(context: ToolContext) -> tuple[frozenset[str], Mapping[tuple[str, str], frozenset[int]], frozenset[tuple[str, str]], str | None]:
    """Coverage the kernel observed for this session; always present, possibly empty."""
    return (context.read_paths, context.read_lines, context.read_whole_files, context.read_hash)


@dataclass(frozen=True, slots=True)
class ReadTracking:
    """Read observations grouped by canonical file path and source content hash."""

    read_paths: frozenset[str]
    read_lines: Mapping[tuple[str, str], frozenset[int]]
    read_whole_files: frozenset[tuple[str, str]]


def enforce_read_before_write(
    *,
    context: ToolContext,
    tool_name: str,
    raw_path: str,
    candidate: Path,
    display_path: str,
) -> None:
    read_paths, _read_lines, _read_whole_files, _read_hash = _coverage(context)
    if context.session_id is None:
        return
    context.require_session_id()
    if not candidate.exists() or not candidate.is_file():
        return
    if candidate.resolve().as_posix() in read_paths:
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
    raw_path: str,
    candidate: Path,
    display_path: str,
    start_line: int,
    end_line: int,
) -> None:
    """Require every line in ``[start_line, end_line]`` (1-based, inclusive) to
    have been revealed by a prior read result.

    Fails closed: a file with no recorded line data rejects every change.
    """
    read_paths, _read_lines, _read_whole_files, _read_hash = _coverage(context)
    if context.session_id is None:
        return
    context.require_session_id()
    if not candidate.exists() or not candidate.is_file():
        return
    resolved = candidate.resolve().as_posix()
    if resolved not in read_paths:
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
    raw_path: str,
    candidate: Path,
    display_path: str,
    expected_hash: str | None = None,
) -> None:
    """Require a complete, non-truncated file observation before overwrite."""
    read_paths, _read_lines, read_whole_files, _read_hash = _coverage(context)
    if context.session_id is None or not candidate.exists() or not candidate.is_file():
        return
    context.require_session_id()
    resolved = candidate.resolve().as_posix()
    if resolved not in read_paths:
        raise_tool_diagnostic(
            message=(f"{tool_name} requires reading the current file before modifying it: {display_path}"),
            error_kind="tool_input_mismatch",
            reason="write_without_read",
            retry_guidance=("Use read on the target path first, review the current content, then retry the change."),
            details={"path": display_path, "raw_path": raw_path},
        )
    content = candidate.read_bytes()
    actual_hash = hashlib.sha256(content).hexdigest()
    if expected_hash is not None and expected_hash != actual_hash:
        raise_tool_diagnostic(
            message=f"{tool_name} rejected because the file changed since it was read (stale edit).",
            error_kind="stale_edit",
            reason="content_hash_mismatch",
            retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry.",
            details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": display_path},
        )
    # Require an authorized whole-file read of this file with the same actual SHA-256
    total_lines = len(candidate.read_text(encoding="utf-8").splitlines())
    enforce_seen_lines(
        context=context,
        tool_name=tool_name,
        raw_path=raw_path,
        candidate=candidate,
        display_path=display_path,
        start_line=1,
        end_line=total_lines,
    )
    if (resolved, actual_hash) not in read_whole_files:
        stale_reads = [h for (p, h) in read_whole_files if p == resolved and h != actual_hash]
        if stale_reads:
            raise_tool_diagnostic(
                message=f"{tool_name} rejected because the file changed since it was read (stale edit).",
                error_kind="stale_edit",
                reason="content_hash_mismatch",
                retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry.",
                details={"actual_hash": actual_hash, "path": display_path},
            )
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


def _seen_lines_for_context(context: ToolContext, path: str, *, current_hash: str | None = None) -> frozenset[int] | None:
    """Lines revealed for ``path``; ``current_hash`` binds the lookup to the exact version being edited."""
    _read_paths, read_lines, _read_whole_files, read_hash = _coverage(context)
    bound_hash = read_hash if current_hash is None else current_hash
    if bound_hash is not None:
        return read_lines.get((path, bound_hash))
    observations = [lines for (observed_path, _), lines in read_lines.items() if observed_path == path]
    return observations[0] if len(observations) == 1 else None


def _has_whole_file_for_context(context: ToolContext, path: str) -> bool:
    _read_paths, _read_lines, read_whole_files, read_hash = _coverage(context)
    if read_hash is not None:
        return (path, read_hash) in read_whole_files
    observations = [key for key in read_whole_files if key[0] == path]
    return len(observations) == 1


class CharProvenanceTracker:
    """Tracks character-level line provenance across sequential edits.

    Characters from the original file map to their 1-based original line numbers.
    Characters inserted by earlier edits map to None (known own-generated).
    When a later edit touches text, any original lines covered must have been seen.
    """

    def __init__(self, original_content: str) -> None:
        self._content = original_content
        #: The exact version this tracker started from. Line coverage binds to this hash, never to
        #: the context's read_hash, which is None once two versions of the path have been observed.
        self._base_hash = hashlib.sha256(original_content.encode("utf-8")).hexdigest()
        prov: list[int | None] = []
        line_num = 1
        for ch in original_content:
            prov.append(line_num)
            if ch == "\n":
                line_num += 1
        self._provenance: list[int | None] = prov

    @property
    def content(self) -> str:
        return self._content

    def check_seen_lines(
        self,
        start_idx: int,
        end_idx: int,
        *,
        context: ToolContext,
        resolved_path: str,
        tool_name: str,
        display_path: str,
        raw_path: str,
        is_external: bool,
    ) -> None:
        read_paths, _read_lines, _read_whole_files, _read_hash = _coverage(context)
        if context.session_id is None or is_external:
            return
        context.require_session_id()
        if resolved_path not in read_paths:
            raise_tool_diagnostic(
                message=f"{tool_name} requires reading the current file before modifying it: {display_path}",
                error_kind="tool_input_mismatch",
                reason="write_without_read",
                retry_guidance="Use read on the target path first, review the current content, then retry the change.",
                details={"path": display_path, "raw_path": raw_path},
            )
        seen = _seen_lines_for_context(context, resolved_path, current_hash=self._base_hash)
        if seen is None:
            _raise_unseen_range(
                tool_name=tool_name,
                display_path=display_path,
                raw_path=raw_path,
                unseen_ranges=[(1, 1)],
            )
        touched_original_lines = {p for p in self._provenance[start_idx:end_idx] if p is not None}
        if not touched_original_lines:
            return
        unseen_ranges = _unseen_ranges(seen, min(touched_original_lines), max(touched_original_lines))
        # Filter unseen ranges to only include lines that were actually touched
        filtered_unseen: list[tuple[int, int]] = []
        for u_start, u_end in unseen_ranges:
            missing = [line for line in range(u_start, u_end + 1) if line in touched_original_lines]
            if missing:
                ranges: list[tuple[int, int]] = []
                for line in missing:
                    if ranges and line == ranges[-1][1] + 1:
                        ranges[-1] = (ranges[-1][0], line)
                    else:
                        ranges.append((line, line))
                filtered_unseen.extend(ranges)
        if filtered_unseen:
            _raise_unseen_range(
                tool_name=tool_name,
                display_path=display_path,
                raw_path=raw_path,
                unseen_ranges=filtered_unseen,
            )

    def apply_replacement(self, start_idx: int, end_idx: int, new_text: str) -> None:
        self._content = self._content[:start_idx] + new_text + self._content[end_idx:]
        self._provenance = self._provenance[:start_idx] + [None] * len(new_text) + self._provenance[end_idx:]


__all__ = [
    "CharProvenanceTracker",
    "ReadTracking",
    "enforce_read_before_write",
    "enforce_seen_lines",
    "enforce_seen_whole_file",
]
