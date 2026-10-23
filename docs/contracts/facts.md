# Public session facts

Plugins depend on SDK contracts, rather than another plugin's tool names or the core's persisted event payloads. The existing observer callback is the push interface; `PluginServices.read_facts()` is the historical interface. Both use the same core decoder and SDK dataclasses. SQLite's append-only session events remain the sole source of truth; there is no separate bus, queue, dependency, or fact table.

## Meanings and boundaries

| Contract | Meaning |
| --- | --- |
| `ToolEffect` | Authority, approval, read-only admission, and recovery policy. `WRITE` does not imply a file edit. |
| `ToolDefinition.semantics` | Business purpose declared by a tool. `ToolSemantic.RESOURCE_EDIT` identifies edit attempts, including failures. Names are presentation and dispatch identities. |
| `ToolSuccess.coverage` | Existing `ReadObservation` records: path, content version, and lines/whole-file window the result reports showing. |
| `ToolSuccess.changes` | `ResourceChange` records for successful create/update/delete/move operations. One invocation can change several resources. |
| `InvocationFinishedFact` | Outcome, frozen owner/effects/semantics, model attribution, measured JSON envelope sizes, result bounds, diagnostic category, read/change observations, and declared resource paths. |
| `WakeupFiredFact` | One atomic wakeup claim, with recorded owner, wakeup identity, declared role and immutable payload. It is not a user request or a run completion. |
| `ChildSpawnedFact` | One actual child session. A batch with three children yields three source facts, regardless of its tool name. |
| `RunRequestedFact.resumed` | An explicit resume request. A second user prompt is not a resume. |
| `ApprovalRequestedFact` | One actual approval request. |
| `ContextProjectedFact` | An applied core window projection with measured covered/dropped UTF-8 bytes. It does not depend on a compressor plugin id. |
| `UsageFact`, `RunFinishedFact` | Existing truthful usage and terminal-run observations. |

Mixed-mode tools declare `ToolDefinition.semantic_rules`: a mapping from `ToolSemantic` to a Draft 2020-12 JSON Schema over invocation arguments. `semantics_for(arguments)` combines static `semantics` with all matching rules. Rules are immutable, validated at definition ingress, and survive the ordinary definition codec; they are data rather than executable callbacks or a custom predicate language.

```python
ToolDefinition(
    name="structural_tool",
    description="Search, preview, or replace code",
    effects=frozenset({ToolEffect.WRITE}),
    semantic_rules={
        ToolSemantic.RESOURCE_EDIT: {
            "properties": {"mode": {"const": "replace"}},
            "required": ["mode"],
        },
    },
)
```

`ast_grep` uses this rule: search and preview are not edit attempts; replace is an edit attempt even if validation, approval, or execution refuses it. One semantic can use standard `anyOf`/`allOf` schemas for more complex conditions. Static meanings always apply; matching rules add meanings and never remove them. These predicates do not alter effects, approval, read-only admission, replay policy, or input validation.

The core freezes the resolved semantics in the tool intent before execution. Failures use that recorded declaration too. Pre-execution refusals resolve against the same invocation arguments without creating an intent; unresolved tools have no declared meanings. History and recovery read the recorded meanings and never re-evaluate a changed catalog's predicates.

Tools report their own successful resource observations. The core records them with invocation identity and the declaration used for that invocation. Consumers must not reconstruct historical semantics from the currently installed catalog. Read observations do not themselves grant write permission: the existing coverage reducer still enforces clipping, version, session, and model visibility constraints.

A tool can declare paths using `path_argument_keys`, including `edits[].path`. Invocation facts collect those paths and successful read/change paths. Skills uses that surface for touched-file rules; it no longer guesses parameter names or parses patch text. Undeclared shell effects are not inferred from command text. Resource observations are claims by trusted tools, not filesystem monitoring.

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

Installing a window projection is staged with the callback, and the verified projection and its core fact commit in the same SQLite transaction. A callback/commit failure applies neither. No database schema migration is needed.

## Current contract and effectiveness v2

Only the current persisted fact shape is supported. Invocation outcomes require their full recorded semantic metadata, and public fact events require their recorded observer eligibility. Missing fields fail explicitly; the runtime never joins older tool intents, fills fields from the current catalog, or bypasses replay validation for older events. There is no legacy-data adapter or automatic conversion. This SDK change follows the existing explicit core-image upgrade rules, which do not make incompatible event records compatible.

`/effectiveness` emits `schema_version=2` and aggregates public facts:

- Repeated reads mean the same path, content hash, line window, whole-file flag, and clipping flag were observed again. Another content version is a different observation.
- Follow-up reads add lines to an earlier partial observation of the same version, or finish that version with a complete whole-file read. Repeating the same partial range is not a follow-up.
- Per-model edits include only declared edit attempts or actual recorded changes with model attribution. A missing model attribution is a legitimate unknown, distinct from an invalid record with missing required metadata.
- Delegated tasks count actual child facts; resumes count explicit resumes; compression counts core projection facts.
- `result_bytes` measures the canonical UTF-8 JSON result envelope, including its output, body, and observations. It is not provider wire size or token usage.
- The report retains aggregate counts only. It reads persisted public SDK facts and retains no resource paths, source text, arguments, or output text.


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
payload kind before settling or rearming a schedule. Stale and foreign firings
still advance its source cursor but cannot affect its schedules.
