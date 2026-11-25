"""Behavioral ``find`` tool orchestration.

Pipeline: bounded lexical prescreen (files-plugin grep query) -> one structured
model judgment over prescreened candidates only -> line validation plus
evidence excerpts via the files-plugin read query. Every returned line number
is checked against a real file read; judge-invented lines are rejected visibly.
"""

from __future__ import annotations

from typing import ClassVar

from voidcode.capabilities.files.find_queries import FindGrepMatch
from voidcode.capabilities.files.grep_tool import MAX_MATCHES
from voidcode.capabilities.tool_errors import ToolDiagnosticError, expected_failures
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.provider import ProviderExecutionError
from voidcode.sdk.services import PluginServices, QueryContractError, QueryUnavailable
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

from .evidence import _accept_verdicts, _locations_from_matches
from .judge import FIND_JUDGE_SCHEMA, _fit_judge_prompt, _snippet_cuts
from .models import (
    _FIND_STATUS_JUDGE_ERROR,
    _FIND_STATUS_JUDGE_UNAVAILABLE,
    _FIND_STATUS_NO_HIT,
    _FIND_STATUS_OK,
    _FIND_STATUS_PARTIAL,
    _MAX_MODEL_QUERY_PROMPT_BYTES,
    FindArgs,
    FindLocation,
    FindResultBody,
    FindStatus,
    JudgeFailureStatus,
    PrescreenNote,
    RejectedVerdict,
    SkippedMatch,
    SnippetCut,
    UnjudgedCandidate,
)
from .prescreen import _build_candidates, extract_keywords
from .queries import _cancelled, _FindCancelled, _run_grep
from .rendering import _auto_detail, _build_body, _literal_detail

_PROVIDER_FAILURE_GUIDANCE: dict[str, str] = {
    "cancelled": "The provider cancelled the judge turn; retry the find.",
    "transient_failure": "Retry the find; the judge transport failed transiently.",
    "rate_limit": "Retry the find after the provider rate limit clears.",
    "not_configured": "Configure the selected model/provider and retry the find.",
    "missing_auth": "Provide credentials for the selected provider and retry the find.",
    "invalid_model": "Choose a configured model and retry the find.",
}


