# Background Actions Contract

Owner background actions are plugin-owned drivers that run outside any model turn.
A plugin enqueues an action, the core claims it and runs the owner's
`Plugin.background_action` driver on a worker thread, and each driver `invoke`
commits under a fresh lease. The foreground session stays usable throughout:
drivers never take the session run lock.

## Identity and storage

- Actions share the `continuations` table with model continuations. The discriminator
  is the core-controlled `source` column (`@core/background`), never `payload.kind`:
  ordinary continuations accept arbitrary JSON and must pass through untouched.
- Idempotency namespaces are separate per source family: the same
  `(session, owner, key)` may name one model message AND one background action, and
  neither lookup swallows the other. Keys are arbitrary strings, never prefixed.
- Every model path filters background rows out: claim, pending/claimed reads,
  the claimed-work guard, staged cancels, adoption and activation blockers.
  `cancel_continuations` never touches a background row; cancellation goes through
  the abort-aware `cancel_background_action`.
- No schema bump: the envelope (`{claim, action}`, plus a core-owned top-level `error`
  on failed settles) lives in the existing payload column. The owner's action object is
  arbitrary JSON and is never rewritten: a business `error` key inside it is data, and
  status/wait readers only ever read the envelope-top `error`.

## Driver contract

- `Plugin.background_action(context)` runs once per claimed action on its own
  thread. The driver keeps only the `BackgroundContext` (action id, immutable
  payload, abort event) — never a `PluginServices`.
- `context.invoke(callback)` runs the callback exactly once under a fresh
  owner/revision-bound lease and commits its staged work atomically. Only a
  `SequenceConflict` retries, by re-submitting the SAME staged proposal against
  the fresh tip; any other conflict fails visibly. Callbacks are never replayed.
  A lost owner-state CAS raises the typed SDK `StateConflict`: the transaction
  committed nothing, and an owner that knows its callback is a pure/idempotent
  state mutation may re-read and re-run that callback itself (bounded).
- `ToolContext.abort_signal` inside a callback is a live view of the action's
  abort: explicit action cancellation, core shutdown, or an admitted revision
  retirement sets it, and the next invoke boundary raises `InterruptedError`.
- `context.activation_requested` is set before an activation tries to take the
  dispatch fence and cleared if the attempt is refused. A long-lived driver
  should not start a new `invoke` while it is set, and should leave its current
  one promptly, so activation can drain cooperative drivers. Refusal after the
  bounded drain window still reports the in-flight identities.
- A driver that returns settles `completed`; an explicit cancellation of its
  bound action settles `cancelled`. A driver that raises settles `failed` with
  its `Type: message` recorded in the envelope. Shutdown and lost work preserve
  their interrupted stop cause rather than being reported as user cancellation.
  A missing driver for the claimed owner is a `failed` settle, never a silent drop.

## Awaiting a deferred tool

`PluginServices.invoke_tool(...)` stages a same-session call and returns a
`ToolInvocationReceipt`; it does not execute or wait inside the callback. A background
driver may wait only after its `context.invoke(...)` callback has returned:

```python
receipt = context.invoke(lambda ctx, services: services.invoke_tool("read", {"path": "README.md"}, expected_revision=ctx.revision_id))
outcome = context.wait_tool(receipt)

if outcome.status == "completed":
    completed_result = outcome.result  # ToolSuccess or ToolFailure, never a preview
elif outcome.budget_exhaustion is not None:
    exhaustion = outcome.budget_exhaustion  # typed BudgetExhaustion
else:
    cause = outcome.stop_cause
    execution = outcome.execution  # may describe unknown effects
```

- The result is correlated to the exact request, owner, action, session, and revision.
  Completed outcomes carry the actual `ToolSuccess` or `ToolFailure`; canonical output
  references are hydrated in full. Other terminal outcomes may still carry an actual
  result if the tool ran, but a request that never ran or was lost has no invented result.
  Inspect `status`, `stop_cause`, `budget_exhaustion`, and `execution`; only committed
  `user_stop` / `owner_cancel` is cancellation. Budget exhaustion is typed; shutdown,
  errors, and lost execution remain interrupted with actual or unknown effect metadata.
