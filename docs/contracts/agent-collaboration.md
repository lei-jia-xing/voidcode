# Agent Collaboration Contract

Delegated agent work is core-owned: a parent session starts child sessions, steers them, and reads their durable outcomes through SDK services. The bundled `task`/`task_batch` tools (Subagents plugin) are the model-facing surface — their operations, arguments, profiles, and narrowing policy are canonical in the [subagents](../../src/voidcode/plugins/subagents/README.md) README; this document covers only the core-owned facts, services, and invariants underneath. `transcript://<child>` and `read` expose the readable transcript. No plugin owns child lifecycle state, and no caller can widen scope, stance, or budget.

## Durable facts

- A child is a normal session with `parent_session_id`, `root_session_id`, and `depth`; only the direct parent may `join_child`, `send_child`, or `cancel_child`. The run-bound subtree cancellation below also permits ancestors within their own descendant tree. Depth, the configured chain child quota (`policy.max_root_children`), and the parent's visible tool scope bound every spawn; a requested allowlist may only narrow.
- `core.child_spawned` records the linkage, label, effective allowlist, and `notify_on_completion`. The notify flag is persisted in the fact so a restart reconstructs the request from the durable log, never from a thread-local argument.
- `core.child_finished` is the terminal fact for exactly one `(child_session_id, run_id)`: status, error, stop cause, label, optional typed `budget_exhaustion` from that run's committed result, output (inline under `CHILD_INLINE_OUTPUT_BYTES`, otherwise a bounded preview plus `output_sha256`/`output_bytes` reachable through the artifact page API), and the immutable history snapshot (`sha256`, `bytes`).
- Waiting on an authentic approval checkpoint or recoverable interruption is parked, not terminal. A public resume starts subsequent work; only an actual terminal result or public cancellation publishes a terminal child fact.
- Child birth atomically captures its originating action's root id and optional finite authorization. Queued child sends and wakeups likewise capture that origin when registered; drivers never borrow a parent's later checkpoint. A local optional `max_steps` limits provider dispatches only. `None` does not refill captured finite root authorization, and a child cannot create a new root through `Core.run`.
- Interrupted is not synonymous with cancelled: only committed `user_stop`/`owner_cancel` maps to `ChildOutcome.status="cancelled"`. Budget, shutdown, error, or unknown stops remain interrupted with their actual or unknown `stop_cause`.
- `ChildOutcome.budget_exhaustion` is optional typed `BudgetExhaustion` data. Core reads a recoverable budget pause from that exact child's committed `RUN_INTERRUPTED` payload, never from current root counters; the pause remains `interrupted` and has no `CHILD_FINISHED` fact or completion notice. The tested public resume starts a distinct run id, and its later terminal fact carries only the outcome committed for that new run.

## Join and steering

- `join_child` waits for the child's CURRENT run. It is never satisfied by an older terminal fact: outcomes are remembered per run id, and a follow-up run owns a new run id.
- `send_child` appends one attributed follow-up in the child's own session. A running or approval-parked child queues it; a child without outstanding work starts a new run immediately. The receipt (`started`/`queued`, status, optional run/message id) never claims consumption or completion. A running parent merges claimed messages into the SAME run at a committed batch boundary, never across an in-flight tool call, approval checkpoint, or token stream.
- Authorization follows the current stance: only the direct parent may send, and a parent that is read-only may not steer a child that is not durably read-only. Scope and stance can never widen across the lineage.

## Subagent monitor

