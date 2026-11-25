"""Verdict validation and read-verified evidence locations for ``find``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from voidcode.capabilities.files.find_queries import FindGrepMatch
from voidcode.capabilities.tool_errors import ToolDiagnosticError
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices

from .judge import _bounded_rationale
from .models import (
    _EVIDENCE_CONTEXT_LINES,
    _MAX_EVIDENCE_LINES,
    FindArgs,
    FindLocation,
    RejectedVerdict,
    SkippedMatch,
    UnjudgedCandidate,
)
from .prescreen import _Candidate
from .queries import _read_segment, _render_excerpt


def _accept_verdicts(
    args: FindArgs,
    candidates: list[_Candidate],
    verdicts: list[object],
    *,
    context: ToolContext,
    services: PluginServices,
) -> tuple[list[FindLocation], list[RejectedVerdict], int, list[UnjudgedCandidate]]:
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    seen: set[int] = set()
    locations: list[FindLocation] = []
    rejected: list[RejectedVerdict] = []
    declined = 0
    for verdict in verdicts:
        if not isinstance(verdict, Mapping):
            rejected.append({"path": None, "line": None, "reason": "verdict_not_object"})
            continue
        raw_id = verdict.get("candidate_id")
        candidate = by_id.get(raw_id) if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
        if candidate is None:
            rejected.append({"path": verdict.get("path"), "line": verdict.get("line"), "reason": "unknown_candidate"})
            continue
        if candidate.candidate_id in seen:
            rejected.append({"path": candidate.path, "line": verdict.get("line"), "reason": "duplicate_verdict"})
            continue
        seen.add(candidate.candidate_id)
        if verdict.get("verdict") != "match":
            declined += 1
            continue
        location_or_rejection = _accept_match(args, candidate, verdict, context=context, services=services)
        if isinstance(location_or_rejection, FindLocation):
            locations.append(location_or_rejection)
        else:
            rejected.append(location_or_rejection)
    unjudged: list[UnjudgedCandidate] = [
        {"candidate_id": candidate.candidate_id, "path": candidate.path, "reason": "missing_verdict"}
        for candidate in candidates
        if candidate.candidate_id not in seen
    ]
    return locations, rejected, declined, unjudged


def _accept_match(
    args: FindArgs,
    candidate: _Candidate,
    verdict: Mapping[str, object],
    *,
    context: ToolContext,
    services: PluginServices,
) -> FindLocation | RejectedVerdict:
    _ = args
    line = verdict.get("line")
    if verdict.get("path") != candidate.path:
        return {"path": candidate.path, "line": line, "reason": "path_mismatch"}
    if not isinstance(line, int) or isinstance(line, bool) or line not in candidate.match_lines:
        return {"path": candidate.path, "line": line, "reason": "line_not_prescreened"}
    shown = {number for number, _ in candidate.snippets}
    if line not in shown:
        return {"path": candidate.path, "line": line, "reason": "line_not_shown"}
    raw_end = verdict.get("end_line", line)
    end_line = raw_end if isinstance(raw_end, int) and not isinstance(raw_end, bool) else -1
    if end_line < line:
        return {"path": candidate.path, "line": line, "reason": "invalid_range"}
    span = end_line - line + 1
    if span + 2 * _EVIDENCE_CONTEXT_LINES > _MAX_EVIDENCE_LINES:
        return {
            "path": candidate.path,
            "line": line,
            "reason": "range_too_large",
            "max_span": _MAX_EVIDENCE_LINES - 2 * _EVIDENCE_CONTEXT_LINES,
        }
    rationale = verdict.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        return {"path": candidate.path, "line": line, "reason": "invalid_verdict_field"}
    rationale = _bounded_rationale(rationale)
    offset = max(1, line - _EVIDENCE_CONTEXT_LINES)
    limit = span + 2 * _EVIDENCE_CONTEXT_LINES
    try:
        page = _read_segment(candidate.path, offset=offset, limit=limit, tool_context=context, services=services)
    except ToolDiagnosticError as exc:
        reason = "line_out_of_range" if exc.reason == "offset_out_of_range" else "evidence_unreadable"
        return {
            "path": candidate.path,
            "line": line,
            "reason": reason,
            "error": exc.message,
            "error_kind": exc.error_kind,
            "details": dict(exc.error_details),
        }
    page_lines = {record.line for record in page.lines}
    if line not in page_lines:
        if page.line_count is not None and line > page.line_count:
            return {"path": candidate.path, "line": line, "reason": "line_out_of_range"}
        return {"path": candidate.path, "line": line, "reason": "evidence_page_clipped"}
    if end_line not in page_lines:
        if page.line_count is not None and end_line > page.line_count:
            return {"path": candidate.path, "line": line, "end_line": end_line, "reason": "line_out_of_range"}
        return {"path": candidate.path, "line": line, "end_line": end_line, "reason": "evidence_page_clipped"}
    return FindLocation(
        path=candidate.path,
        start_line=line,
        end_line=end_line,
        excerpt=_render_excerpt(page),
        rationale=rationale,
        via="judge",
        evidence_offset=offset,
        evidence_limit=limit,
    )


def _locations_from_matches(
    args: FindArgs,
    matches: tuple[FindGrepMatch, ...] | list[FindGrepMatch],
    *,
    via: Literal["grep", "grep_fallback"],
    context: ToolContext,
    services: PluginServices,
) -> tuple[list[FindLocation], tuple[SkippedMatch, ...]]:
    _ = args
    locations: list[FindLocation] = []
    skipped: list[SkippedMatch] = []
    for match in matches:
        offset = max(1, match.line - _EVIDENCE_CONTEXT_LINES)
        limit = 1 + 2 * _EVIDENCE_CONTEXT_LINES
        try:
            page = _read_segment(match.file, offset=offset, limit=limit, tool_context=context, services=services)
        except ToolDiagnosticError as exc:
            skipped.append(
                {
                    "path": match.file,
                    "reason": "unreadable",
                    "error": exc.message,
                    "error_kind": exc.error_kind,
                    "details": dict(exc.error_details),
                }
            )
            continue
        locations.append(
            FindLocation(
                path=match.file,
                start_line=match.line,
                end_line=match.line,
                excerpt=_render_excerpt(page),
                rationale=f"literal match for the query text (via {via})",
                via=via,
                evidence_offset=offset,
                evidence_limit=limit,
            )
        )
    return locations, tuple(skipped)
