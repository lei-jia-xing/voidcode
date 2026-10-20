# Agent Collaboration Contract

Delegated agent work is kernel-owned: a parent session starts child sessions, steers them, and reads their durable outcomes through SDK services. The bundled `task`/`task_batch` tools (Delegation plugin) are the model-facing surface; `voidcode://transcript/<child>` and `read` expose the readable transcript. No plugin owns child lifecycle state, and no caller can widen scope, stance, or budget.

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
- The model acknowledges the run it actually consumed through `acknowledge_child_completion`. The acknowledgement is staged on the invocation lease and commits atomically with the successful tool result carrying the outcome: a crash cannot acknowledge a result the model never saw, and a notice already claimed or completed is untouched.
- Restart recovery republishes a terminal child run whose parent fact is missing, rebuilding status/output/error/stop cause/label/history from durable events. It publishes exactly once and delivers the notice; child work is never rerun.

## Transcript and history paging

- The readable renderer emits user messages, assistant turns with their tool calls, tool results, child facts, progress, and plugin events; kernel bookkeeping is omitted. Child payloads are included (`include_payload=True`) so a transcript is readable, never a payload-stripped skeleton.
- Live history paging uses a byte cursor: request `after_sequence` plus `history_offset`, then continue from `next_after_sequence` and `next_event_offset`. One page is bounded by `PAGE_BYTES` (40 KiB); a single block larger than a page is split on a UTF-8 boundary, never clipped. Output paging uses `output_sha256` plus `output_offset` (default 8 KiB pages) and does not depend on the child still running.
- Pages are sized to stay well under the kernel's 256 KiB context hard bound; a consumer pages instead of requesting the whole transcript.

## Open risk: concurrent control-event checkpoint race

An earlier smoke run that spawned a new child on every parent provider turn (many concurrent
publications) failed once with `checkpoint references the wrong event kind`. Three subsequent
runs with the corrected script passed and it has not reproduced. Concurrent child control events
appended to a parent `_commit` path are covered by rebased commits, but this exact interleaving
remains unproven: it is a known residual risk, not a fixed defect or a verified guarantee.