- `wait_tool` is for the background driver only, outside every active leased callback
  (including tool, command, and hook callbacks). Reentrant use is refused rather than
  blocking a callback that still owns the lease. The host releases the callback,
  activation, and session-run guards before waiting; it retains neither a
  `PluginServices` object nor a lease, so ordinary client/API work and approval remain
  responsive. Completion/cancellation notifications wake the waiter; it does not poll.
- The ordinary dispatcher remains authoritative: the request uses the action's captured
  origin tool visibility, stance, and root authorization, plus current restrictions that
  can only narrow it, and still passes revision, schema, pre-hook, approval, recursion,
  and inherited-budget gates. Missing legacy captured authority, hidden tools, and stale
  revisions fail visibly; later broader scope or root grants are never borrowed.
- Exact action cancellation disarms its queued approval/request; unrelated native
  approvals and actions are untouched. A confirmed in-flight stop is not implied by a
  cancellation signal: unresolved effects stay unknown/interrupted. Core close wakes
  waits with shutdown semantics. A lost process does not reconstruct Python frames or
  replay requests; completed mutations remain committed and pending edits are not run
  after restart. A new explicit action is required to continue.
- This is a safe background composition API, not foreground suspension: ordinary plugin
  callbacks cannot suspend and later resume with their Python stack intact. Full
  foreground `eval` and remote-resource capabilities remain separate P1 product work.

See also the [SDK service-scope contract](./plugin-authoring.md#7-sdk-service-scopes-and-contracts).

## Status, cancel, wait

- `background_action_status` is owner- and session-bound: foreign rows read as
  unknown. `pending` / `running` / `completed` / `failed` / `cancelled` come
  from the row; `interrupted` means claimed-but-unattended (see below).
- `cancel_background_action` marks a pending row `cancelled` without ever
  running it; a live driver is signalled and settles `cancelled` itself; a
  stale claim is marked `cancelled` outright. Terminal and foreign rows return
  `False`.
- `Core.wait_background_actions(session_id)` waits for the session's currently
  active actions; past failures never poison a new wait. With `action_id` the
  wait names one exact receipt: even a pre-settled failure raises `RuntimeError`
  with its id and recorded error. Stale rows and timeouts are reported honestly,
  never as success.

## Liveness without heartbeats

- Cross-process double-driving is impossible: `Core` construction holds the
  workspace's exclusive flock (`another process owns this workspace`), so no
  second process can hold a claim on the same database. A claimed row this
  process does not drive is therefore stale by construction.
- Stale rows report `interrupted` and need an explicit new action; nothing is
  ever auto-replayed. Closing the core wakes live waits with shutdown semantics.
  A public activation is refused while a session is running or its work remains
  claimed; after activation is admitted, safe-boundary retirement parks any
  remaining live driver. The activation transaction rebinds the *acting session's*
  pending rows to the new revision, and those rows run under the new generation;
  rows on other sessions keep their recorded revision and are only driven when
  their revision is active again, never silently migrated. A claimed row parked
  by retirement never resumes: resume is always an explicit new action.

## Idempotent child spawn

- `spawn_child(..., idempotency_key)` maps `(parent, owner, key)` to one
  deterministic child id (canonical-JSON encoded, then hashed). A retry with
  the same effective spec (prompt, scope with `None` distinct from `[]`,
  max steps, stance) returns the existing child; the same key with a different
  spec is refused visibly.
- The mapping is readable without creating: `find_child` (service) and
  `Core.find_keyed_child` return the mapped child or `None`, bound to the
  calling parent and owner. The child's own birth record (`core.child_reserved`,
  sequence 1, committed atomically with the child row) carries the keyed identity
  and original spec hash, so the mapping stays verifiable even when a crash lands
  between the child row and the parent fact; a differing spec is still refused.
  The reserved kind is core bookkeeping: it projects to no public fact,
  transcript entry, history block, or observer notice.
- The spawn budget is the single governed quota
  (`ExecutionConfig.max_root_children`, default 4): concurrent spawns at the
  limit accept at most one child, and a full budget still re-fetches an
  existing key instead of refusing it.
- Crash-window repair starts a driver only for a pristine child this core has not
  claimed yet; the start claim is atomic, so concurrent retries start exactly one
  initial run (a fresh core repairs the same way). A child that already ran is
  returned as-is with its unknown mutations never replayed.
- Cancelling a reserved child whose first run never started claims it with an
  owner-cancel interruption: it reports cancelled, a late driver refuses to
  start it, and only an explicit follow-up may run it afresh.
