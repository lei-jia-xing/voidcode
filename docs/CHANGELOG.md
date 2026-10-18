# Changelog

Notable changes to the VoidCode runtime. Entries are grouped by the milestone in
`docs/runtime-kernel-and-self-bootstrap-plan.md` and only record behavior that is
covered by executable tests.

## Unreleased

### Capability activation (P3)
- Same-session hot update: a workspace runs one active capability generation, and a switch is a
  single SQLite transaction (`SqliteStore.commit_activation`) covering the immutable revision, the
  workspace pointer, every session ref (including the acting `running` session), migrated or
  deactivated owner state, queue/budget changes, events, projection and checkpoint. Blockers are
  re-checked inside the transaction; a refusal is recorded durably and nothing is cancelled
  implicitly.
- `Kernel` holds one immutable generation snapshot (revision, pinned SDK hash, effective config,
  admission, host, tools, commands) so configuration, catalog, schema, context and dispatch always
  describe the same generation. Each host dispatch holds a call slot until its result commits; an
  activation fence refuses new dispatches and reports foreign in-flight calls as busy instead of
  blocking them, and no lock or transaction is held across an RPC.
- Kernel-owned `kernel_reload` tool stages a durable request and returns; the reserved
  `kernel.reload` command uses the same activation service for explicit user switches. Approved
  switches activate the artifact pinned *before* the approval, so later source edits cannot change
  the approved target.
