# Core/SDK Contracts

This directory contains the authoritative technical contracts for the VoidCode core, its SDK seam, and client integration. Plugin-owned usage, configuration, state, and limits live in each plugin's package README, indexed once in [docs](../README.md#plugins).

## Core contracts

- [`artifacts.md`](./artifacts.md): Shared artifact references, output recovery, and SDK storage/read boundaries.
- [`facts.md`](./facts.md): Typed durable observations, governance effects, core byte-observation semantics, and historical paging.
- [`transcript.md`](./transcript.md): Typed, lossless committed conversation paging and session inspection.
- [`agent-collaboration.md`](./agent-collaboration.md): Delegated agent collaboration: child facts, run-scoped join/steering, completion notices and atomic acknowledgement, restart publication recovery, and bounded transcript/history paging.
- [`background-actions.md`](./background-actions.md): Owner background actions: driver registration, per-invoke leases, source-isolated storage, cancel/wait, stale liveness, and idempotent child spawn.
- [`optional-queries.md`](./optional-queries.md): Typed, optional plugin collaboration: the generic read-only query transport, typed requests/answers, distinct absence/failure outcomes, and capability substitution.
- [`hot-activation.md`](./hot-activation.md): Same-session capability activation: trusted admission, immutable generation snapshots, state validation/deactivation, the activation transaction, durable automatic continuation, and restart restore.
- Generation adoption (cross-build moves) is stop-and-restart plus `bootstrap.adopt_generation`; there is no supervised upgrade path.
- [`client-transport.md`](./client-transport.md): WebSocket JSON-RPC, core CLI, inline TUI, and web client: catalog/revision binding, typed event frames, byte-paged artifact reads, and the two decision gates.
- [`plugin-authoring.md`](./plugin-authoring.md): Third-party plugin author contract: package layout, tools/commands/views, state deactivation, revisions, and executable diagnostics. Runnable reference: `examples/tally-counter/`.

## Plugin entry docs

See the canonical [plugin index](../README.md#plugins): one README per bundled plugin. Cross-references from core contracts point there (agent format → delegation, scripted workflows → workflows, goal accounting → goal, edit/version domain policy → files/lsp).

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
- **Root action budget**: a fresh action may carry an optional finite `root_action_budget`; omission creates no hidden provider-turn/action cap. Provider dispatches and deferred invocations reserve units atomically against that root, and fan-out children, queued follow-ups, child sends, and wakeups scheduled during an active run retain the root identity captured at reservation. Resume extensions are append-only, idempotently keyed grants; drains and restarts never reset usage. A fresh user action receives a new root identity, while unknown/legacy root identity is refused rather than guessed.
- **Cancel settlement**: an explicit user/owner cancellation settles parked work without an active run — an approval wait, and a recoverable (non-terminal) interruption — into one terminal checkpoint in the same transaction that cancels its owned claim, reporting the parked batch members as interrupted first. It only ever touches the session's own workspace, and a session that still claims parked work without its own authentic run identity is refused visibly before any write rather than reported as an ordinary no-work cancellation; an owner cancellation only scopes work staged by that same owner. A session admits fresh work (user turn, command-queued continuation, auto-drain) when it is idle/finished/failed or terminally interrupted; anything else requires an explicit resume, while a fresh user action started from a settled interruption opens its own new root.
- **Interrupted dispatches**: a provider call that never completed settles as an unknown-usage fact (never a fabricated zero, and never a wall-clock duration inferred across dead-process downtime) before any fresh dispatch is billed; failures that are not a real cancellation never report `user_stop`, and a missing stop cause on an interrupted run is a loud error rather than an invented cause. Run-finished facts carry the elapsed duration the originating process measured, persisted with the event, so replay never re-measures it; when it is absent the fact reports unknown.
- **Effect classification**: policy admission classifies *external mutation* (`WRITE`, `EXECUTE`, `SPAWN`) through one shared predicate. `read_only` refuses `WRITE` and `EXECUTE` — never `READ`, never `SESSION`, which is internal core/plugin state (a goal status update is not a filesystem or process mutation), and never `SPAWN` on its own, because delegation creates no privilege: a restricted owner may only spawn a child that inherits the restriction, and the core records that stance on the child. Approval mode auto-allows reads and internal state operations in every mode, so only externally mutating work reaches the human gate; `read_only` is checked before approval, so it stays stronger than `yolo`, and a `WRITE` tool still waits under `ask` and is still refused under `read_only`.
- **Keyed continuations**: a continuation enqueued with an idempotency key gets a deterministic identity derived from `(session, owner, key)`, so two concurrent leases that both stage before either commits return the same id, which names exactly one durable row; unkeyed continuations keep random identities, and an existing durable row for a key keeps its original id.
- **Budget admission**: explicit provider-turn caps (`max_steps`) and shared root-action budgets are separate dimensions; exhaustion is a non-success with source, consumed/remaining state, and the required continuation authority. `RunSpec.max_steps=None` leaves a fresh action uncapped; `ResumeSpec.max_steps=None` retains the checkpoint's current cap, while an explicit resume value sets a new total cap for that resumable action and must exceed already-consumed steps to allow progress. Resume does not reset step consumption or silently remove a finite cap. Unknown cost/token usage remains unknown; no large integer stands in for unlimited.

## Side model queries

`PluginServices.model_query` performs one governed, tool-free provider turn. It defaults
to an isolated prompt; `include_context=True` reads the current conversation window,
including its verified compaction summary and contributed plugin context. An unfinished
native tool batch is omitted as a whole, so providers never receive dangling tool calls.
The question and answer do not extend the main conversation; actual provider usage is
recorded in `core.model_query`, attributed to the invoking owner, and charged once to
the session usage report and active budgets. An exhausted budget refuses the query
before dispatch; missing usage stays unknown. The bundled `/btw` command requests this
contextual mode.
