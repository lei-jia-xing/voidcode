# Public session facts

Plugins depend on SDK contracts, rather than another plugin's tool names or the core's persisted event payloads. The existing observer callback is the push interface; `PluginServices.read_facts()` is the historical interface. Both use the same core decoder and SDK dataclasses. SQLite's append-only session events remain the sole source of truth; there is no separate bus, queue, dependency, or fact table.

## Meanings and boundaries

| Contract | Meaning |
| --- | --- |
| `ToolEffect` | Authority, approval, read-only admission, and recovery policy. `WRITE` does not imply a file edit. |
| `ToolDefinition` | Authority declaration: name, description, input schema, effects, replay policy, icon. Business meaning comes from recorded observations, not a separate declaration: there are no per-tool semantic predicates. Names are presentation and dispatch identities.
| `ToolSuccess.coverage` | Existing `ReadObservation` records: path, content version, and lines/whole-file window the result reports showing. |
| `ToolSuccess.changes`, `ToolFailure.changes` | `Change` records for committed create/update/delete/move operations. A failed or cancelled invocation may have committed a prefix; its outcome does not erase those changes. |
| `InvocationFinishedFact` | Outcome, frozen owner/effects/semantics, model attribution, measured JSON envelope sizes, result bounds, diagnostic category, read/change observations, and declared resource paths. |
| `WakeupFiredFact` | One atomic wakeup claim, with recorded owner, wakeup identity, declared role and immutable payload. It is not a user request or a run completion. |
| `ChildSpawnedFact` | One actual child session. A batch with three children yields three source facts, regardless of its tool name. |
| `RunRequestedFact.resumed` | An explicit resume request. A second user prompt is not a resume. |
| `ApprovalRequestedFact` | One actual approval request. |
| `ContextProjectedFact` | An applied core window projection with measured covered/dropped UTF-8 bytes. It does not depend on a compressor plugin id. |
| `UsageFact` | One truthful provider dispatch: session/run/revision, logical turn, exact event and dispatch sequences, provider/model, usage (missing stays unknown), cost, elapsed time, `source` (`turn` or `model_query`), and the invoking owner for a side query. A governed model query has no separate provider intent, so its own committed event is the dispatch record and `dispatch_sequence == event_sequence`. |
| `RunFinishedFact` | Terminal or paused run state with the canonical stop cause, measured elapsed time, and the typed `budget_exhaustion` cap when the core recorded one. A cap names its dimension and exact source: `provider_turns`/`max_steps`, `root_actions`/`root_action_budget`, or an owner budget (`owner_tokens` with `max_input_tokens`/`max_output_tokens`/`max_total_tokens`, `owner_cost_usd`/`max_cost_usd`, `owner_elapsed_seconds`/`max_elapsed_seconds`) with measured `consumed`, the configured `limit`, clamped `remaining`, and its continuation authority. An unknown metric fails before a stop and never carries guessed counters. |

Tools declare governance effects (`ToolEffect`) on their definition; results carry observations (`coverage`, `changes`). The core freezes both into `fact_metadata` at commit time: owner, effects, measured envelope sizes, result bounds, diagnostic category, read/change observations, and declared resource paths. `InvocationFinishedFact.semantics` is a retained required metadata slot: the current engine records it empty for every invocation (the tool intent hardcodes `"semantics": []`), and consumers must treat it as opaque committed data, never re-derive it from the current catalog. There are no per-tool semantic declarations, predicates, or argument rules; `ast_grep` declares plain `WRITE` effects for all modes.

The core freezes the recorded declaration in the tool intent before execution. Pre-execution refusals create no intent; unresolved tools have no recorded declaration. History and recovery read the recorded metadata and never re-evaluate a changed catalog.

Read observations are historical facts, not write permissions. File mutations validate explicit content hashes and edit matching without requiring a prior read in the session.

When the core projects a `TextOutput`, omitting its redundant machine body does
not clip the selected page or invalidate its read observations. Only actual
selected-content clipping (or the producer's own `ReadObservation.clipped`)
marks those reads clipped. Complete machine data remains at the canonical
result URI. Read facts therefore distinguish an omitted body from unseen file
bytes; ordinary whole-file reads do not become incomplete just because their
typed body repeats the displayed page.

A tool can declare paths using `path_argument_keys`, including `edits[].path`. Invocation facts collect those paths and successful read/change paths. Skills uses that surface for touched-file rules; it no longer guesses parameter names or parses patch text. Undeclared shell effects are not inferred from command text. File-tool observations are claims by trusted tools, not filesystem monitoring — but exec changes ARE supervisor-observed: when the sandbox backend is configured, the confined supervisor snapshots writable roots before/after each child and the engine merges those observed changes into the tool's `changes` (success and failure alike, deduped against the tool's self-report). Background jobs contribute their cumulative sidecar when read or cancelled; service processes never stage.