- Pure owner hooks `Plugin.migrate_state` (candidate host) and `Plugin.prepare_deactivate` (current
  host, session rows receive that row's own event tip, workspace rows `None`), validated against the
  optional `PluginManifest.state_schema`; downgrades are refused and disabled owners lose their
  queued work while keeping durable state.
- Durable automatic continuation: a model-requested switch records a resume intent in the acting
  session's checkpoint; the same run is the continuation, and a crash between commit and dispatch is
  finished once by the kernel's startup driver. A user-issued reload records no intent and bills
  nothing.
- Restart restores the persisted revision and its immutable private configuration snapshot (0600,
  `<state>/private/<revision_id>.json`) instead of re-admitting changed workspace source or config;
  trust is bound to canonical source paths captured once at startup.
- Observer facts are delivered to exactly the owners their *source* revision registered; receipts
  are one per `(event, owner)` with the misleading `generation` column removed.
- Kernel schema version 2: an existing v1 database is refused read-only before any write, and new
  `workspace_state` records the workspace's active revision. Mutating client invokes and
  confirmations now require `expected_revision` (the displayed catalog/view revision), and command
  catalogs and rendered views return the revision they were captured under.
- Pending plugin confirmations are durable facts: `kernel.confirmation_requested` commits atomically
  with the confirmation row, and every decision (allow/deny/stale) emits one
  `kernel.confirmation_decided`, so clients can restore pending requests after a reload and reduce
  them from the event stream instead of the live command result.

### Goal plugin (P2)
- New self-contained official plugin at `src/voidcode/plugins/goal/` (`__init__.py` +
  `goal_plugin.py`) speaking only the public SDK: tools (`create_goal`, `update_goal`,
  `get_goal`), commands (`goal`, `goal.set`, `goal.edit`, `goal.pause`, `goal.resume`,
  `goal.clear`, `goal.load_objective`), declarative views (`goal`, `goal.details`,
  `goal.form`), a context contributor, and a fact observer.
- Strongly typed JSON state with strict parsing: schema version, identity, status,
  objective, token budget, per-metric nullable token counters, measured elapsed time,
  cost, allowance windows, and blocked-audit counters. Unknown extra fields, missing
  required fields, and coerced booleans/numbers are rejected instead of silently
  repaired.
- Window-scoped budget enforcement: each work window opens with an explicit kernel
  constraint (`since_sequence` + allowance) computed from the truthful spend of closed
  windows; unknown spend is never treated as zero, so a finite budget raise or a budgeted
  resume is refused with a visible error until the user uncaps or creates a new goal.
  Unreported usage makes budgeted metrics unknown and fails closed (`usage-limited`),
  while the plugin cancels only its own automatic queue and keeps the constraint in place.
- Truthful accounting: unreported dispatches set token metrics to unknown rather than
  zero, known values cannot rewrite an unknown total into a partial number, unknown cost
  does not poison known tokens, and measured work time comes from provider dispatch
  facts instead of fabricated zeros.
- 3-turn genuine-block discipline: `update_goal(status="blocked")` stages an audit count
  once per distinct logical turn, fails intentionally for the first two turns, and only
  permits `blocked` after three consecutive distinct turns.
- Prompt parity restored: the model-facing text is the legacy goal prompt (`runtime/goal.py`)
  migrated verbatim — active continuation, budget limit and objective-edited variants keep
  their original wording, section order and `<objective>`/`<untrusted_objective>` markers,
  adapted only for nullable counters (`unknown`, never `0`) and the new `usage-limited`
  statuses. Tool descriptions are the legacy `tools/goal.py` text.
- State-bound presentation: `goal.form` is a create form (`goal.set`) with no goal and an
  edit form (`goal.edit`) with an existing goal, so a consumer edit preserves goal
  identity, creation time, charged usage and budget. `goal.pause` also escapes
  blocked/limited states, and `user_stop` applies the full pause policy (close window,
  release constraint, cancel queue).
- Long user objectives spill to unique, exclusively created workspace attachments
  (`.voidcode/attachments/<uuid>/goal-objective.md`) with a required SHA256 digest and
  workspace containment checks; attachment files are immutable history and are never
  overwritten or deleted. `goal.load_objective` reads the full text back.

### Kernel durability corrections (P2)
- Checkpoint references (`batch_sequence`, `intent_sequence`, `approval_sequence`) are
  now bound to the actual appended event sequence inside the same transaction that
  appends the source event (`SqliteStore.commit_staged(..., checkpoint_bindings=...)`),
  replacing the previous two-commit fix-up. A SIGKILL between the source append and the
  caller is covered by executable crash tests for both a native tool batch and a native
  approval ask: the fresh process resumes the authentic prefix, re-bills nothing, and
  cannot bypass the ask.
- Observer fact delivery is restricted at the host boundary to exactly the subscribed
  owners that still miss a receipt for that source event, so two-owner partial receipt
  state no longer re-fires the already-delivered owner.
- Keyed continuation identity is deterministic (`sha256(session, owner, key)`), so two
  concurrent leases that stage before either commits return one id that names exactly one
  durable row.
- Explicit user/owner cancellation settles recoverable parked work, not only approval
  waits; one shared fresh-work admission predicate (idle/finished/failed or terminally
  interrupted) now also gates command-queued auto-drive and confirmation re-invocation,
  and `confirm_action` routes owner cancellations like an ordinary invocation.
- Policy admission classifies external mutation (`WRITE`/`EXECUTE`/`SPAWN`) through the shared
  `has_external_mutations` predicate instead of requiring exactly `{read}`: internal `SESSION`
  state operations (native goal `create_goal`/`update_goal`, `goal.pause`/`goal.resume`/`goal.clear`)
  are no longer denied under `read_only` and no longer wait for approval under `ask`, restoring the
  legacy goal vertical; `WRITE` still waits under `ask` and is still refused under `read_only`
  (checked before approval, so `read_only` remains stronger than `yolo`), and external mutation
  combinations stay denied even alongside `READ`/`SESSION`.
- Unsettled dispatches settle before context assembly, every capped metric (tokens, cost,
  elapsed) fails closed on unknown, run-finished elapsed is measured at origin and
  persisted with the event instead of re-measured on replay, a missing stop cause on an
  interrupted run is an error rather than an invented `user_stop`, and a pre-existing
  same-version database missing the root action-chain columns (`continuations.root_id`,
  `root_budgets`) is rejected byte-unchanged by the read-only preflight.
- Verification pins, kept distinct on purpose: the Goal 27-case suite and its paid
  `deepseek-flash` **yolo** smoke (goal complete, 7331 tokens, 6.356 s, `cost_usd` `None`,
  restart with no extra spend) are recorded above at engine `95e72163…`, which already included
  the native-cancellation owner scope; the later engine revision `8f1bdb6045ff0efb…` adds the
  read-only/ask effect classification and was verified only by generic non-paid loopback runs
  (182 passed: governance/vertical/unit kernel+plugins+SDK, Goal 27 re-confirmed by the goal
  owner on that pin, kernel transport, kernel TUI) — no paid call is repeated for an engine hash
  change, and the read-only/ask branches are proven by
  `test_internal_session_effects_need_no_read_only_waiver_or_approval` plus the native-scope
  cases in `test_native_tool_cancellation_is_scoped_to_its_owner`.

### Documentation
- `docs/contracts/goal-plugin.md`: author/user contract for the goal plugin (state model,
  windowed accounting, command/view surface, spill and containment rules).

### Verification

**Full P2 is verified** (not P3). Run these from the repository root; each command is the
check itself.

```
uv run pytest tests/unit/plugins/test_goal_plugin.py tests/integration/test_goal_plugin.py -q
uv run pytest tests/integration/test_kernel_governance.py tests/integration/test_kernel_vertical.py \
  tests/unit/kernel tests/unit/plugins tests/unit/sdk \
  tests/integration/test_goal_plugin.py tests/unit/plugins/test_goal_plugin.py \
  tests/integration/test_kernel_transport.py tests/unit/test_kernel_tui.py -q
uv run ruff check src/voidcode/plugins/goal tests/unit/plugins/test_goal_plugin.py tests/integration/test_goal_plugin.py
uv run ty check src/voidcode/plugins/goal
uv run python scripts/generate_plugin_identities.py --check
```

- Exercised on the approved frozen stack (`kernel/engine.py 8f1bdb60…` — the paid-evidence
  `95e72163…` plus the SESSION read-only/ask classification only — `kernel/host.py 4b4c8453…`,
  `kernel/storage/store.py 8947131f…`, SDK directory input `b15172ef…`, plugin
  `goal_plugin.py 4ee8a848…` / manifest `5556ecdf…`): **182 passed** inclusive — governance +
  vertical + unit kernel/plugins/SDK (150), Goal 27 (19 unit + 8 integration), kernel transport,
  kernel TUI. Goal coverage: strict state parsing, windowed accounting, unknown metrics, budget
  refusal/uncap, the three-turn blocked audit including "one native batch = one turn", long
  objective single-frame transport with tamper detection, second-plugin isolation and
  process-restart persistence.
- Live smoke on the paid-evidence stack: `deepseek/deepseek-flash` with an **idle `goal.set`**
  (no user prompt, no manual drain) — the goal context alone drove read `17 25` → write `42` →
  read back → explicit complete; goal `complete`, tokens 7331, measured elapsed 6.356 s,
  `cost_usd` preserved as `None`, and a fresh kernel showed the same state without extra work.
  Paid evidence stays at `engine 95e72163…`; the later hash adds only the SESSION
  read-only/ask classification and is covered by generic non-paid loopback tests, so no paid
  call is repeated for it.
- Startup acceptance (throwaway script, deleted): a genuine goal persisted through production
  commands, ordinary base chat working with the plugin config-disabled and no goal context in
  the request, then the exact config restored with the original session intact, no unintended
  provider work and no leftover continuation. Same-session live activation belongs to P3.
- Client-side proof (CLI/HTTP/Web/TUI, GenericSurfaces, accepted): `KernelApp 8ebf0d06…` and
  `Forma fe0d460…` frozen; the real full-bundle browser run with `KERNEL_SMOKE_GOAL=1` passed in
  1.3 min (cold start yields exactly one auto session; same-SID trailing-slash client flip;
  parked native approval page reload keeps the same id; native allow/deny; stored confirms;
  real first `goal.edit` POST with budget 5000 returning status ok; null POST status ok; same
  goal id / created_at / exact 4503-char text), the full-goal PTY run is green (31 commits /
  4618 paints, no settled replay or duplicates), frontend 169 cases + typecheck/eslint,
  transport+TUI 24, and API/identity checks all pass.

### Kernel and SDK (earlier P1/P2 milestones)
- Isolated plugin host with per-call leases, strict capability admission, and
  persist-before-activate revisions.
- Scoped plugin services: optimistic state replacement, namespaced events, durable
  continuations with idempotency keys, windowed usage reports, owner budgets, and typed
  context/observer callbacks.
- Generic CLI and HTTP clients over one kernel transport surface, including command
  catalogs, view rendering, confirmations, and continuation draining.
