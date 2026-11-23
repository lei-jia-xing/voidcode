"""Behavioral find: natural-language behavior descriptions to verified file locations.

Pipeline: bounded lexical prescreen (shared grep tool) -> one structured
model judgment over prescreened candidates only -> line validation plus
evidence excerpts via the shared read tool. Every returned line number is
checked against a real file read; judge-invented lines are rejected visibly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidcode.capabilities.files._common import validate_glob_patterns, validate_non_empty
from voidcode.capabilities.files.grep_tool import MAX_MATCHES, GrepResultBody, GrepResultMatch, GrepTool
from voidcode.capabilities.files.read_tool import ReadResultBody, ReadTool
from voidcode.capabilities.tool_errors import ToolDiagnosticError, expected_failures
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    ConfirmedStop,
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolDiagnostics,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

#: Bounds the per-keyword grep fan-out of one find invocation; extra query
#: terms are dropped and reported in coverage.
_MAX_KEYWORDS = 8
#: Bounds the judge prompt: match lines shown per candidate file.
_MAX_SNIPPET_MATCHES_PER_FILE = 12
#: Bounds the judge prompt: snippet line width.
_MAX_SNIPPET_LINE_CHARS = 500
#: Evidence excerpt window around an accepted line range.
_EVIDENCE_CONTEXT_LINES = 2

FindStatus = Literal["ok", "no_hit", "partial", "timeout", "judge_unavailable", "judge_error"]
JudgeFailureStatus = Literal["timeout", "judge_unavailable", "judge_error"]

_FIND_STATUS_OK: FindStatus = "ok"
_FIND_STATUS_NO_HIT: FindStatus = "no_hit"
_FIND_STATUS_PARTIAL: FindStatus = "partial"
_FIND_STATUS_TIMEOUT: JudgeFailureStatus = "timeout"
_FIND_STATUS_JUDGE_UNAVAILABLE: JudgeFailureStatus = "judge_unavailable"
_FIND_STATUS_JUDGE_ERROR: JudgeFailureStatus = "judge_error"


class _FindCancelled(Exception):
    """Abort landed inside a nested grep/read call; the outer invoke reports it."""


_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "of",
        "on",
        "or",
        "show",
        "than",
        "that",
        "the",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
        "you",
        "find",
        "all",
    }
)

#: Strict judge contract: only prescreened candidates, path/line copied from
#: the candidate card, one rationale per verdict.
FIND_JUDGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer", "minimum": 0},
                    "path": {"type": "string", "minLength": 1},
                    "verdict": {"type": "string", "enum": ["match", "no_match"]},
                    "line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "rationale": {"type": "string", "minLength": 1},
                },
                "required": ["candidate_id", "path", "verdict", "line", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


class FindArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str
    path: str = "."
    mode: Literal["auto", "literal"] = "auto"
    limit: int = Field(default=10, ge=1, le=50)
    include: list[str] | None = None
    exclude: list[str] | None = None
    max_candidates: int = Field(default=30, ge=1, le=100)
    model: str | None = None
    max_output_tokens: int = Field(default=2000, ge=1)

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
    prescreen: tuple[dict[str, object], ...]
    screened_files: tuple[str, ...]
    judged_files: tuple[str, ...]
    skipped: tuple[dict[str, object], ...]
    snippet_cuts: tuple[dict[str, object], ...]
    rejected: tuple[dict[str, object], ...]
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
    cost: dict[str, object]

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "detail": self.detail,
            "locations": [location.as_payload() for location in self.locations],
            "coverage": self.coverage.as_payload(),
            "cost": dict(self.cost),
        }


@dataclass
class _Candidate:
    candidate_id: int
    path: str
    match_lines: list[int]
    snippets: list[tuple[int, str]]


def extract_keywords(query: str) -> tuple[list[str], list[str]]:
    """Split a behavior description into lexical prescreen terms plus dropped terms."""
    seen: set[str] = set()
    kept: list[str] = []
    dropped: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_]+", query.lower()):
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        if len(kept) < _MAX_KEYWORDS:
            kept.append(token)
        else:
            dropped.append(token)
    return kept, dropped


def _grep_arguments(args: FindArgs, *, pattern: str, limit: int, context: int, ignore_case: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "pattern": pattern,
        "path": args.path,
        "ignore_case": ignore_case,
        "context": context,
        "limit": limit,
    }
    if args.include is not None:
        payload["include"] = list(args.include)
    if args.exclude is not None:
        payload["exclude"] = list(args.exclude)
    return payload


# NOTE (nested-composition fence): find invokes the shared grep/read tools
# directly instead of routing through the tool pipeline, so nested calls get
# no separate ledger/audit entry. That is defensible ONLY while both nested
# tools are READ-effect (approval gates mutating work only; lease auth still
# applies through the passed context/services — a tripwire test pins their
# effects) AND their intermediate pages reach no session model: grep matches
# feed a tool-free side-turn judge prompt plus local validation, and read
# pages become excerpts stored in the typed body, never in output.text — so
# attaching zero ReadObservation coverage is correct. Revisit the day excerpts
# enter output.text or either nested tool gains a non-read effect.


def _raise_nested_failure(result: ToolFailure, *, fallback_message: str) -> NoReturn:
    """Re-raise a nested file-tool failure with its diagnostic payload intact.

    The find boundary converts this to a ``find`` result, so the nested result
    is never returned under the wrong tool name.
    """
    diagnostics = result.diagnostics
    message = result.error or fallback_message
    if diagnostics is not None and diagnostics.kind is not None:
        raise ToolDiagnosticError(
            message=message,
            error_kind=diagnostics.kind,
            error_details=dict(diagnostics.details),
            retry_guidance=diagnostics.guidance,
        )
    raise ToolDiagnosticError(
        message=message,
        error_kind="tool_execution_error",
        error_details={"reason": "nested_tool_failure", "tool": result.tool_name},
        retry_guidance=None,
    )


def _run_grep(
    args: FindArgs,
    *,
    pattern: str,
    limit: int,
    context: int,
    tool_context: ToolContext,
    services: PluginServices,
) -> GrepResultBody:
    """Invoke the shared grep tool exactly as Core would; nested diagnostics propagate."""
    result = GrepTool().invoke(
        ToolCall(tool_name="grep", arguments=_grep_arguments(args, pattern=pattern, limit=limit, context=context)),
        context=tool_context,
        services=services,
    )
    if isinstance(result, ToolFailure):
        if _cancelled(tool_context):
            raise _FindCancelled(result.error or "grep search cancelled")
        _raise_nested_failure(result, fallback_message="grep search failed")
    body = result.body
    if not isinstance(body, GrepResultBody):
        raise ValueError("grep search returned no result body")
    return body


def _read_segment(path: str, *, offset: int, limit: int, tool_context: ToolContext, services: PluginServices) -> ReadResultBody:
    """Read an evidence segment through the shared read tool; nested diagnostics propagate."""
    result = ReadTool().invoke(
        ToolCall(tool_name="read", arguments={"path": path, "offset": offset, "limit": limit}),
        context=tool_context,
        services=services,
    )
    if isinstance(result, ToolFailure):
        if _cancelled(tool_context):
            raise _FindCancelled(result.error or "read cancelled")
        _raise_nested_failure(result, fallback_message=f"read failed for {path}")
    if not isinstance(result.body, ReadResultBody):
        raise ValueError(f"read returned no file page for {path}")
    return result.body


def _render_excerpt(body: ReadResultBody) -> str:
    return "\n".join(f"{record.line}: {record.text}" for record in body.lines)


def _truncated_snippet(text: str) -> str:
    if len(text) <= _MAX_SNIPPET_LINE_CHARS:
        return text
    return text[:_MAX_SNIPPET_LINE_CHARS] + "…"


def _snippet_cuts(candidates: list[_Candidate]) -> tuple[dict[str, object], ...]:
    """Which judged files had judge-prompt snippets cut, and why. Empty when caps never bite."""
    cuts: list[dict[str, object]] = []
    for candidate in candidates:
        total = len(candidate.match_lines)
        shown = len(candidate.snippets)
        long_lines = sum(1 for _, text in candidate.snippets if len(text) > _MAX_SNIPPET_LINE_CHARS)
        if total <= shown and long_lines == 0:
            continue
        cuts.append(
            {
                "path": candidate.path,
                "matches_total": total,
                "matches_shown": shown,
                "long_lines_cut": long_lines,
                "reason": f"judge prompt snippet caps: {_MAX_SNIPPET_MATCHES_PER_FILE} matches/file, {_MAX_SNIPPET_LINE_CHARS} chars/line",
            }
        )
    return tuple(cuts)


def _cancelled(tool_context: ToolContext) -> bool:
    return tool_context.abort_signal is not None and tool_context.abort_signal.cancelled


class FindTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="find",
        icon=Icon(glyph="◎"),
        description=(
            "Locate code by behavior: describe what the code does in natural language and get "
            "ranked file locations with evidence excerpts. Lexical prescreen plus one structured "
            "model judgment; every line is validated against a real file read. Literal mode "
            "searches exact text without any model call. Symbol and semantic navigation are out of scope here."
        ),
        input_schema=FindArgs.model_json_schema(),
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
    )

    @expected_failures
    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        args = FindArgs.model_validate(dict(call.arguments))
        if _cancelled(context):
            return ToolFailure(
                tool_name=self.definition.name,
                error="find cancelled",
                output=TextOutput("find cancelled", bounds=OutputBounds(partial=True)),
                execution=ConfirmedStop(cancellation_signalled=True),
            )
        try:
            if args.mode == "literal":
                return self._invoke_literal(args, context=context, services=services)
            return self._invoke_auto(args, context=context, services=services)
        except _FindCancelled as exc:
            return ToolFailure(
                tool_name=self.definition.name,
                error=str(exc),
                output=TextOutput(str(exc), bounds=OutputBounds(partial=True)),
                execution=ConfirmedStop(cancellation_signalled=True),
            )

    # --- literal path: grep only, no model calls ---

    def _invoke_literal(self, args: FindArgs, *, context: ToolContext, services: PluginServices) -> ToolResult:
        body = _run_grep(args, pattern=args.query, limit=args.limit, context=0, tool_context=context, services=services)
        locations, skipped = self._locations_from_matches(args, body.matches, via="grep", context=context, services=services)
        truncated = body.truncated
        reasons = ("grep result truncated; narrow path or pattern",) if truncated else ()
        status = _FIND_STATUS_PARTIAL if truncated else (_FIND_STATUS_OK if locations else _FIND_STATUS_NO_HIT)
        detail = self._literal_detail(args, status, locations, skipped=skipped, truncated=truncated)
        return self._success(
            args,
            status=status,
            detail=detail,
            locations=locations,
            keywords=(),
            dropped=(),
            prescreen=({"pattern": args.query, "matches": len(body.matches), "truncated": body.truncated},),
            screened=tuple(sorted({match.file for match in body.matches})),
            judged=(),
            skipped=skipped,
            snippet_cuts=(),
            rejected=(),
            declined=0,
            dropped_results=max(0, len(body.matches) - len(locations) - len(skipped)),
            truncated=truncated,
            reasons=reasons,
            model_calls=0,
        )

    # --- auto path: prescreen -> judge -> validate ---

    def _invoke_auto(self, args: FindArgs, *, context: ToolContext, services: PluginServices) -> ToolResult:
        keywords, dropped_terms = extract_keywords(args.query)
        prescreen_notes: list[dict[str, object]] = []
        matches: list[tuple[str, GrepResultMatch]] = []
        truncated = False
        truncation_reasons: list[str] = []
        for keyword in keywords:
            remaining = MAX_MATCHES - len(matches)
            if remaining <= 0:
                truncated = True
                truncation_reasons.append(f"prescreen match cap ({MAX_MATCHES}) reached; later keywords skipped")
                prescreen_notes.append({"keyword": keyword, "skipped": "prescreen match cap reached"})
                continue
            try:
                body = _run_grep(args, pattern=keyword, limit=remaining, context=1, tool_context=context, services=services)
            except ToolDiagnosticError as exc:
                prescreen_notes.append({"keyword": keyword, "error": exc.message})
                continue
            if body.truncated:
                truncated = True
                truncation_reasons.append(f"grep truncated for keyword {keyword!r}; narrow path or pattern")
            prescreen_notes.append(
                {
                    "keyword": keyword,
                    "matches": len(body.matches),
                    "files": len({match.file for match in body.matches}),
                    "truncated": body.truncated,
                }
            )
            matches.extend((keyword, match) for match in body.matches)
            if _cancelled(context):
                return ToolFailure(
                    tool_name=self.definition.name,
                    error="find cancelled",
                    output=TextOutput("find cancelled", bounds=OutputBounds(partial=True)),
                    execution=ConfirmedStop(cancellation_signalled=True),
                )

        screened_files = tuple(sorted({match.file for _, match in matches}))
        if not matches:
            return self._grep_fallback(
                args,
                keywords=keywords,
                dropped_terms=dropped_terms,
                prescreen_notes=prescreen_notes,
                context=context,
                services=services,
            )

        candidates, skipped_cap = self._build_candidates(matches, max_candidates=args.max_candidates)
        skipped: list[dict[str, object]] = list(skipped_cap)
        if skipped_cap:
            truncated = True
            truncation_reasons.append(f"candidate cap ({args.max_candidates}) reached; {len(skipped_cap)} file(s) not judged")
        judged_files = tuple(candidate.path for candidate in candidates)
        cuts = _snippet_cuts(candidates)
        prompt = self._judge_prompt(args.query, candidates)
        try:
            answer = services.model_query_structured(
                prompt,
                result_schema=FIND_JUDGE_SCHEMA,
                model=args.model,
                max_output_tokens=args.max_output_tokens,
                include_context=False,
            )
        except TimeoutError as exc:
            return self._verdict_free_result(
                args,
                status=_FIND_STATUS_TIMEOUT,
                detail=f"find timed out waiting for the judge: {exc}",
                keywords=keywords,
                dropped_terms=dropped_terms,
                prescreen_notes=prescreen_notes,
                screened_files=screened_files,
                judged_files=judged_files,
                skipped=skipped,
                snippet_cuts=cuts,
                truncated=truncated,
                truncation_reasons=truncation_reasons,
            )
        except Exception as exc:  # noqa: BLE001 - judge transport failures are one status, never data
            return self._verdict_free_result(
                args,
                status=_FIND_STATUS_JUDGE_UNAVAILABLE,
                detail=f"find judge unavailable: {exc}",
                keywords=keywords,
                dropped_terms=dropped_terms,
                prescreen_notes=prescreen_notes,
                screened_files=screened_files,
                judged_files=judged_files,
                skipped=skipped,
                snippet_cuts=cuts,
                truncated=truncated,
                truncation_reasons=truncation_reasons,
            )
        if answer.parse_error is not None:
            return self._verdict_free_result(
                args,
                status=_FIND_STATUS_JUDGE_ERROR,
                detail=f"find judge reply unusable ({answer.parse_error.kind}): {answer.parse_error.message}",
                keywords=keywords,
                dropped_terms=dropped_terms,
                prescreen_notes=prescreen_notes,
                screened_files=screened_files,
                judged_files=judged_files,
                skipped=skipped,
                snippet_cuts=cuts,
                truncated=truncated,
                truncation_reasons=truncation_reasons,
            )
        raw_verdicts = answer.data.get("verdicts") if answer.data is not None else None
        # Owned JSON is immutable: sequences arrive as tuples, not lists.
        verdicts = list(raw_verdicts) if isinstance(raw_verdicts, (list, tuple)) else []
        locations, rejected, declined = self._accept_verdicts(args, candidates, verdicts, context=context, services=services)
        if _cancelled(context):
            return ToolFailure(
                tool_name=self.definition.name,
                error="find cancelled",
                output=TextOutput("find cancelled", bounds=OutputBounds(partial=True)),
                execution=ConfirmedStop(cancellation_signalled=True),
            )
        ranked = locations[: args.limit]
        dropped_results = len(locations) - len(ranked)
        if truncated:
            status = _FIND_STATUS_PARTIAL
        elif ranked:
            status = _FIND_STATUS_OK
        else:
            status = _FIND_STATUS_NO_HIT
        detail = self._auto_detail(args, status, ranked, rejected=rejected, declined=declined, truncated=truncated, reasons=truncation_reasons)
        return self._success(
            args,
            status=status,
            detail=detail,
            locations=ranked,
            keywords=tuple(keywords),
            dropped=tuple(dropped_terms),
            prescreen=tuple(prescreen_notes),
            screened=screened_files,
            judged=judged_files,
            skipped=tuple(skipped),
            snippet_cuts=cuts,
            rejected=tuple(rejected),
            declined=declined,
            dropped_results=dropped_results,
            truncated=truncated,
            reasons=tuple(truncation_reasons),
            model_calls=1,
        )

    def _grep_fallback(
        self,
        args: FindArgs,
        *,
        keywords: list[str],
        dropped_terms: list[str],
        prescreen_notes: list[dict[str, object]],
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        """Zero prescreen hits: one literal grep keeps literal queries working without a judge call."""
        body = _run_grep(args, pattern=args.query, limit=args.limit, context=0, tool_context=context, services=services)
        locations, skipped = self._locations_from_matches(args, body.matches, via="grep_fallback", context=context, services=services)
        truncated = body.truncated
        reasons = ("grep fallback truncated; narrow path or pattern",) if truncated else ()
        if truncated:
            status = _FIND_STATUS_PARTIAL
        elif locations:
            status = _FIND_STATUS_OK
        else:
            status = _FIND_STATUS_NO_HIT
        prescreen_notes = [*prescreen_notes, {"fallback": "literal", "matches": len(body.matches), "truncated": truncated}]
        if status == _FIND_STATUS_NO_HIT:
            detail = f"find: no match for {args.query!r} (keyword prescreen and literal grep both empty)"
        else:
            detail = self._literal_detail(args, status, locations, skipped=skipped, truncated=truncated)
        return self._success(
            args,
            status=status,
            detail=detail,
            locations=locations,
            keywords=tuple(keywords),
            dropped=tuple(dropped_terms),
            prescreen=tuple(prescreen_notes),
            screened=tuple(sorted({match.file for match in body.matches})),
            judged=(),
            skipped=skipped,
            snippet_cuts=(),
            rejected=(),
            declined=0,
            dropped_results=max(0, len(body.matches) - len(locations) - len(skipped)),
            truncated=truncated,
            reasons=reasons,
            model_calls=0,
        )

    # --- candidates and verdicts ---

    @staticmethod
    def _build_candidates(matches: list[tuple[str, GrepResultMatch]], *, max_candidates: int) -> tuple[list[_Candidate], list[dict[str, object]]]:
        by_file: dict[str, _Candidate] = {}
        order: list[str] = []
        for _, match in matches:
            candidate = by_file.get(match.file)
            if candidate is None:
                candidate = _Candidate(candidate_id=len(order), path=match.file, match_lines=[], snippets=[])
                by_file[match.file] = candidate
                order.append(match.file)
            if match.line not in candidate.match_lines:
                candidate.match_lines.append(match.line)
                if len(candidate.snippets) < _MAX_SNIPPET_MATCHES_PER_FILE:
                    candidate.snippets.append((match.line, match.text))
        candidates = [by_file[path] for path in order]
        for candidate in candidates:
            candidate.match_lines.sort()
            candidate.snippets.sort(key=lambda item: item[0])
        skipped: list[dict[str, object]] = [
            {"path": candidate.path, "reason": "candidate_cap", "matches": len(candidate.match_lines)} for candidate in candidates[max_candidates:]
        ]
        return candidates[:max_candidates], skipped

    @staticmethod
    def _judge_prompt(query: str, candidates: list[_Candidate]) -> str:
        cards: list[str] = []
        for candidate in candidates:
            snippet = "\n".join(f"    {number}: {_truncated_snippet(text)}" for number, text in candidate.snippets)
            cards.append(
                f"candidate {candidate.candidate_id}: {candidate.path}\n"
                f"  prescreened match lines: {', '.join(map(str, candidate.match_lines))}\n"
                f"{snippet}"
            )
        return (
            "Which of these prescreened code locations implement the described behavior?\n"
            f"Behavior: {query}\n\n"
            + "\n\n".join(cards)
            + "\n\nRules: judge ONLY the candidates above. For candidate_id/path copy the card exactly; "
            "for a match, line must be one of that candidate's prescreened match lines and end_line "
            "(when given) must be at or after line; for no_match copy the candidate's first match "
            "line. One verdict per candidate, with a one-sentence rationale."
        )

    def _accept_verdicts(
        self,
        args: FindArgs,
        candidates: list[_Candidate],
        verdicts: list[object],
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> tuple[list[FindLocation], list[dict[str, object]], int]:
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        seen: set[int] = set()
        locations: list[FindLocation] = []
        rejected: list[dict[str, object]] = []
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
            location_or_rejection = self._accept_match(args, candidate, verdict, context=context, services=services)
            if isinstance(location_or_rejection, FindLocation):
                locations.append(location_or_rejection)
            else:
                rejected.append(location_or_rejection)
        return locations, rejected, declined

    @staticmethod
    def _accept_match(
        args: FindArgs,
        candidate: _Candidate,
        verdict: Mapping[str, object],
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> FindLocation | dict[str, object]:
        _ = args
        line = verdict.get("line")
        if verdict.get("path") != candidate.path:
            return {"path": candidate.path, "line": line, "reason": "path_mismatch"}
        if not isinstance(line, int) or isinstance(line, bool) or line not in candidate.match_lines:
            return {"path": candidate.path, "line": line, "reason": "line_not_prescreened"}
        raw_end = verdict.get("end_line", line)
        end_line = raw_end if isinstance(raw_end, int) and not isinstance(raw_end, bool) else -1
        if end_line < line:
            return {"path": candidate.path, "line": line, "reason": "invalid_range"}
        rationale = verdict.get("rationale")
        if not isinstance(rationale, str) or not rationale:
            return {"path": candidate.path, "line": line, "reason": "invalid_verdict_field"}
        offset = max(1, line - _EVIDENCE_CONTEXT_LINES)
        limit = (end_line - line + 1) + 2 * _EVIDENCE_CONTEXT_LINES
        try:
            page = _read_segment(candidate.path, offset=offset, limit=limit, tool_context=context, services=services)
        except ToolDiagnosticError as exc:
            return {
                "path": candidate.path,
                "line": line,
                "reason": "line_out_of_range",
                "error": exc.message,
                "error_kind": exc.error_kind,
                "details": dict(exc.error_details),
            }
        shown_lines = {record.line for record in page.lines}
        if line not in shown_lines or end_line not in shown_lines:
            return {"path": candidate.path, "line": line, "reason": "line_out_of_range"}
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
        self,
        args: FindArgs,
        matches: tuple[GrepResultMatch, ...] | list[GrepResultMatch],
        *,
        via: Literal["grep", "grep_fallback"],
        context: ToolContext,
        services: PluginServices,
    ) -> tuple[list[FindLocation], tuple[dict[str, object], ...]]:
        _ = args
        locations: list[FindLocation] = []
        skipped: list[dict[str, object]] = []
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

    # --- results ---

    _JUDGE_FAILURE_GUIDANCE: ClassVar[dict[JudgeFailureStatus, str]] = {
        "timeout": "Retry the find; nothing was judged.",
        "judge_unavailable": "Check provider availability and retry the find.",
        "judge_error": "Retry the find; the unusable judge reply was discarded.",
    }

    def _verdict_free_result(
        self,
        args: FindArgs,
        *,
        status: JudgeFailureStatus,
        detail: str,
        keywords: list[str],
        dropped_terms: list[str],
        prescreen_notes: list[dict[str, object]],
        screened_files: tuple[str, ...],
        judged_files: tuple[str, ...],
        skipped: list[dict[str, object]],
        snippet_cuts: tuple[dict[str, object], ...],
        truncated: bool,
        truncation_reasons: list[str],
    ) -> ToolFailure[FindResultBody]:
        _ = args
        body = self._build_body(
            status=status,
            detail=detail,
            locations=[],
            keywords=tuple(keywords),
            dropped=tuple(dropped_terms),
            prescreen=tuple(prescreen_notes),
            screened=screened_files,
            judged=judged_files,
            skipped=tuple(skipped),
            snippet_cuts=snippet_cuts,
            rejected=(),
            declined=0,
            dropped_results=0,
            truncated=truncated,
            reasons=tuple(truncation_reasons),
            model_calls=1,
        )
        return ToolFailure(
            tool_name=self.definition.name,
            error=detail,
            output=TextOutput(detail, bounds=OutputBounds(truncated=truncated, partial=True)),
            body=body,
            diagnostics=ToolDiagnostics(
                kind=f"find_{status}",
                summary=detail,
                guidance=self._JUDGE_FAILURE_GUIDANCE[status],
            ),
        )

    @staticmethod
    def _literal_detail(
        args: FindArgs,
        status: FindStatus,
        locations: list[FindLocation],
        *,
        skipped: tuple[dict[str, object], ...],
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

    @staticmethod
    def _auto_detail(
        args: FindArgs,
        status: FindStatus,
        locations: list[FindLocation],
        *,
        rejected: list[dict[str, object]],
        declined: int,
        truncated: bool,
        reasons: list[str],
    ) -> str:
        _ = args
        lines = [f"find [{status}]: {len(locations)} verified location(s)"]
        for location in locations:
            lines.append(f"{location.path}:{location.start_line}-{location.end_line}: {location.rationale}")
        if declined:
            lines.append(f"{declined} candidate(s) declined by the judge")
        for rejection in rejected:
            lines.append(f"rejected {rejection.get('path')}:{rejection.get('line')} ({rejection.get('reason')})")
        if truncated:
            lines.extend(f"screening truncated: {reason}" for reason in reasons)
        lines.append("model calls: 1")
        return "\n".join(lines)

    @staticmethod
    def _build_body(
        *,
        status: FindStatus,
        detail: str,
        locations: list[FindLocation],
        keywords: tuple[str, ...],
        dropped: tuple[str, ...],
        prescreen: tuple[dict[str, object], ...],
        screened: tuple[str, ...],
        judged: tuple[str, ...],
        skipped: tuple[dict[str, object], ...],
        snippet_cuts: tuple[dict[str, object], ...],
        rejected: tuple[dict[str, object], ...],
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

    def _success(
        self,
        args: FindArgs,
        *,
        status: FindStatus,
        detail: str,
        locations: list[FindLocation],
        keywords: tuple[str, ...],
        dropped: tuple[str, ...],
        prescreen: tuple[dict[str, object], ...],
        screened: tuple[str, ...],
        judged: tuple[str, ...],
        skipped: tuple[dict[str, object], ...],
        snippet_cuts: tuple[dict[str, object], ...],
        rejected: tuple[dict[str, object], ...],
        declined: int,
        dropped_results: int,
        truncated: bool,
        reasons: tuple[str, ...],
        model_calls: int,
    ) -> ToolSuccess[FindResultBody]:
        _ = args
        body = self._build_body(
            status=status,
            detail=detail,
            locations=locations,
            keywords=keywords,
            dropped=dropped,
            prescreen=prescreen,
            screened=screened,
            judged=judged,
            skipped=skipped,
            snippet_cuts=snippet_cuts,
            rejected=rejected,
            declined=declined,
            dropped_results=dropped_results,
            truncated=truncated,
            reasons=reasons,
            model_calls=model_calls,
        )
        partial = status != _FIND_STATUS_OK
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(detail, bounds=OutputBounds(truncated=truncated, partial=partial)),
            body=body,
        )
