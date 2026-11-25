"""Model-visible detail text and typed result bodies for ``find``."""

from __future__ import annotations

from .models import (
    FindArgs,
    FindCoverage,
    FindLocation,
    FindResultBody,
    FindStatus,
    PrescreenNote,
    RejectedVerdict,
    SkippedMatch,
    SnippetCut,
    UnjudgedCandidate,
)


def _literal_detail(
    args: FindArgs,
    status: FindStatus,
    locations: list[FindLocation],
    *,
    skipped: tuple[SkippedMatch, ...],
    truncated: bool,
) -> str:
    lines = [f"find [{status}]: {len(locations)} literal location(s) for {args.query!r}"]
    lines.extend(f"{location.path}:{location.start_line}: {location.rationale}" for location in locations)
    if skipped:
        lines.append(f"{len(skipped)} match(es) skipped as unreadable")
    if truncated:
        lines.append("screening truncated; narrow path or pattern")
    lines.append("model calls: 0 (grep only)")
    return "\n".join(lines)


def _auto_detail(
    status: FindStatus,
    locations: list[FindLocation],
    *,
    dropped_results: int,
    rejected: list[RejectedVerdict],
    declined: int,
    unjudged: list[UnjudgedCandidate],
    snippet_cuts: tuple[SnippetCut, ...],
    dropped_terms: list[str],
    prescreen_notes: list[PrescreenNote],
    truncated: bool,
    reasons: list[str],
) -> str:
    lines = [f"find [{status}]: {len(locations)} verified location(s)"]
    for location in locations:
        lines.append(f"{location.path}:{location.start_line}-{location.end_line}: {location.rationale}")
    if dropped_results:
        lines.append(f"showing {len(locations)} of {len(locations) + dropped_results} verified location(s); raise limit to include more")
    if declined:
        lines.append(f"{declined} candidate(s) declined by the judge")
    if unjudged:
        lines.append(f"{len(unjudged)} candidate(s) received no verdict; judge coverage is incomplete")
    for rejection in rejected:
        lines.append(f"rejected {rejection.get('path')}:{rejection.get('line')} ({rejection.get('reason')})")
    if snippet_cuts:
        lines.append(f"judge snippet caps applied to {len(snippet_cuts)} file(s)")
    if dropped_terms:
        lines.append(f"{len(dropped_terms)} query term(s) dropped by the keyword cap: {', '.join(dropped_terms)}")
    failed_terms = [str(note["keyword"]) for note in prescreen_notes if "error" in note and "keyword" in note]
    if failed_terms:
        lines.append(f"prescreen failed for {len(failed_terms)} term(s): {', '.join(failed_terms)}")
    if truncated:
        lines.extend(f"screening truncated: {reason}" for reason in reasons)
    lines.append("model calls: 1")
    return "\n".join(lines)


def _build_body(
    *,
    status: FindStatus,
    detail: str,
    locations: list[FindLocation],
    keywords: tuple[str, ...],
    dropped: tuple[str, ...],
    prescreen: tuple[PrescreenNote, ...],
    screened: tuple[str, ...],
    judged: tuple[str, ...],
    skipped: tuple[SkippedMatch, ...],
    unjudged: tuple[UnjudgedCandidate, ...],
    snippet_cuts: tuple[SnippetCut, ...],
    rejected: tuple[RejectedVerdict, ...],
    declined: int,
    dropped_results: int,
    truncated: bool,
    reasons: tuple[str, ...],
    model_calls: int,
) -> FindResultBody:
    return FindResultBody(
        status=status,
        detail=detail,
        locations=tuple(locations),
        coverage=FindCoverage(
            keywords=keywords,
            dropped_terms=dropped,
            prescreen=prescreen,
            screened_files=screened,
            judged_files=judged,
            skipped=skipped,
            unjudged=unjudged,
            snippet_cuts=snippet_cuts,
            rejected=rejected,
            declined=declined,
            dropped_results=dropped_results,
            truncated=truncated,
            truncation_reasons=reasons,
            model_calls=model_calls,
        ),
        cost={"model_calls": model_calls, "candidates_judged": len(judged)},
    )