Command completion events separately retain bounded, deduplicated supervisor
observations in `observed_changes` (canonical host paths, excluding scratch-only
changes), even when the callback throws or is cancelled. These observations are
not a complete account of external effects and do not create tool invocation
facts. A cancelled command records `cancelled: true` and an error outcome; its
staged plugin state, events, continuations, cancellations, and budget are discarded.

A deliberate user/owner stop settles the abandoned native tool batch before the
run interruption is recorded. The active invocation reports unknown external
effects; remaining calls report that they were not executed. Cancelled tool
self-reports are discarded. A crash or shutdown retains its recoverable
checkpoint; recovery still governs whether an unfinished invocation may replay.

Hooks which authorize an invocation remain synchronous control contracts (`ToolIntent`, gate decisions). Facts describe committed outcomes and cannot authorize or rewrite them.

## Reading and observing

```python
from voidcode.sdk.hooks import InvocationFinishedFact

cursor = 0
while True:
    page = services.read_facts(after_sequence=cursor, limit=100)
    for fact in page.facts:
        if isinstance(fact, InvocationFinishedFact):
            for change in fact.changes:
                consume(change.path, change.operation)
    cursor = page.next_sequence
    if not page.has_more:
        break
```

`limit` is a source-event limit in `1..200`, not a number of matching facts or a byte budget. A page may contain no public facts and still have `has_more=True`; always advance to `next_sequence`. One source event produces at most one fact; batch resource observations are nested inside that fact, so paging cannot skip siblings. Facts contain semantic metadata, not raw arguments or output bodies.

Observers subscribe using `Plugin.observed_facts`. Their eligibility is frozen on source events, and their state/events plus per-owner receipt commit together. Newly staged projection facts are delivered after the callback's proposal commits. Child facts appended while a parent tool is running are delivered at the next governed commit after that callback returns. Startup replay settles undelivered facts; a committed receipt prevents repeat delivery. External effects inside observers are not made exactly-once by a SQLite receipt.

A provider intent is in flight, not observed usage. New orphan dispatch settlements append an interruption event, so a cursor that passed the intent still sees unknown usage later.

A committed `core.model_query` is a public usage fact: one governed side query produces exactly one `UsageFact` with `source="model_query"` and the invoking plugin owner. It is delivered to `usage` observers under the same frozen-eligibility/once-only receipt rule as a turn, so observer-maintained accounting includes side-query spend exactly once; the session usage report still derives the same bill directly from the event and neither path doubles it. A side query committed before the root session ever captured a run root has no authorization to lease observers under: its fact stays readable through `read_facts`, and delivery remains pending until a run captures a root (child sessions use their birth record). Model-query events written before this fact contract carry no frozen recipients and are not replayed to push observers.

Installing a window projection is staged with the callback, and the verified projection and its core fact commit in the same SQLite transaction. A callback/commit failure applies neither. No database schema migration is needed.

Provider usage `output_tokens` counts total generated output, including reasoning. When a provider reports candidate and thinking counts separately, the adapter sums them only when both are known; an incomplete total remains `None`, distinct from an observed zero. Historical usage facts retain the values recorded by their original adapter and are not recomputed.

## Current contract

Only the current persisted fact shape is supported. Invocation outcomes require their full recorded metadata, and public fact events require their recorded observer eligibility. Missing fields fail explicitly; the runtime never joins older tool intents, fills fields from the current catalog, or bypasses replay validation for older events. There is no legacy-data adapter or automatic conversion. Generation adoption (stop the old build, start the new one against the same workspace and state) does not make incompatible event records compatible.



## Wakeup ownership and recovery

A wakeup claim atomically records its run identity and eligible observer owners.
The core delivers `wakeup_fired` before driving that run. If it stops after the
claim and before delivery, startup replays the same source fact. A committed
per-owner receipt prevents duplicate callback effects on subsequent restarts.

`WakeupFiredFact.payload` is the owner's scheduled message data, recursively
immutable; it is not an inferred tool argument or output. A subscriber may observe
other owners' wakeups in its session, so owner-specific policy checks `fact.owner`
against `services.owner`, the identity bound to its live lease. This supports
admitting the same source under a different plugin id without a hardcoded owner.
Cron additionally requires the tracked current wakeup id, schedule id and its own
payload kind before recording a pending settlement. It records that pending
settlement from the `wakeup_fired` fact and settles it on the session's next
terminal `run_finished`, so a resume under a new run identity is still joined to
the original firing. The observer is only a trigger: on run-start, wakeup, and
terminal notifications it pages facts from a durable cursor to exhaustion and
commits the cursor with the resulting settlements. A failed callback leaves the
cursor untouched, and the next writable trigger replays the same range; terminal
facts found in that range settle even when the run is no longer live. Stale and
foreign firings are ignored without touching a schedule.