- `PluginServices.list_descendants()` returns typed `DescendantNode` snapshots of the bound session's whole descendant subtree, including children owned by other plugins. The bound session itself, its ancestors, siblings, and foreign roots are excluded. Status, run/stop cause, label, activity, last committed sequence, queued follow-up, and approval flags come from durable state; absence remains `None`. An unstarted node has no real run id, including after its reserved cancellation.
- `subagents.monitor` renders this tree read-only through `command.input` only: a refreshing empty-or-populated choices roster plus markdown detail pages with Back/Cancel/Next/Refresh. Detail history pages use `read_session_history` with an 8 KiB `page_bytes` budget and authoritative cursors; browsing creates no command events, no receipts, and no acknowledgement.
- `PluginServices.cancel_descendant_subtree(target_session_id, expected_run_id=...)` is the mutation behind `subagents.monitor.cancel` (SESSION, confirmed). The expected run is required; explicit null binds a node that has never started a real run. Initial stale confirmation is refused before mutation. Each node signals only its census-observed run through the run-bound abort registry, or uses a pinned parked settle that cannot rebase onto a newer run.
- A counted, temporary subtree fence covers admissions during cancellation. Store transactions check it in store-to-lifecycle lock order; cancellation never reads the store while holding the lifecycle lock or waits for a provider under that lock. Admissions committed before the fence appear in the subsequent census. Spawn/send transactions also check the originating invocation's captured cancellation signal and lease, so an old call cannot register work after the fence has returned.
- The receipt distinguishes accepted signals (`cancelled`), disarmed queued work (`disarmed_queued`), unstarted claims (`claimed_reserved`), already-terminal nodes, and `observed_newer_run`. Disarming and parked settlement are run-bound; newer work is left untouched. An explicit follow-up after cancellation remains legal. No permanent tombstone or workspace-wide lineage cache is kept.


## Read-only agent result resource

- The Subagents plugin registers `agent://<child_session_id>` as a current-status/latest-output view, not as an alias for `transcript://`. Task receipts expose `agent_uri` separately from the existing live transcript `history_uri`; the saved history snapshot remains an immutable `artifact://` resource.
- Core `ResourceReaderServices.read_child_snapshot` returns one caller-visible descendant's current status/run/stop cause plus the latest eligible committed output without joining, acknowledging, consuming notices, or otherwise changing child work. Missing and non-visible ids both return `None`; parent, sibling, and unrelated sessions are outside the descendant scope.
- Current lifecycle facts and published-output facts stay separate. Core selects the newest `CHILD_FINISHED` fact with `status="completed"` and nonempty inline output or a full output artifact; failed, interrupted, cancelled, empty, and preview-only facts do not erase the prior result. A prior output is marked superseded only while a distinct newer run is active; the current run id/status remain visible even when its result is empty or unavailable.
- `agent://` pages every eligible publication through the shared immutable artifact pager. Core materializes committed inline output as a deduplicated text artifact during the caller-scoped snapshot; this does not rewrite the child fact or alter join results. Ordinary line and UTF-8 byte selectors apply, and continuations use the immutable `artifact://` reference. Metadata remains available in the canonical body without a result-size rejection. The root accepts no path/query suffix: `/key/index` remains unavailable until a typed structured child-publication contract exists.
- VoidCode has no separate accepted-yield marker, so the nonempty-completed-output rule is an explicit approximation, not a claim of exact OMP yield semantics. Disabling the Subagents provider removes `agent://` with no fallback.

## Completion notices and acknowledgement

- With `notify_on_completion`, the child's terminal publication stages one bounded notice continuation in the same transaction as `core.child_finished`, keyed by `(parent_session_id, child_session_id, run_id)`. An idle parent is woken; a busy parent merges the notice at its next safe boundary; a deeper or foreign session never receives it. Automatic delivery leaves notices pending on an interrupted parent instead of reviving cancelled work, including after restart. A later explicit follow-up or drain remains legal.
- The model acknowledges the run it actually consumed through `acknowledge_child_completion`. The acknowledgement is staged on the invocation lease and commits atomically with the successful tool result carrying the outcome: a crash cannot acknowledge an outcome without durably committing the consuming tool result, and a notice already claimed or completed is untouched. This guarantees durable consumption, not provider/model receipt of the notice.
- Restart recovery republishes every terminal child run whose parent fact is missing (including an older run beside a newer follow-up), rebuilding status/output/error/stop cause/label/history from durable events. It publishes each exactly once and delivers the run-scoped notice; child work is never rerun.

## Transcript and history paging

- The readable renderer emits user messages, assistant turns with their tool calls, tool results, child facts, progress, and plugin events; core bookkeeping is omitted. Child payloads are included (`include_payload=True`) so a transcript is readable, never a payload-stripped skeleton.
- Live history paging uses a byte cursor: request `after_sequence` plus `history_offset`, then continue from `next_after_sequence` and `next_event_offset`. One page is bounded by `PAGE_BYTES` (40 KiB); a single block larger than a page is split on a UTF-8 boundary, never clipped. Output paging uses `output_sha256` plus `output_offset` (default 8 KiB pages) and does not depend on the child still running.
- Page bounds are caller/storage policies, not proof of a provider context limit. The obsolete global 256 KiB serialized-message reject is removed; adaptive active-model token/image headroom is still an explicit SDK/Core gap.

