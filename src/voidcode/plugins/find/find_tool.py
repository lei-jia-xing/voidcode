"""Stable facade for the find plugin.

The implementation is split into cohesive modules: ``models`` (arguments,
bounds, statuses, and typed result bodies), ``prescreen`` (query keywords and
candidate assembly), ``judge`` (schema and bounded prompt framing),
``queries`` (files-plugin grep/read execution), ``evidence`` (verdict
acceptance and read-verified locations), ``rendering`` (detail and body
rendering), and ``tool`` (the ``FindTool`` orchestration).

Modules depend only downward, and this facade is the single import surface for
plugin registration, tests, and historical callers. No orchestration lives
here. Public names are listed in ``__all__``; private names remain importable
because existing suites exercise helpers and constants directly.
Monkeypatching a helper or constant should target its owning module.
"""

from __future__ import annotations

# ruff: noqa: F401 - compatibility re-exports are the point of this facade.
from .evidence import _accept_match, _accept_verdicts, _locations_from_matches
from .judge import (
    _JUDGE_CARD_MARKER,
    _JUDGE_PROMPT_PREFIX,
    _JUDGE_PROMPT_RULES,
    _STRUCTURED_QUERY_SUFFIX,
    FIND_JUDGE_SCHEMA,
    _bounded_rationale,
    _candidate_card,
    _fit_judge_prompt,
    _render_judge_prompt,
    _snippet_cuts,
    _truncated_snippet,
)
from .models import (
    _EVIDENCE_CONTEXT_LINES,
    _FIND_STATUS_JUDGE_ERROR,
    _FIND_STATUS_JUDGE_UNAVAILABLE,
    _FIND_STATUS_NO_HIT,
    _FIND_STATUS_OK,
    _FIND_STATUS_PARTIAL,
    _MAX_EVIDENCE_LINES,
    _MAX_KEYWORDS,
    _MAX_MODEL_QUERY_PROMPT_BYTES,
    _MAX_OUTPUT_TOKENS,
    _MAX_QUERY_CHARS,
    _MAX_RATIONALE_CHARS,
    _MAX_SNIPPET_LINE_CHARS,
    _MAX_SNIPPET_MATCHES_PER_FILE,
    FindArgs,
    FindCost,
    FindCoverage,
    FindLocation,
    FindResultBody,
    FindStatus,
    JudgeFailureStatus,
    PrescreenNote,
    PrescreenSkip,
    RejectedReason,
    RejectedVerdict,
    SkippedMatch,
    SkippedReason,
    SnippetCut,
    UnjudgedCandidate,
)
from .prescreen import _STOPWORDS, _build_candidates, _Candidate, extract_keywords
from .queries import _cancelled, _FindCancelled, _grep_request, _read_segment, _render_excerpt, _run_grep
from .rendering import _auto_detail, _build_body, _literal_detail
from .tool import _PROVIDER_FAILURE_GUIDANCE, FindTool

__all__ = [
    "FIND_JUDGE_SCHEMA",
    "FindArgs",
    "FindCost",
    "FindCoverage",
    "FindLocation",
    "FindResultBody",
    "FindStatus",
    "FindTool",
    "JudgeFailureStatus",
    "PrescreenNote",
    "PrescreenSkip",
    "RejectedReason",
    "RejectedVerdict",
    "SkippedMatch",
    "SkippedReason",
    "SnippetCut",
    "UnjudgedCandidate",
    "extract_keywords",
]
