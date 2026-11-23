# find

Locate code by behavior: describe what the code does in natural language and
get ranked file locations with evidence excerpts and why-matched rationales.

## Surface

- Tool `find` (`READ`): input is a behavior description plus optional path
  scope (`path`, `include`/`exclude` globs), `limit` (max locations, default
  10), `max_candidates` (files sent to the judge, default 30), and the
  per-call model budget (`model`, `max_output_tokens`, default 2000).
- Output is uniform: `status`, `locations` (each with `path`, `start_line` /
  `end_line`, `excerpt`, `rationale`, `via`, and re-readable `evidence`
  offset/limit), `coverage` (keywords screened, files screened/judged/skipped
  with reasons, truncation flags), and `cost` (`model_calls` made).

## Behavior and limits

- Auto mode runs a bounded lexical prescreen first (one shared-`grep` call per
  query keyword, total matches capped at grep's own `MAX_MATCHES`), then
  exactly one structured judge call (`model_query_structured` with a strict
  `verdicts` schema: `candidate_id`/`path`/`verdict`/`line`/`rationale`
  fields) over prescreened candidates only.
- Every returned line is validated against a real file read through the shared
  `read` tool: the verdict path must equal the candidate path, the line must
  be a prescreened match line inside the file's real line count, the rationale
  must be non-empty text, and the excerpt is the read page itself. Violations
  are rejected visibly in `coverage.rejected` (`path_mismatch`,
  `line_not_prescreened`, `invalid_range`, `line_out_of_range`,
  `unknown_candidate`, `duplicate_verdict`, `invalid_verdict_field`) — never
  silently fixed.
- Statuses are strictly distinct and `ok` never ships empty: `ok` (verified
  hits, complete screening), `no_hit` (judge declined everything, or keyword
  prescreen plus literal grep both empty), and `partial` (any truncation:
  prescreen caps, grep truncation, or candidate-cap skips — hits included are
  still verified) arrive as tool success; `timeout` (judge call timed out),
  `judge_unavailable` (judge transport/budget/provider failure), and
  `judge_error` (judge reply unparsable or schema-invalid) arrive as typed
  tool failures with the same `FindResultBody` plus diagnostics — never
  success-shaped. A grep-level refusal (e.g. an over-long line aborting the
  fallback search) propagates as grep's own typed diagnostic, never as an
  empty `no_hit`.
- Literal queries keep working without any model call: `mode: "literal"` is
  grep-only, and auto mode falls back to one literal grep when the keyword
  prescreen finds nothing (`via: "grep_fallback"`, `model_calls: 0`).
- No vector DB, no daemon index, no background threads; model access goes only
  through the existing `model_query_structured` plumbing with an explicit
  per-call budget. Symbol and semantic navigation (definitions, references,
  rename scope) are out of scope here.
