# Goal Plugin Technical Contract

Authoritative specification for the official VoidCode Goal Plugin (`src/voidcode/plugins/goal/`).

The Goal Plugin implements autonomous pursuit of user objectives, windowed token accounting, hard core budget enforcement, 3-turn blocked-audit discipline, durable continuations, and declarative UI presentations exclusively through the public VoidCode SDK.

---

## 1. Boundary & Governance

- **Zero Privileged Coupling**: The plugin interacts with the runtime solely through `PluginServices`, `ToolContext`, `CommandInvocation`, and typed fact observers. It makes zero imports from `core/`, `runtime/`, or `storage/` and owns no internal SQLite tables.
- **State Partitioning**: Durable state is governed by the core under namespace `goal`, scope `session`, and `schema_version = 1`. All state updates are atomic CAS operations via `services.replace_state(expected_version)`.
- **Registration**: the `Plugin` returned by `create_plugin` declares tools/commands/views plus `observed_facts=("usage", "invocation_finished", "run_finished")`; definitions live next to the handlers in `goal_plugin.py`.

---

## 2. State Model (`GoalState`)

```python
GoalState:
    schema_version: int = 1
    goal_id: str                      # UUID4 hex identity
    status: GoalStatus                # active | paused | blocked | usage-limited | budget-limited | complete
    objective: str                    # Full text if <= 4000 chars, or attachment reference prompt if spilled
    created_at: int                   # Millisecond epoch timestamp
    updated_at: int                   # Millisecond epoch timestamp
    objective_file_ref: str | None    # Relative path (.voidcode/attachments/<uuid>/goal-objective.md)
    objective_file_sha256: str | None # SHA256 digest of spilled file content
    token_budget: int | None          # Explicit positive integer budget or None (unbounded)
    tokens_used: int | None           # Lifetime cumulative tokens consumed (nullable when unreported)
    window_tokens_used: int | None    # Current allowance-window spend (None when unreported in this window)
    window_allowance: int | None      # Core-enforced allowance for the current window (None when unbounded)
    input_tokens: int | None          # Cumulative input tokens (nullable)
    output_tokens: int | None         # Cumulative output tokens (nullable)
    cache_read_tokens: int | None     # Cumulative cache read tokens (nullable)
    uncached_input_tokens: int | None # Cumulative uncached input tokens (nullable)
    has_unknown_usage: bool           # True if any active window encountered unreported usage
    time_used_seconds: float | None   # Cumulative measured execution elapsed time
    cost_usd: float | None            # Cumulative provider cost in USD (nullable)
    active_windows: tuple[ActiveWindow, ...] # Contiguous dispatch sequence accounting intervals
    blocked_turns_count: int          # Consecutive distinct logical turns asserting blocked status
    last_blocked_logical_turn: str | None # Identifier of the most recent logical turn requesting blocked
    objective_edited: bool            # True once the goal objective was edited by the user; reset by replacement/clear
```

### Strict Parsing Rules
- Unknown extra keys are rejected with `ValueError`.
- Missing required fields (`schema_version`, `goal_id`, `status`, `objective`, `created_at`, `updated_at`) fail deserialization.
- No coercive defaults: unreported metrics remain `None` and are never coerced to `0`. Unknown statuses are never coerced to `active`.

---

## 3. Windowed Accounting & Budget Enforcement

### Dispatch Attribution Window
Usage is tracked by discrete sequence intervals: `(start_sequence, end_sequence]`.
- An active window starts when a goal is created or resumed: `start_sequence = through_sequence`.
- The window closes when the goal is paused, completed, or blocked: `end_sequence = through_sequence`.
- Dispatches issued before goal creation or during a pause are strictly excluded.
- In-flight dispatches arriving after a pause are attributed to the closed window in which they were dispatched.

### Hard Core Budget Constraint
- When active and budgeted, the plugin stages a core constraint:
  `services.set_budget(since_sequence=window_start, max_total_tokens=window_allowance)`
- Only **create** and **resume** open a new window. An explicit budget edit on a live goal
  keeps the current open window and its baseline, and re-stages the constraint for that same
  baseline with the allowance `new_budget - spend`, where spend is the truthful token spend
  of the closed windows reported by core usage (windowed `get_usage`).
- **Unknown spend is never treated as zero.** If any closed window has unreported usage, a
  finite budget raise or a budgeted resume is refused with a visible error; the user either
  uncaps (`token_budget = null`) or creates a new goal. No fresh allowance is invented.
