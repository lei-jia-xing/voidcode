# find

Locate code by behavior: describe what the code does in natural language and
get file locations with evidence excerpts and why-matched rationales.

## Surface

- Tool `find` (`READ`, `NETWORK`; never replayed): effects are declared for the
  whole tool, not per operation. Literal mode is model-free `READ`; auto mode
  also makes one provider judge turn, so the tool declares the union. Future
  per-operation effects are SDK design, not implemented here.
- Input is a behavior description (`query`, at most 1000 characters) plus
  optional path scope (`path`, `include`/`exclude` globs), `limit` (max
  locations, default 10, max 50), `max_candidates` (files sent to the judge,
  default 30, max 100), and the per-call judge budget (`model`,
  `max_output_tokens`, default 2000, max 8000).
- Output is uniform: `status`, `locations` (each with `path`, `start_line` /
  `end_line`, `excerpt`, `rationale`, `via`, and re-readable `evidence`
  offset/limit), `coverage` (keywords screened, files with matches, judged and
  skipped candidates with reasons, `unjudged`, rejected verdicts, snippet cuts,
  truncation flags), and `cost` (`model_calls`, `candidates_judged`).
- Locations follow the judge's verdict order; this tool does no relevance
  scoring or ranking of its own.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See
[configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Auto mode runs a bounded lexical prescreen first (one shared-`grep` call per
  query keyword, total matches capped at grep's own `MAX_MATCHES`), then
  exactly one structured judge call (`model_query_structured` with a strict
  `verdicts` schema) over candidate cards. The judge prompt is capped below the
  core's 64 KiB model-query limit, including the structured suffix. Candidate
  cards and snippets are JSON-encoded as untrusted data; candidates that do not
  fit are skipped with a visible `prompt_budget` reason and `partial` status.
- The judge sees at most 12 snippet lines per file and 500 characters per line;
  only those shown lines are selectable. Cuts are reported in
  `coverage.snippet_cuts`, and a verdict for a prescreened-but-unshown line is
  rejected as `line_not_shown`.
- Every returned line is validated against a real file read through the shared
  `read` tool: the verdict path must equal the candidate path, the line must be
  a shown match line, the rationale must be non-empty text (bounded to 500
  characters), an accepted end line must fit one read page, and the excerpt is
  the read page itself. Violations are rejected visibly in `coverage.rejected`
  (`path_mismatch`, `line_not_prescreened`, `line_not_shown`, `invalid_range`,
  `range_too_large`, `evidence_page_clipped`, `line_out_of_range`,
  `evidence_unreadable`, `unknown_candidate`, `duplicate_verdict`,
  `invalid_verdict_field`) — never silently fixed.
- Missing verdicts are coverage failures, not completeness: candidates with no
  verdict appear in `coverage.unjudged` (`missing_verdict`) and force `partial`.
  `ok` means every judged candidate produced a verdict and screening was not
  truncated; it never ships empty.
- `no_hit` is a complete negative result (judge declined everything, or the
  keyword prescreen plus literal fallback were empty) and is **not** marked
  partial. `partial` covers prescreen/candidate/prompt caps, grep truncation,
  and unjudged candidates; verified hits included in a partial result are still
  validated.
- `judge_unavailable` covers provider/config/transport failures, including
  cancelled turns that were not caused by the caller; its diagnostics carry the
  provider error kind when one is available. `judge_error` covers an unparsable
  or schema-invalid reply. Both arrive as typed tool failures with the same
  `FindResultBody` plus diagnostics — never success-shaped. A caller cancellation
  is reported as a confirmed stop, not as a provider status.
- Literal queries keep working without any model call: `mode: "literal"` is
  grep-only, and auto mode falls back to one literal grep when the keyword
  prescreen finds nothing (`via: "grep_fallback"`, `model_calls: 0`). Literal and
  fallback matching is case-sensitive; keyword prescreening is case-insensitive.
  If fallback grep matched but evidence could not be read, the detail says so
  rather than claiming both searches were empty.
- `.gitignore` is not honored: the shared grep default is `respect_gitignore:
  false`, and find exposes no override. Standard ignored directories (`.git`,
  `node_modules`, `__pycache__`, build outputs, …) are still skipped. `read`,
  `grep`, and therefore `find` keep external read access under the files
  capability's documented path policy.
- No vector DB, no daemon index, no background threads; model access goes only
  through the existing `model_query_structured` plumbing with an explicit
  per-call output cap. Judge spend is recorded as `core.model_query`; it is not
  part of the session usage/budget report. Symbol and semantic navigation
  (definitions, references, rename scope) are out of scope here.
