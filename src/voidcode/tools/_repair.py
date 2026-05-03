from __future__ import annotations

import difflib
import re
from typing import NoReturn


class ToolDiagnosticError(ValueError):
    def __init__(
        self,
        *,
        message: str,
        error_kind: str,
        error_details: dict[str, object] | None = None,
        retry_guidance: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_kind = error_kind
        self.error_details = dict(error_details or {})
        self.retry_guidance = retry_guidance


def raise_tool_diagnostic(
    *,
    message: str,
    error_kind: str,
    reason: str,
    retry_guidance: str | None = None,
    details: dict[str, object] | None = None,
) -> NoReturn:
    payload: dict[str, object] = {"reason": reason}
    if details:
        payload.update(details)
    raise ToolDiagnosticError(
        message=message,
        error_kind=error_kind,
        error_details=payload,
        retry_guidance=retry_guidance,
    )


def preview_line(text: str, *, max_length: int = 96) -> str:
    line = text.replace("\t", "\\t")
    if len(line) <= max_length:
        return line
    return f"{line[: max_length - 3]}..."


def bounded_block_preview(lines: list[str], start: int, length: int) -> str:
    context_before = 1
    context_after = 1
    first = max(0, start - context_before)
    last = min(len(lines), start + length + context_after)
    preview_lines: list[str] = []
    for index in range(first, last):
        marker = ">" if start <= index < start + length else " "
        preview_lines.append(f"    {marker} L{index + 1}: {preview_line(lines[index])}")
    return "\n".join(preview_lines)


def bounded_candidate_diff(expected: str, candidate: str, *, max_lines: int = 14) -> str:
    diff_lines = list(
        difflib.unified_diff(
            expected.splitlines(),
            candidate.splitlines(),
            fromfile="expected",
            tofile="current",
            lineterm="",
            n=1,
        )
    )
    if not diff_lines:
        return ""

    if len(diff_lines) > max_lines:
        diff_lines = [*diff_lines[: max_lines - 1], "... diff truncated ..."]

    return "\n".join(f"    {line}" for line in diff_lines)


def looks_line_number_prefixed(text: str) -> bool:
    non_empty_lines = [line for line in text.split("\n") if line.strip()]
    if not non_empty_lines:
        return False

    prefixed_count = sum(1 for line in non_empty_lines if re.match(r"^\s*\d+\s*[:|]\s?", line))
    return prefixed_count >= max(1, len(non_empty_lines) // 2)


def line_prefix_retry_guidance(*, argument_name: str = "oldString") -> str:
    return (
        f"{argument_name} appears to include read output line prefixes like '42: '; "
        "remove those prefixes and retry with only file text."
    )


def near_text_matches(
    content: str,
    expected: str,
    *,
    limit: int = 2,
    min_ratio: float = 0.58,
) -> list[tuple[float, int, str]]:
    expected_lines = expected.split("\n")
    if expected_lines and expected_lines[-1] == "":
        expected_lines = expected_lines[:-1]
    if not expected_lines:
        return []

    lines = content.split("\n")
    window_size = min(len(expected_lines), len(lines))
    if window_size == 0:
        return []

    candidates: list[tuple[float, int, str]] = []
    for start in range(len(lines) - window_size + 1):
        block = "\n".join(lines[start : start + window_size])
        ratio = difflib.SequenceMatcher(None, expected.strip(), block.strip()).ratio()
        if ratio >= min_ratio:
            candidates.append((ratio, start, block))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[:limit]


def format_text_repair_hints(
    *,
    content: str,
    expected: str,
    label: str = "Near-match hints",
    note_for_match: str = "near text match",
) -> list[str]:
    lines = content.split("\n")
    expected_lines = expected.split("\n")
    if expected_lines and expected_lines[-1] == "":
        expected_lines = expected_lines[:-1]
    window_size = min(len(expected_lines), len(lines))
    if window_size == 0:
        return []

    hints = []
    for ratio, start, block in near_text_matches(content, expected):
        hints.append(
            f"  - L{start + 1} ({round(ratio * 100)}% similar; {note_for_match})\n"
            f"{bounded_block_preview(lines, start, window_size)}\n"
            "    Diff (- expected, + current):\n"
            f"{bounded_candidate_diff(expected, block)}"
        )

    if not hints:
        return []
    return [f"{label}:", *hints]
