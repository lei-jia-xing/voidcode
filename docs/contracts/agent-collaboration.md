# Agent Collaboration Contract

Delegated agent work is core-owned: a parent session starts child sessions, steers them, and reads their durable outcomes through SDK services. The bundled `task`/`task_batch` tools (Delegation plugin) are the model-facing surface; `voidcode://transcript/<child>` and `read` expose the readable transcript. No plugin owns child lifecycle state, and no caller can widen scope, stance, or budget.

## Durable facts

- A child is a normal session with `parent_session_id`, `root_session_id`, and `depth`; only the direct parent may join, send, or cancel it. Depth (`DELEGATION_MAX_DEPTH`), the chain spawn budget (`DELEGATION_SPAWN_BUDGET`), and the parent's visible tool scope bound every spawn; a requested allowlist may only narrow.
- `kernel.child_spawned` records the linkage, label, effective allowlist, and `notify_on_completion`. The notify flag is persisted in the fact so a restart reconstructs the request from the durable log, never from a thread-local argument.
- `kernel.child_finished` is the terminal fact for exactly one `(child_session_id, run_id)`: status, error, stop cause, label, output (inline under `CHILD_INLINE_OUTPUT_BYTES`, otherwise a bounded preview plus `output_sha256`/`output_bytes` reachable through the artifact page API), and the immutable history snapshot (`sha256`, `bytes`).
- Waiting on an authentic approval checkpoint and a recoverable interruption are parked, not terminal. Only a public resume or cancel publishes their terminal fact.

## Join and steering

- `join_child` waits for the child's CURRENT run. It is never satisfied by an older terminal fact: outcomes are remembered per run id, and a follow-up run owns a new run id.
- `send_child` appends one attributed follow-up in the child's own session. A running or approval-parked child queues it; a child without outstanding work starts a new run immediately. The receipt (`started`/`queued`, status, optional run/message id) never claims consumption or completion. A running parent merges claimed messages into the SAME run at a committed batch boundary, never across an in-flight tool call, approval checkpoint, or token stream.
- Authorization follows the current stance: only the direct parent may send, and a parent that is read-only may not steer a child that is not durably read-only. Scope and stance can never widen across the lineage.

## Completion notices and acknowledgement

- With `notify_on_completion`, the child's terminal publication stages one bounded notice continuation in the same transaction as `kernel.child_finished`, keyed by `(parent_session_id, child_session_id, run_id)`. An idle parent is woken; a busy parent merges the notice at its next safe boundary; a deeper or foreign session never receives it.
- The model acknowledges the run it actually consumed through `acknowledge_child_completion`. The acknowledgement is staged on the invocation lease and commits atomically with the successful tool result carrying the outcome: a crash cannot acknowledge an outcome without durably committing the consuming tool result, and a notice already claimed or completed is untouched. This guarantees durable consumption, not provider/model receipt of the notice.
- Restart recovery republishes every terminal child run whose parent fact is missing (including an older run beside a newer follow-up), rebuilding status/output/error/stop cause/label/history from durable events. It publishes each exactly once and delivers the run-scoped notice; child work is never rerun.

## Transcript and history paging

- The readable renderer emits user messages, assistant turns with their tool calls, tool results, child facts, progress, and plugin events; core bookkeeping is omitted. Child payloads are included (`include_payload=True`) so a transcript is readable, never a payload-stripped skeleton.
- Live history paging uses a byte cursor: request `after_sequence` plus `history_offset`, then continue from `next_after_sequence` and `next_event_offset`. One page is bounded by `PAGE_BYTES` (40 KiB); a single block larger than a page is split on a UTF-8 boundary, never clipped. Output paging uses `output_sha256` plus `output_offset` (default 8 KiB pages) and does not depend on the child still running.
- Pages are sized to stay well under the core's 256 KiB context hard bound; a consumer pages instead of requesting the whole transcript.

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
  intervening `kernel.child_finished`.
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
  After restart, join loads the durable `kernel.child_finished` fact for the child's CURRENT run;
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