- Budget enforcement is **window-scoped** (`window_tokens_used` vs `window_allowance`),
  while `tokens_used` remains the lifetime cumulative:
  - When window usage reaches the allowance → `budget-limited`.
  - When the window's billable metric is unreported (`window_tokens_used is None`) → `usage-limited`.
  - In both cases the plugin cancels **its own** automatic queue and **retains** the core
    budget constraint; it never uncaps an in-flight user budget.
  - Late bills for a **closed** window: while the goal is active they tighten the current
    allowance against the unchanged baseline; while paused they update lifetime counters
    only (no re-arm, no global block).
- Pause policy (`goal.pause`, and `user_stop` from a run) closes the open window at the
  actual stop sequence, clears the owner's core constraint and cancels its queue.
  `goal.pause` is a valid escape from `active`, `blocked`, `usage-limited` and
  `budget-limited`; an unbudgeted `goal.resume` also clears any stale constraint.
- Truthful metric accounting:
  - Unreported dispatch (`usage is None`) sets `tokens_used`, `window_tokens_used`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `uncached_input_tokens` to `None` — never `0` — while still accounting any reported `elapsed_seconds`/`cost_usd`.
  - Once a metric total is unknown it stays unknown; a later reported value cannot silently rewrite it to a partial number.
  - Unknown cost does not poison known billable tokens.
  - Measured work time accumulates from `UsageFact.elapsed_seconds`; `RunFinishedFact` neither fabricates a `0` elapsed nor double counts the same work.

---

## 4. Objective Spilling & Containment

Objectives exceeding `MAX_MODEL_OBJECTIVE_CHARS` (4000 characters) are persisted to workspace attachments:
- **Location**: `.voidcode/attachments/<uuid>/goal-objective.md`
- **Creation**: Exclusive write mode (`xb`) ensuring no accidental overwrites.
- **Containment**: Target path must strictly resolve inside `workspace.resolve()`; path traversal (`../`) is rejected.
- **Integrity**: Full SHA256 digest recorded in state. Missing or modified files trigger visible errors upon reading.
- **Immutability**: Attachment files represent immutable historical references and are never deleted or truncated on replacement or clear.

---

## 5. 3-Turn Genuine Block Discipline

A model invocation of `update_goal(status="blocked")` enforces an objective impasse barrier:
1. **Turn Deduplication**: Block requests are counted at most once per distinct `context.logical_turn`. Repeated native batch calls or process restarts within the same logical turn do not increment the counter.
2. **Intentional Failure Proposals**: For attempts 1 and 2, `update_goal` returns `ToolFailure` but intentionally stages the incremented `blocked_turns_count` and `last_blocked_logical_turn` into session state. Core commits this staged proposal.
3. **Turn 3 Acceptance**: On the third distinct consecutive blocked turn, the transition to `blocked` is permitted.
4. **Reset Conditions**:
   - Meaningful progress: any non-bookkeeping tool (`read`, `write`, `bash`, etc.) succeeding (`InvocationFinishedFact(status="ok")`).
   - Clean turn completion: `RunFinishedFact(status="completed")` where no blocked attempt was made in that turn.
   - User interaction: explicit `goal.resume` or objective edit.

---

## 5b. Prompt parity

The model-facing text is the legacy goal prompt, migrated verbatim from `runtime/goal.py`
(no strategy edits):

- Active continuation = legacy `continuation_prompt`: cross-turn persistence, "do not shrink
  the objective / do not redefine success", rough edges vs verified end state, "work from
  evidence" (worktree/external state authoritative), completion initially unproven covering
  every requirement with no remaining work and an explicit `update_goal status="complete"`,
  and the four-point blocked audit (not on the first blocker, three consecutive turns, true
  impasse, never because work is hard/slow/uncertain/incomplete or would benefit from
  clarification). Section order and the `<objective>` marker are unchanged.
- Budget limit = legacy `budget_limit_prompt` (same order, `<objective>` marker).
- Objective edited = legacy `objective_updated_prompt` with `<untrusted_objective>`: the new
  objective supersedes the old one and old-only work must not continue. It is **prepended**
  to the legacy continuation (never replacing it), so persistence/completion/blocked audits
  stay in every active context.

  Delivery lifecycle: `goal.edit` sets the factual bit `objective_edited` when the objective
  actually changes and it is **retained** until the goal is replaced or cleared — a run
  finishing never clears it, so a dispatch that was already in flight with the old context
  cannot swallow the notice. Consequence (deliberate ceiling): the notice repeats in later
  contexts until the goal changes again. One-shot delivery would need dispatch-bound context
  receipts, which this plugin does not have; repeating is preferred over silently losing the
  user's edit. `# ponytail:` in the renderer names the same ceiling.
- Adaptations are limited to typed state: nullable counters render as `unknown` (never `0`,
  and never `unbounded` when a finite remainder is merely unknown), absent budget renders as
  `none`, unbounded budget renders as `unbounded`, and the new `usage-limited`/`blocked`
  statuses get a truthful notice using the same markers.
