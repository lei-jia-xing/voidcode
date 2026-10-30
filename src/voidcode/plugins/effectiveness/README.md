# effectiveness

Aggregate per-tool outcome metrics for the current session from committed facts.

## Surface

- Command `/effectiveness` (no arguments). Runs with `busy_gate=False` and the `READ` effect.
- Returns a human-readable summary plus the full report payload (schema version 2): per-tool success/error counts and rates, per-model edit-tool outcomes, totals, and measured usage. `null` means unknown, never zero.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Reads the calling session's public facts in pages of 200 and measured usage via SDK services. Core fact fields and byte semantics live in [facts](../../../../docs/contracts/facts.md); what follows is this plugin's projection on top of them.
- Report definitions: `repeated_read_count` counts reads whose (path, content hash, lines, whole-file, clipped) identity was already seen; `followup_read_count` counts reads adding new lines for an already-seen (path, content hash) version, or a first full unclipped read after partial ones. The per-model table is a WRITE-or-changes proxy: an invocation counts as an edit for its model only when its effects include `WRITE` or it reports changes, and only when a model is attributed. Counters also include compactions (`ContextProjectedFact`), approval requests, resumed runs, and delegated tasks (`ChildSpawnedFact`).
- Per-tool rows add retries-after-error (next success after an error), retry-guidance, truncated/partial, argument/result byte totals and averages, and per-error-kind counts. Sessions with no calls report `null` rates; usage with any unknown value sets `has_unknown`.
- Stateless projection: keeps no state and stages no plugin events. No arguments, result text, paths, or hashes are retained — only counts, rates, byte totals, and error kinds. (The core still persists the command's `COMMAND_COMPLETED` receipt.)

The slash command also presents readable diagnostics with refresh and ten-item
paging (`offset` in structured invocation). Unknown usage and price stay unknown;
coverage is marked incomplete when reported facts cannot establish full usage.
Tool/model names and error categories are previews; full aggregate fields remain
in command data. Reading diagnostics does not mutate plugin state; invocation receipts remain durable history.

Factory configuration accepts no plugin options; only the injected `providers`
map is ignored. Unknown fields are rejected visibly.