class FindTool:
    # NOTE: effects are declared for the whole tool, not per operation. Literal
    # mode is model-free READ; auto mode adds one provider (NETWORK) call. The
    # tool-level union is the current honest approximation. Fine-grained
    # per-operation effects are future SDK design, not implemented here.
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="find",
        icon=Icon(glyph="◎"),
        description=(
            "Locate code by behavior: describe what the code does in natural language and get "
            "file locations with evidence excerpts. Lexical prescreen plus one structured "
            "model judgment; every line is validated against a real file read. Literal mode "
            "searches exact text without any model call. Symbol and semantic navigation are out of scope here. "
            "Respects the search root's .gitignore by default; set respect_gitignore=false to include ignored files."
        ),
        input_schema=FindArgs.model_json_schema(),
        effects=frozenset({ToolEffect.READ, ToolEffect.NETWORK}),
        path_argument_keys=("path",),
    )

    @expected_failures
    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        args = FindArgs.model_validate(dict(call.arguments))
        if _cancelled(context):
            return self._cancelled_result()
        try:
            if args.mode == "literal":
                return self._invoke_literal(args, context=context, services=services)
            return self._invoke_auto(args, context=context, services=services)
        except QueryUnavailable as exc:
            return self._dependency_failure(
                "find_files_unavailable",
                f"find requires the files plugin for workspace grep/read: {exc}",
                "Enable the files plugin and retry the find.",
            )
        except QueryContractError as exc:
            return self._dependency_failure(
                "find_query_contract",
                f"find files query contract failed: {exc}",
                "Fix the files plugin query answer and retry the find.",
            )
        except ValueError as exc:
            if isinstance(exc, ToolDiagnosticError):
                raise
            return self._dependency_failure(
                "find_query_contract",
                f"find files query contract failed: {exc}",
                "Fix the files plugin query answer and retry the find.",
            )
        except _FindCancelled as exc:
            return self._cancelled_result(str(exc))

    def _dependency_failure(self, kind: str, error: str, guidance: str) -> ToolFailure:
        return ToolFailure(
            tool_name=self.definition.name,
            error=error,
            output=TextOutput(error),
            diagnostics=ToolDiagnostics(kind=kind, summary=error, guidance=guidance),
        )

    def _cancelled_result(self, error: str = "find cancelled") -> ToolFailure:
        return ToolFailure(
            tool_name=self.definition.name,
            error=error,
            output=TextOutput(error, bounds=OutputBounds(partial=True)),
            execution=ConfirmedStop(cancellation_signalled=True),
        )

    # --- literal path: grep only, no model calls ---

    def _invoke_literal(self, args: FindArgs, *, context: ToolContext, services: PluginServices) -> ToolResult:
        body = _run_grep(args, pattern=args.query, limit=args.limit, context=0, tool_context=context, services=services, ignore_case=False)
        locations, skipped = _locations_from_matches(args, body.matches, via="grep", context=context, services=services)
        truncated = body.truncated
        reasons = ("grep result truncated; narrow path or pattern",) if truncated else ()
        status = _FIND_STATUS_PARTIAL if truncated else (_FIND_STATUS_OK if locations else _FIND_STATUS_NO_HIT)
        detail = _literal_detail(args, status, locations, skipped=skipped, truncated=truncated)
        prescreen_note: PrescreenNote = {"pattern": args.query, "matches": len(body.matches), "truncated": body.truncated}
        return self._success(
            args,
            status=status,
            detail=detail,
            locations=locations,
            keywords=(),
            dropped=(),
            prescreen=(prescreen_note,),
            screened=tuple(sorted({match.file for match in body.matches})),
            judged=(),
            skipped=skipped,
            unjudged=(),
            snippet_cuts=(),
            rejected=(),
            declined=0,
            dropped_results=max(0, len(body.matches) - len(locations) - len(skipped)),
            truncated=truncated,
            reasons=reasons,
            model_calls=0,
            partial=truncated or bool(skipped),
        )

    # --- auto path: prescreen -> judge -> validate ---

    def _invoke_auto(self, args: FindArgs, *, context: ToolContext, services: PluginServices) -> ToolResult:
        keywords, dropped_terms = extract_keywords(args.query)
        prescreen_notes: list[PrescreenNote] = []
        matches: list[tuple[str, FindGrepMatch]] = []
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
                return self._cancelled_result()

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

        candidates, skipped_cap = _build_candidates(matches, max_candidates=args.max_candidates)
        skipped: list[SkippedMatch] = list(skipped_cap)
        if skipped_cap:
            truncated = True
            truncation_reasons.append(f"candidate cap ({args.max_candidates}) reached; {len(skipped_cap)} file(s) not judged")
        prompt, candidates, prompt_dropped = _fit_judge_prompt(args.query, candidates)
        if prompt_dropped:
            skipped.extend({"path": candidate.path, "reason": "prompt_budget", "matches": len(candidate.match_lines)} for candidate in prompt_dropped)
            truncated = True
            truncation_reasons.append(f"judge prompt cap ({_MAX_MODEL_QUERY_PROMPT_BYTES} bytes) reached; {len(prompt_dropped)} file(s) not judged")
        if not candidates:
            return self._verdict_free_result(
                args,
                status=_FIND_STATUS_JUDGE_UNAVAILABLE,
                detail=f"find judge prompt cannot fit any candidate under {_MAX_MODEL_QUERY_PROMPT_BYTES} bytes",
                keywords=keywords,
                dropped_terms=dropped_terms,
                prescreen_notes=prescreen_notes,
                screened_files=screened_files,
                judged_files=(),
                skipped=skipped,
                snippet_cuts=(),
                truncated=truncated,
                truncation_reasons=truncation_reasons,
                dispatched=False,
            )
        judged_files = tuple(candidate.path for candidate in candidates)
        cuts = _snippet_cuts(candidates)
        try:
            answer = services.model_query_structured(
                prompt,
                result_schema=FIND_JUDGE_SCHEMA,
                model=args.model,
                max_output_tokens=args.max_output_tokens,
                include_context=False,
            )
        except ProviderExecutionError as exc:
            if exc.kind == "cancelled" and _cancelled(context):
                return self._cancelled_result()
            return self._verdict_free_result(
                args,
                status=_FIND_STATUS_JUDGE_UNAVAILABLE,
                detail=f"find judge unavailable ({exc.kind}): {exc}",
                keywords=keywords,
                dropped_terms=dropped_terms,
                prescreen_notes=prescreen_notes,
                screened_files=screened_files,
                judged_files=judged_files,
                skipped=skipped,
                snippet_cuts=cuts,
                truncated=truncated,
                truncation_reasons=truncation_reasons,
                dispatched=True,
                provider_kind=exc.kind,
                guidance=_PROVIDER_FAILURE_GUIDANCE.get(exc.kind),
            )
        except (ValueError, RuntimeError) as exc:
            return self._verdict_free_result(
                args,
                status=_FIND_STATUS_JUDGE_UNAVAILABLE,
                detail=f"find judge unavailable before dispatch: {exc}",
                keywords=keywords,
                dropped_terms=dropped_terms,
                prescreen_notes=prescreen_notes,
                screened_files=screened_files,
                judged_files=judged_files,
                skipped=skipped,
                snippet_cuts=cuts,
                truncated=truncated,
                truncation_reasons=truncation_reasons,
                dispatched=False,
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
                dispatched=True,
            )
        raw_verdicts = answer.data.get("verdicts") if answer.data is not None else None
        # Owned JSON is immutable: sequences arrive as tuples, not lists.
        verdicts = list(raw_verdicts) if isinstance(raw_verdicts, (list, tuple)) else []
        locations, rejected, declined, unjudged = _accept_verdicts(args, candidates, verdicts, context=context, services=services)
        if _cancelled(context):
            return self._cancelled_result()
        ranked = locations[: args.limit]
        dropped_results = len(locations) - len(ranked)
        incomplete = truncated or bool(unjudged)
        if incomplete:
            status = _FIND_STATUS_PARTIAL
        elif ranked:
            status = _FIND_STATUS_OK
        else:
            status = _FIND_STATUS_NO_HIT
        detail = _auto_detail(
            status,
            ranked,
            dropped_results=dropped_results,
            rejected=rejected,
            declined=declined,
            unjudged=unjudged,
            snippet_cuts=cuts,
            dropped_terms=dropped_terms,
            prescreen_notes=prescreen_notes,
            truncated=truncated,
            reasons=truncation_reasons,
        )
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
            unjudged=tuple(unjudged),
            snippet_cuts=cuts,
            rejected=tuple(rejected),
            declined=declined,
            dropped_results=dropped_results,
            truncated=truncated,
            reasons=tuple(truncation_reasons),
            model_calls=1,
            partial=incomplete,
        )

    def _grep_fallback(
        self,
        args: FindArgs,
        *,
        keywords: list[str],
        dropped_terms: list[str],
        prescreen_notes: list[PrescreenNote],
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        """Zero prescreen hits: one literal grep keeps literal queries working without a judge call."""
        body = _run_grep(args, pattern=args.query, limit=args.limit, context=0, tool_context=context, services=services, ignore_case=False)
        locations, skipped = _locations_from_matches(args, body.matches, via="grep_fallback", context=context, services=services)
        truncated = body.truncated
        reasons = ("grep fallback truncated; narrow path or pattern",) if truncated else ()
        if truncated:
            status = _FIND_STATUS_PARTIAL
        elif locations:
            status = _FIND_STATUS_OK
        else:
            status = _FIND_STATUS_NO_HIT
        fallback_note: PrescreenNote = {"fallback": "literal", "matches": len(body.matches), "truncated": truncated}
        prescreen_notes = [*prescreen_notes, fallback_note]
        if status == _FIND_STATUS_NO_HIT:
            failed_terms = [note["keyword"] for note in prescreen_notes if "error" in note and "keyword" in note]
            if body.matches:
                detail = f"find [no_hit]: {len(body.matches)} literal match(es) found but none could be read as evidence"
            elif failed_terms:
                detail = (
                    f"find: no match for {args.query!r} (keyword prescreen failed for {', '.join(map(str, failed_terms))} and literal grep was empty)"
                )
            else:
                detail = f"find: no match for {args.query!r} (keyword prescreen and literal grep both empty)"
        else:
            detail = _literal_detail(args, status, locations, skipped=skipped, truncated=truncated)
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
            unjudged=(),
            snippet_cuts=(),
            rejected=(),
            declined=0,
            dropped_results=max(0, len(body.matches) - len(locations) - len(skipped)),
            truncated=truncated,
            reasons=reasons,
            model_calls=0,
            partial=truncated or bool(skipped),
        )

    # --- results ---

    _JUDGE_FAILURE_GUIDANCE: ClassVar[dict[JudgeFailureStatus, str]] = {
        "judge_unavailable": "Check provider configuration and availability, then retry the find.",
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
        prescreen_notes: list[PrescreenNote],
        screened_files: tuple[str, ...],
        judged_files: tuple[str, ...],
        skipped: list[SkippedMatch],
        snippet_cuts: tuple[SnippetCut, ...],
        truncated: bool,
        truncation_reasons: list[str],
        dispatched: bool,
        provider_kind: str | None = None,
        guidance: str | None = None,
    ) -> ToolFailure[FindResultBody]:
        _ = args
        body = _build_body(
            status=status,
            detail=detail,
            locations=[],
            keywords=tuple(keywords),
            dropped=tuple(dropped_terms),
            prescreen=tuple(prescreen_notes),
            screened=screened_files,
            judged=judged_files if dispatched else (),
            skipped=tuple(skipped),
            unjudged=(),
            snippet_cuts=snippet_cuts,
            rejected=(),
            declined=0,
            dropped_results=0,
            truncated=truncated,
            reasons=tuple(truncation_reasons),
            model_calls=1 if dispatched else 0,
        )
        diagnostics_details: dict[str, object] = {}
        if provider_kind is not None:
            diagnostics_details["provider_error_kind"] = provider_kind
        return ToolFailure(
            tool_name=self.definition.name,
            error=detail,
            output=TextOutput(detail, bounds=OutputBounds(truncated=truncated, partial=True)),
            body=body,
            diagnostics=ToolDiagnostics(
                kind=f"find_{status}",
                summary=detail,
                details=diagnostics_details,
                guidance=guidance or self._JUDGE_FAILURE_GUIDANCE[status],
            ),
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
        partial: bool,
    ) -> ToolSuccess[FindResultBody]:
        _ = args
        body = _build_body(
            status=status,
            detail=detail,
            locations=locations,
            keywords=keywords,
            dropped=dropped,
            prescreen=prescreen,
            screened=screened,
            judged=judged,
            skipped=skipped,
            unjudged=unjudged,
            snippet_cuts=snippet_cuts,
            rejected=rejected,
            declined=declined,
            dropped_results=dropped_results,
            truncated=truncated,
            reasons=reasons,
            model_calls=model_calls,
        )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(detail, bounds=OutputBounds(truncated=truncated, partial=partial)),
            body=body,
        )
