# Core & Plugin Contracts

This directory contains the authoritative technical contracts for the VoidCode core, its plugin seam, and client integration.

## Contracts

- [`agent-definitions.md`](./agent-definitions.md): Claude Code Markdown agent format, supported execution subset, and discovery rules.

- [`transcript.md`](./transcript.md): Typed, lossless committed conversation paging and session inspection.

- [`artifacts.md`](./artifacts.md): Shared artifact references, output recovery, and SDK storage/read boundaries.

- [`facts.md`](./facts.md): Typed durable observations, tool semantics, historical paging, and effectiveness metrics.
- [`plugin-coupling-audit.md`](./plugin-coupling-audit.md): Remaining implementation dependencies, resolved todo coupling, and the next public contracts to introduce.

- [`optional-queries.md`](./optional-queries.md): Typed, optional plugin collaboration, replacement providers, absence and failure behavior, and capability-based workflow scopes.

- [`client-transport.md`](./client-transport.md): Core HTTP/SSE API, core CLI, inline TUI, and the web client: catalog/revision binding, view descriptors, event streams, and the two decision gates.
- [`agent-collaboration.md`](./agent-collaboration.md): Delegated agent collaboration: child facts, run-scoped join/steering, completion notices and atomic acknowledgement, restart publication recovery, and bounded transcript/history paging.
- [`goal-plugin.md`](./goal-plugin.md): Official Goal plugin contracts, state schema, windowed accounting, 3-turn blocked audit, and declarative views.
- [`hot-activation.md`](./hot-activation.md): Same-session capability activation: trusted admission, immutable generation snapshots, state validation/deactivation, the activation transaction, durable automatic continuation, and restart restore.
- [`core-upgrade.md`](./core-upgrade.md): Adoption transaction and restart recovery (historical spec; placeholder CLI commands in that file are no longer applicable).
- [`plugin-authoring.md`](./plugin-authoring.md): Third-party plugin author contract: package layout, tools/commands/views, state deactivation, revisions, and executable diagnostics. Runnable reference: `examples/tally-counter/`.

Wire models, configuration, and tool definitions are defined directly in code, which is the only source of truth:
- SDK contracts: `src/voidcode/sdk/` (`plugin.py`, `tools.py`, `commands.py`, `views.py`).
- Product configuration parsing: `src/voidcode/config.py` (`voidcode.json`); product wiring: `src/voidcode/bootstrap.py` (`open_core`, `adopt_generation`).
- Core event vocabulary and dispatch: `src/voidcode/core/engine.py`.
- HTTP wire models: `src/voidcode/transport/models.py`; the live OpenAPI document is served at `GET /api/openapi.json`.
## Core Durability Semantics

The core (`src/voidcode/core/`) governs runs over append-only events and durable native batches:

- **Observer receipts**: every fact-carrying event (`core.model_turn`, `core.provider_interrupted`, tool completion/interruption, run outcomes) is delivered to each subscribed owner exactly once, with a receipt row persisting the delivery. Delivery at the runtime boundary is restricted to exactly the owners that still miss a receipt for that source event, so one owner's receipt neither re-fires it nor blocks a still-missing owner, and each notified owner's staged state/events commit atomically with its receipts. Startup replay is restricted to the core's own workspace and active revision and uses the logical turn, run, and revision persisted with the source event (never a re-derived one).
- **Logical turns**: one logical turn per fresh provider dispatch, stable across native batches, approvals, and resumes; the root action identity of a durable chain is a separate, stable value.
- **Checkpoint references**: a checkpoint reference (`batch_sequence`, `intent_sequence`, `approval_sequence`) is bound to the actual appended sequence of its source event inside the same SQLite transaction that appends it. There is no follow-up fix-up commit, so a crash can never leave a durable native batch, ask, or tool intent without the checkpoint reference that names it, and a resume of that prefix is never refused or re-billed.
- **Root action budget**: each root chain (a user run or the first continuation of a chain) owns one finite durable budget. Each provider dispatch reserves one unit inside the same transaction that writes `core.provider_intent`, so crashes cannot refund a consumed step, and claims, drains, restarts, and fan-out children all share the original budget. A fresh user action started from a settled terminal interruption opens a new root; no drain or restart resets an existing root.
- **Cancel settlement**: an explicit user/owner cancellation settles parked work without an active run — an approval wait, and a recoverable (non-terminal) interruption — into one terminal checkpoint in the same transaction that cancels its owned claim, reporting the parked batch members as interrupted first. It only ever touches the session's own workspace, and a session that still claims parked work without its own authentic run identity is refused visibly before any write rather than reported as an ordinary no-work cancellation; an owner cancellation only scopes work staged by that same owner. A session admits fresh work (user turn, command-queued continuation, auto-drain) when it is idle/finished/failed or terminally interrupted; anything else requires an explicit resume, while a fresh user action started from a settled interruption opens its own new root.
- **Interrupted dispatches**: a provider call that never completed settles as an unknown-usage fact (never a fabricated zero, and never a wall-clock duration inferred across dead-process downtime) before any fresh dispatch is billed; failures that are not a real cancellation never report `user_stop`, and a missing stop cause on an interrupted run is a loud error rather than an invented cause. Run-finished facts carry the elapsed duration the originating process measured, persisted with the event, so replay never re-measures it; when it is absent the fact reports unknown.
- **Effect classification**: policy admission classifies *external mutation* (`WRITE`, `EXECUTE`, `SPAWN`) through one shared predicate. `read_only` refuses `WRITE` and `EXECUTE` — never `READ`, never `SESSION`, which is internal core/plugin state (a goal status update is not a filesystem or process mutation), and never `SPAWN` on its own, because delegation creates no privilege: a restricted owner may only spawn a child that inherits the restriction, and the core records that stance on the child. Approval mode auto-allows reads and internal state operations in every mode, so only externally mutating work reaches the human gate; `read_only` is checked before approval, so it stays stronger than `yolo`, and a `WRITE` tool still waits under `ask` and is still refused under `read_only`.
- **Keyed continuations**: a continuation enqueued with an idempotency key gets a deterministic identity derived from `(session, owner, key)`, so two concurrent leases that both stage before either commits return the same id, which names exactly one durable row; unkeyed continuations keep random identities, and an existing durable row for a key keeps its original id.
- **Budget admission**: every capped metric fails closed when its value is unknown (tokens, cost, and elapsed alike); the final cancellation and budget check runs immediately before the dispatch that reserves the root step and bills the provider.

## Side model queries

`PluginServices.model_query` performs one governed, tool-free provider turn. It defaults
to an isolated prompt; `include_context=True` reads the current conversation window,
including its verified compaction summary and contributed plugin context. An unfinished
native tool batch is omitted as a whole, so providers never receive dangling tool calls.
The question and answer do not extend the main conversation; usage is recorded in
`core.model_query`. The bundled `/btw` command requests this contextual mode.