## Concurrency and recovery invariants

These are the core/store invariants enforced by the implementation, exercised by deterministic
real-core schedules and the collaboration regression suite. They are not a claim of exhaustive
scheduling coverage.

- **Execution prefix.** `execution.events` is always one unique contiguous committed prefix whose
  length equals `execution.session.last_event_sequence`. Rebasing reads only a bounded target tip
  (the commit's returned snapshot or the post-claim snapshot), appends only the missing range, and
  fails closed on a non-contiguous or incomplete read. It never appends an unbounded store tail.
  A child-message claim reconciles the whole range from the old tip to the post-claim tip, allowing
  exactly the claimed `continuation_started` sequences; it does not advance the session over an
  intervening `core.child_finished`.
- **Sequence conflicts.** `SequenceConflict` means the atomic CAS committed nothing. The same
  payload is retried after reconciling to the fresh tip (bounded retries), including commits that
  carry staged plugin state or continuations: the failed transaction re-applies nothing twice and
  never repeats an external invocation. Run-id, revision, state-version, confirmation, and other
  conflicts are never retried. `RootBudgetExhausted` is a distinct signal and is the only conflict
  mapped to `limit_reached`; a real durable budget refusal still stops the run.
- **Join publication window.** A terminal child session whose fact/history are still being
  produced is not reported as a completed outcome. Join waits for the publisher; on its deadline
  (including `timeout_seconds=0`) it returns the existing nonterminal outcome
  (`status="running"`, current run id, no output). Once published, join returns the full fact.
  After restart, join loads the durable `core.child_finished` fact for the child's CURRENT run;
  it never substitutes an older run's fact. A terminal cancellation's durable fact (error and
  history) is likewise preferred over the bare status outcome.
- **Per-run history.** A saved snapshot is the session prefix through THAT run's genuine terminal
  event (`run_completed`, `run_failed`, or terminal `run_interrupted` with user/owner stop): all
  prior session history is included, a later follow-up's events are excluded even when the earlier
  publication is delayed, and late same-run control/progress rows do not extend the cutoff. If the
  authentic terminal event is missing, the fact records a `history_error` and omits the snapshot
  instead of saving a misleading whole-session transcript.
- **Out-of-order recovery.** Publication idempotency scans all parent facts for the requested
  `(child, run)` instead of stopping at the first other-run fact. Startup recovery enumerates every
  terminal run in a child session's durable events (`run_completed`, `run_failed`, terminal
  `run_interrupted` with `user_stop`/`owner_cancel`), publishes each missing run exactly once, and
  never replays child model/tool work. Recovery performs a complete session scan; ordinary callers
  keep their bounded default. Waiting and recoverable interrupted runs are not terminal and are
  skipped.
- **One started run per session.** `claim_and_start_continuation` is one atomic transaction: it
  refuses a session that is running/waiting, already holds claimed work, has a nonterminal/parked
  checkpoint, or is an idle spawned child whose first run has not committed. A loser stays
  durable and `queued`; its receipt names no run. A `started` receipt names the run id the store
  actually claimed. Recovering a claimed row requires the checkpoint to name that exact run and
  continuation; a mismatch fails the row visibly instead of driving another row's checkpoint.
  Terminal-interrupted sessions with a terminal checkpoint still admit a fresh explicit claim.
- **Cancel/send linearization.** Cancellation publishes only the run id observed when it began,
  never a newer run claimed by a concurrent send. There is intentionally no permanent
  cancellation fence or tombstone: a send that linearizes after the pending sweep may start new
  work, and an explicit follow-up after an accepted cancellation is legal. A send that was
  accepted before the sweep may be cancelled with the pending queue.
- **Atomic spawn budget.** The chain child count and child-session insert happen in one store
  transaction. Concurrent spawns at the limit accept at most one child; rejected attempts create
  no session, no parent fact, and no provider work.