- Tool descriptions (`get_goal`, `create_goal`, `update_goal` and their property descriptions)
  are the legacy `tools/goal.py` text, declared once in `goal_plugin.py`.
- Delivery role: the prompt is contributed in the **legacy user role** —
  `ContextContribution(content=..., role="user")` — so the provider receives the exact text as a
  user message and the core never re-wraps it as `[goal context]` system prose. That matches
  the legacy delivery, where the TUI submitted `continuation_prompt` as the user's prompt and
  the runtime merged `budget_limit_prompt` into the request prompt as steering. The durable
  `pursue_goal` continuation stays an internal system trigger; the text itself always travels
  through the context contribution, so a continuation-driven turn still sees it exactly once.

## 6. Commands

| Command | Effects | Description |
|---|---|---|
| `goal` | `read` | Read-only inspection of active goal state and remaining budget. |
| `goal.set` | `write`, `session` | Create a goal or replace an existing goal. Unfinished replacement returns `pending_confirmation`, then an accepted confirm executes the replacement once. |
| `goal.edit` | `write`, `session` | Edit objective or adjust the token budget. A finite raise requires known closed-window spend (otherwise a visible error); `token_budget=null` explicitly uncaps. |
| `goal.pause` | `session` | Pause the goal (valid from active/blocked/usage-limited/budget-limited): close the window at the current tip, clear the owner's constraint, cancel its queue. |
| `goal.resume` | `session` | Resume a paused/blocked/limited goal: open a new window after closing the old one, re-arm the allowance from known closed-window spend (refuses when spend is unknown; unbudgeted resume clears any stale constraint), enqueue a continuation. |
| `goal.clear` | `session` | Explicit direct clear of goal state, budget, and continuations without extra confirmation. |
| `goal.load_objective`| `read` | READ action returning the full objective text exactly once in `data.objective` (no `output` duplication) for large goals. |

### Objective transport rules
- The full objective crosses the wire exactly once, through `goal.load_objective`'s
  `data.objective`. `goal.edit`'s empty query returns only a bounded form descriptor
  (`initial_values`, `load_action`) plus goal state — never the full text.
- Single-carry rule applies to the **spilled full objective**: it is always fetched through
  the READ loader and is never duplicated in a view payload. A raw UTF-8 byte threshold is
  not a JSON-size guarantee because Unicode and control characters expand under JSON
  escaping. An **inline** objective (bounded by `MAX_MODEL_OBJECTIVE_CHARS`) may be prefilled
  in the form.
- The plugin adds **no goal-specific size cap** and never truncates the objective. The
  platform bounds still apply: the SDK's 1 MiB JSONL frame and the 64 KiB view payload
  limit. A payload that exceeds them fails visibly instead of being silently cut.

---

## 6b. Owner deactivation (disable / re-enable)

Disabling the plugin is a capability activation (`core.reload` with the owner disabled in the
workspace config). The core runs the plugin's pure `Plugin.prepare_deactivate` transform in the
**old** runtime — the one that still holds this schema — and writes the returned copy in the same
transaction that cancels the owner's queued claims and clears its session budgets. The transform
gets no service, lease, store or clock, so every field in the copy is derived from the payload.

`prepare_goal_deactivation(state, scope, scope_key, tip_sequence)`:
- Enforces the scope/tip contract before decoding anything: session scope needs the owning
  session's non-negative integer tip, workspace scope needs none. Decoding then uses the same
  strict `GoalState` parser. A malformed scope, tip or payload raises (the activation fails
  visibly with `state_deactivation_failed`) rather than resetting, coercing or partially pausing
  the goal. An empty payload returns empty: a disable never invents a goal.
- Closes every open window at `tip_sequence`, the owning session's authentic
  `last_event_sequence`. A workspace-scope row carries no session tip and is refused while a
  window is open, and a tip that precedes an open window's start is refused too, so a stale or
  corrupt tip can never write an inverted range. Already-closed windows are copied unchanged.
- Turns `active`, `blocked`, `usage-limited` and `budget-limited` into `paused`; `complete`
  stays `complete`.
- Rewrites nothing else: `goal_id`, `objective` (and its spill ref/digest, `objective_edited`),
  `created_at`, `updated_at`, the lifetime counters, the budget fields, the unknown-usage flag
  and `schema_version` all survive unchanged. The transform has no clock, so it never invents a
  new `updated_at`.
- Declares no `state_schema_version` bump and no `migrate_state`: the paused copy is the same
  version's shape, so a disable/re-enable cycle needs no identity migration.

