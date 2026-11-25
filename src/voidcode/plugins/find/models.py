"""Arguments, statuses, bounds, and typed result bodies for ``find``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Required, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidcode.capabilities.files._common import validate_glob_patterns, validate_non_empty
from voidcode.sdk.tools import ToolDiagnosticKind

#: Bounds the per-keyword grep fan-out of one find invocation; extra query
#: terms are dropped and reported in coverage.
_MAX_KEYWORDS = 8
#: Bounds the judge prompt: match lines shown per candidate file.
_MAX_SNIPPET_MATCHES_PER_FILE = 12
#: Bounds the judge prompt: snippet line width.
_MAX_SNIPPET_LINE_CHARS = 500
#: Bounds one returned judge rationale in the typed body and model text.
_MAX_RATIONALE_CHARS = 500
#: Bounds one user-supplied behavior description before it enters the judge prompt.
_MAX_QUERY_CHARS = 1000
#: Upper bound for the per-call judge output; the default stays at 2000.
_MAX_OUTPUT_TOKENS = 8000
#: Read-tool page limit; evidence spans must fit one page.
_MAX_EVIDENCE_LINES = 2000
#: Core-side limit for one model-query prompt; keep in sync with model_query._turn.
_MAX_MODEL_QUERY_PROMPT_BYTES = 64 * 1024
#: Evidence excerpt window around an accepted line range.
_EVIDENCE_CONTEXT_LINES = 2

FindStatus = Literal["ok", "no_hit", "partial", "judge_unavailable", "judge_error"]
JudgeFailureStatus = Literal["judge_unavailable", "judge_error"]
type SkippedReason = Literal["candidate_cap", "prompt_budget", "unreadable"]
type RejectedReason = Literal[
    "verdict_not_object",
    "unknown_candidate",
    "duplicate_verdict",
    "path_mismatch",
    "line_not_prescreened",
    "line_not_shown",
    "invalid_range",
    "range_too_large",
    "invalid_verdict_field",
    "line_out_of_range",
    "evidence_unreadable",
    "evidence_page_clipped",
]
type PrescreenSkip = Literal["prescreen match cap reached"]


class PrescreenNote(TypedDict, total=False):
    # Keyword-prescreen notes carry ``keyword``; literal/fallback notes carry ``pattern``/``fallback``.
    keyword: str
    pattern: str
    fallback: Literal["literal"]
    matches: int
    files: int
    truncated: bool
    skipped: PrescreenSkip
    error: str


class SkippedMatch(TypedDict, total=False):
    path: Required[str]
    reason: Required[SkippedReason]
    matches: int
    error: str
    error_kind: ToolDiagnosticKind
    details: dict[str, object]


class RejectedVerdict(TypedDict, total=False):
    # Verdict path/line are echoed external JSON values, not trusted identifiers.
    path: object
    line: object
    reason: Required[RejectedReason]
    end_line: int
    max_span: int
    error: str
    error_kind: ToolDiagnosticKind
    details: dict[str, object]


class UnjudgedCandidate(TypedDict):
    candidate_id: int
    path: str
    reason: Literal["missing_verdict"]


class SnippetCut(TypedDict):
    path: str
    matches_total: int
    matches_shown: int
    long_lines_cut: int
    reason: str


class FindCost(TypedDict):
    model_calls: int
    candidates_judged: int


_FIND_STATUS_OK: FindStatus = "ok"
_FIND_STATUS_NO_HIT: FindStatus = "no_hit"
_FIND_STATUS_PARTIAL: FindStatus = "partial"
_FIND_STATUS_JUDGE_UNAVAILABLE: JudgeFailureStatus = "judge_unavailable"
_FIND_STATUS_JUDGE_ERROR: JudgeFailureStatus = "judge_error"


class FindArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(max_length=_MAX_QUERY_CHARS)
    path: str = "."
    mode: Literal["auto", "literal"] = "auto"
    limit: int = Field(default=10, ge=1, le=50)
    include: list[str] | None = None
    exclude: list[str] | None = None
    respect_gitignore: bool = True
    max_candidates: int = Field(default=30, ge=1, le=100)
    model: str | None = None
    max_output_tokens: int = Field(default=2000, ge=1, le=_MAX_OUTPUT_TOKENS)

    _non_empty = field_validator("query", "path", mode="after")(validate_non_empty)
    _glob_patterns = field_validator("include", "exclude", mode="after")(validate_glob_patterns)


@dataclass(frozen=True, slots=True)
class FindLocation:
    path: str
    start_line: int
    end_line: int
    excerpt: str
    rationale: str
    via: Literal["judge", "grep", "grep_fallback"]
    evidence_offset: int
    evidence_limit: int

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "excerpt": self.excerpt,
            "rationale": self.rationale,
            "via": self.via,
            "evidence": {"offset": self.evidence_offset, "limit": self.evidence_limit},
        }


@dataclass(frozen=True, slots=True)
class FindCoverage:
    keywords: tuple[str, ...]
    dropped_terms: tuple[str, ...]
    prescreen: tuple[PrescreenNote, ...]
    screened_files: tuple[str, ...]
    judged_files: tuple[str, ...]
    skipped: tuple[SkippedMatch, ...]
    unjudged: tuple[UnjudgedCandidate, ...]
    snippet_cuts: tuple[SnippetCut, ...]
    rejected: tuple[RejectedVerdict, ...]
    declined: int
    dropped_results: int
    truncated: bool
    truncation_reasons: tuple[str, ...]
    model_calls: int

    def as_payload(self) -> dict[str, object]:
        return {
            "keywords": list(self.keywords),
            "dropped_terms": list(self.dropped_terms),
            "prescreen": list(self.prescreen),
            "screened_files": list(self.screened_files),
            "judged_files": list(self.judged_files),
            "skipped": list(self.skipped),
            "unjudged": list(self.unjudged),
            "snippet_cuts": list(self.snippet_cuts),
            "rejected": list(self.rejected),
            "declined": self.declined,
            "dropped_results": self.dropped_results,
            "truncated": self.truncated,
            "truncation_reasons": list(self.truncation_reasons),
            "model_calls": self.model_calls,
        }


@dataclass(frozen=True, slots=True)
class FindResultBody:
    status: FindStatus
    detail: str
    locations: tuple[FindLocation, ...]
    coverage: FindCoverage
    cost: FindCost

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "detail": self.detail,
            "locations": [location.as_payload() for location in self.locations],
            "coverage": self.coverage.as_payload(),
            "cost": dict(self.cost),
        }
