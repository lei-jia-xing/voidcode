# compaction

Summarize the covered conversation prefix into one model-generated system message, on demand (`/compact`) or automatically past a token gate.

## Surface

- Command `/compact` (effects `SESSION`, `busy_gate=True`, no arguments): projects the covered prefix on demand and reports the core's measured accounting (`covered_from_sequence`, `covered_to_sequence`, covered/dropped bytes, summary kind; the data also carries the covered event count for a fresh projection). A failed, blank, or unshrinkable model summary fails loudly; no-ops when there is nothing new to cover.
- Observer on `usage` + `run_finished`: auto-projects once the newest dispatch's measured prompt input crosses the trigger. The timely `usage` path uses the dispatch's persisted effective input limit (`UsageFact.input_limit_tokens`, read from the durable `PROVIDER_INTENT.projection_allocation.limit_tokens`); unknown usage or an absent limit never compacts. The `run_finished` path has no allocation of its own, so it remains unsized unless an explicit `threshold_tokens` is set. A summary the model cannot produce raises (a `ValueError` the core records as an owned `core.observer_error`) instead of silently doing nothing. An unsized gate returns before reading history.
- Stages one `context_compacted` plugin event per projection with the core-measured accounting, reason (`threshold`/`manual`), measured input tokens, and replacement bytes.
- The replacement carries a legacy `continuity:<sha256-16>` tag derived from the summary text and tool-result counts. Nothing verifies it; it is not an integrity check.

## Configuration

`plugins.compaction.config` (strict; unknown keys refused, `providers` accepted and ignored):

- `enabled` (bool, default `true`): `false` disables the observer; `/compact` still works, and a projection already in force remains. Use the outer `plugins.compaction.enabled: false` to remove the plugin (the core deletes its projection row and parks state).
- `threshold_tokens` (non-negative int or null, default null): explicit fixed budget the window must stay under. `0` is explicitly "off" with or without a catalog window. Authoritative even without a catalog window.
- `reserve_tokens` (non-negative int or null, default null): answer headroom kept free. An explicit reserve must be smaller than an explicit threshold, otherwise the configuration is refused. A defaulted reserve that cannot fit under an explicit threshold is dropped to `0` (the fixed budget wins) instead of collapsing the trigger to `1`.
- `keep_recent_tokens` (non-negative int, default `20000`): recent content that stays verbatim, converted with the newest dispatch's measured tokens-per-character rate (the core's dispatched message content plus tool-schema characters). The tail spend uses provider-visible characters: message content, assistant output, and tool-result text/error. It does not count JSON payload bytes, tool-call arguments, reasoning, or image bytes. If the newest dispatch carries an image or its image inventory is unknown, no honest rate exists; the tail is not retained and the prefix is covered as far as possible.

The `usage` observer passes the fact's persisted effective input limit (`input_limit_tokens`) as the budget basis; the derived trigger is `input_limit - reserve` (15% with the legacy floor and small-window recovery). There is no live model-limits probe and no guessed window: if the allocation is absent or the limit is unknown, the derived gate is unsized (`0`) and auto-compaction stays off for that dispatch. With an explicit `threshold_tokens` the trigger is `threshold - reserve` (or `threshold` when the defaulted reserve was dropped), using the limit to clamp the threshold when present; measured dispatch input above it compacts. The `run_finished` path passes no limit and stays unsized without an explicit threshold.

## Behavior and limits

- The plan covers the committed prefix up to a certified safe boundary (every native call answered inside the prefix) minus the kept recent tail; a summary whose replacement would not shrink the prefix raises, and `project_window` staging plus state/event commits are atomic — a failure applies neither.
- Summary material is a bounded representative sample: the earliest covered items (objective/constraints) and the newest covered work, with a marker for the omitted middle. Tool-call arguments are included as bounded declared intent (`args=`); result text/status stay separate. Images and clipped items are marked; when the sample is truncated, the prompt says so, and the instruction always treats the transcript as untrusted data. Per-item and total character caps apply.
- The replacement is always the model's summary text.
- Accounting is the core's measured numbers read back after verification (`covered_bytes`, `dropped_bytes`), never the plugin's plan. Committed history is untouched: compaction projects the model window only, and consumers still see every entry via [transcript](../../../../docs/contracts/transcript.md).
- Read window: the plugin reads at most 600 committed events from the session's first event (`_MAX_EVENT_PAGES * _EVENT_PAGE_SIZE`). A session with more events cannot be fully covered; once the visible prefix is exhausted, auto/manual compaction no-ops. This is a known major decision pending a prefix-certified history paging contract.
- Deactivation removes the projection row (the raw window becomes visible again) and clears the parked `last_projection`; the cursor and history survive, and a re-enable can re-project immediately without waiting for a new event. Inner `config.enabled: false` does not deactivate.
- Session state (`schema_version 2`): `cursor_sequence` plus the last projection's coverage. Only `summary_kind: "model"` is accepted; the retired `deterministic` kind is refused by the parser and schema (the older changelog note that such stored rows validate is superseded).
- `/compact` with nothing new to project reports a no-op: either the projection in force already covers the visible prefix, or the visible prefix has no newly coverable batch boundary yet.