Consequences after re-enable: a `paused` goal contributes no context
(`goal_context` returns `None`), the observer neither re-arms attribution nor
stages a budget for a paused goal, and the core never replays cancelled
continuations. Only the explicit `goal.resume` re-opens a window and stages the
core constraint for it in the same commit, so no unbilled window exists after a
re-enable. Identity, creation time, lifetime usage and budget survive the whole cycle.

## 6c. Verified evidence

Checks are the commands themselves (run from the repository root):

```
uv run pytest tests/unit/plugins/test_goal_plugin.py tests/integration/test_goal_plugin.py -q
uv run ruff check src/voidcode/plugins/goal tests/unit/plugins/test_goal_plugin.py tests/integration/test_goal_plugin.py
uv run ty check src/voidcode/plugins/goal
```

Exercised on the current tree (`goal_plugin.py a1c98c41…`, unit tests
`4df18502…`, integration tests `1e93226b…`):

- 22 unit cases pass (the 19 P2 behaviour cases plus 3 deactivation cases: pause-at-tip with
  full field preservation, live-status/complete/empty-state semantics including the
  tip-equals-window-start boundary, and malformed scope/tip/state plus inverted-window refusal).
- The prompt reaches the provider verbatim in the legacy user role on the typed context ABI:
  `test_goal_set_on_idle_session_starts_automatic_work` drives a session with no user prompt and
  asserts the first dispatch carries the objective inside a user message while no message uses
  the `[goal context]` system framing.
- All 10 integration cases pass. Two of them are the P3 deactivation cases:
  - `test_deactivation_pure_transform_pauses_live_goal_in_the_runtime`: the actual plugin runtime
    returns the paused copy for an active goal, closes the window at the session's own
    `last_event_sequence`, leaves the durable row untouched, contacts no provider, and refuses a
    workspace-scope tip, a missing session tip and a tip behind the window start without writing
    anything.
  - `test_same_session_disable_and_reenable_keeps_paused_goal`: a real `core.reload` with the
    owner disabled on the SAME session reports `deactivated_owners == ('goal',)`, and the durable
    row is the paused copy with the window closed at the session's own event tip, identity,
    `created_at`, `updated_at`, budget and counters preserved, and the owner budget cleared.
    Disabled, the goal command is unknown and ordinary chat completes with exactly one billed
    turn carrying no goal context and charging the goal nothing. Re-enabled, the row is still
    paused and nothing dispatches until `goal.resume`, which arms the cap in its own commit.
    counters charge that new window only (25, never the ordinary 18), identity, `created_at` and
    flight, before any byte of the reply is written, and observes none for the ordinary turn
    (disabled owner) and exactly the resume-armed cap for the first resumed dispatch
    (`since_sequence` = the new window baseline, `max_total_tokens` = 10). That dispatch bills 25
    tokens, the observer stages `goal_budget_limited` against the same allowance, the goal's
    counters charge that new window only (25, never the ordinary 18), identity, `created_at` and
    budget survive, and the model's own `update_goal` finishes the goal in the same run without
    the concluding provider turn an uncapped run would take.
- `ruff check` and `ty check` pass.

P2 records, made against the P2 frozen stack (`goal_plugin.py 4ee8a848…`, with
`core/engine.py 95e72163…`, `core/storage/store.py 8947131f…`, SDK directory
input `b15172ef…`):

- 27 behaviour cases passed (19 unit + 8 integration).
- Live `deepseek/deepseek-flash` idle `goal.set` run (no user prompt, no manual drain): the
  goal context drove read `17 25` → write `42` → read back → explicit complete; goal
  `complete`, tokens 7331, measured elapsed 6.356 s, `cost_usd` remained `None`; a fresh
  core over the same state showed the identical complete goal with no further dispatch.
- Startup acceptance: with the plugin config-disabled, ordinary base chat completed and the
  provider request carried no goal context; restoring the exact config reopened the original
  session with the goal intact, no unintended provider work and no pending continuation.

## 7. Declarative Views

The plugin registers bounded declarative views implementing `PluginView`:
1. `goal` (`view_type="status"`): Concise session status banner with goal status, truncated preview, and token count.
2. `goal.details` (`view_type="fields"`): Structured table rows displaying Status, Objective, Budget, Tokens Used, and Elapsed Time.
3. `goal.form` (`view_type="form"`): Form presentation bound to the current state —
   with no goal it is a **create** form (`action="goal.set"`, title "Set Goal"); with an
   existing goal it is an **edit** form (`action="goal.edit"`, title "Edit Goal") so a
   consumer edit preserves identity, creation time, charged usage and budget. Inline
   objectives (bounded by `MAX_MODEL_OBJECTIVE_CHARS`) are prefilled; spilled objectives
   are never prefilled and always carry `load_action="goal.load_objective"`, because JSON
   escaping can push a raw-byte-bounded payload past the view frame limit.
