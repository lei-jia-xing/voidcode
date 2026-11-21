# compaction

Summarize the covered conversation prefix into one verifiable system message, on demand (`/compact`) or automatically past a token gate.

## Surface

- Command `/compact` (effects `SESSION`, `busy_gate=True`, no arguments): projects the covered prefix on demand and reports the core's measured accounting (`covered_from_sequence`, `covered_to_sequence`, covered/dropped bytes, event count, summary kind). A failed, blank, or unshrinkable model summary fails loudly; no-ops when there is nothing new to cover.
- Observer on `usage` + `run_finished`: auto-projects once the newest dispatch's measured prompt input crosses the trigger. Unknown usage never compacts. A summary the model cannot produce raises (a `ValueError` the core records as an owned `core.observer_error`) instead of silently doing nothing.
- Stages one `context_compacted` plugin event per projection with the core-measured accounting, reason (`threshold`/`manual`), measured input tokens, and replacement bytes.

## Configuration

`plugins.compaction.config` (strict; unknown keys refused, `providers` accepted and ignored):

- `enabled` (bool, default `true`): `false` disables the observer; `/compact` still works.
- `threshold_tokens` (non-negative int or null, default null): explicit fixed budget the window must stay under. Authoritative even without a catalog window.
- `reserve_tokens` (non-negative int or null, default null): answer headroom kept free.
- `keep_recent_tokens` (non-negative int, default `20000`): recent content that stays verbatim.

The observer resolves the gate with no catalog window, so without an explicit `threshold_tokens` the trigger is unsized (`0`) and the gate stays off. With an explicit threshold the trigger is `threshold - reserve`; measured dispatch input above it compacts. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- The plan covers the committed prefix up to a certified safe boundary (every native call answered inside the prefix) minus the kept recent tail; a summary whose replacement would not shrink the prefix raises, and `project_window` staging plus state/event commits are atomic — a failure applies neither.
- The replacement is always the model's summary text.
- Accounting is the core's measured numbers read back after verification (`covered_bytes`, `dropped_bytes`), never the plugin's plan. Committed history is untouched: compaction projects the model window only, and consumers still see every entry via [transcript](../../../../docs/contracts/transcript.md).
- Session state (`schema_version 2`): `cursor_sequence` plus the last projection's coverage. Disconnect/re-enable keeps history untouched; nothing is re-armed automatically.
