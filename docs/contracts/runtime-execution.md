# Runtime Execution Contract

This document defines the lifecycle, execution boundaries, and persistence invariants of the VoidCode runtime.

## Execution Flow

Execution in VoidCode routes through an explicit boundary separating governance from turn progression:

```text
RuntimeRequest
      │
      ▼
VoidCodeRuntime.run_stream() (src/voidcode/runtime/service.py)
      │
      ▼
RuntimeRunLoopCoordinator.execute_turn_engine() (src/voidcode/runtime/run_loop.py)
      │
      ├───────────────────────────────┐
      ▼                               ▼
Core TurnEngine (core/engine.py)    RuntimeHost (runtime/run_loop.py)
  - Advances turn batches             - Evaluates permissions & approval mode
  - Requests provider completions     - Dispatches tools via registry
  - Yields streaming chunks           - Emits runtime events
                                      - Appends facts to SQLite session_events
```

1. **Request Ingress**: `VoidCodeRuntime.run_stream()` receives a `RuntimeRequest` (carrying prompt, optional session IDs, and request metadata); the runtime control plane resolves the workspace context and runtime configuration when preparing the run.
2. **Turn Progression**: `TurnEngine` advances the conversation loop through neutral turn producers. It yields model text deltas and packages tool requests into turn plans (`TurnPlan`).
3. **Governed Tool Execution**: `RuntimeHost.execute()` checks permissions for each requested tool. Read-only operations execute directly; state-modifying tools pass through approval barriers based on the active `approval_mode`.
4. **Fact Commitment**: Completed tool results (`ReportedCall`) and model turns are committed to SQLite `session_events` with monotonic sequence numbers.

## Session Lifecycle & Status Invariants

Session state is tracked in the `sessions` table and defined by `SessionStatus` in `src/voidcode/runtime/session.py`:

| Status | Category | Description |
|---|---|---|
| `idle` | Active | Initial or unstarted session state. |
| `running` | Active | Turn engine or tool execution is actively in flight. |
| `waiting` | Active (Paused) | Execution is paused waiting for user input (`approval` or `question`). Remains in `waiting` until explicitly answered or resumed. |
| `completed` | Terminal | Session finished its plan successfully; sealed against late writes. |
| `failed` | Terminal | Terminal failure encountered; sealed against late writes. |
| `interrupted` | Terminal | Execution ended without a terminal success seal (such as user cancellation, process signal, or concurrent session overlap). Sealed against late events from the dead run; resumable via explicit re-entry. Tool-level timeouts do not interrupt the whole session. |

### Statuses vs. Run Outcomes
Session status represents the persistent state of the session row in SQLite, while cancellation, error, and timeout reasons are run outcomes returned in the execution response:
- When a whole run is cancelled or terminated prior to completion (such as via user cancellation, process signal, or concurrent session overlap), the session row transitions to `interrupted`.
- When an individual tool execution encounters a tool-provided timeout (such as `shell_exec(..., timeout=N)`), the tool returns per-call error feedback (`runtime.tool_completed` with error status); that timeout alone does not make the session `interrupted`, and execution can continue.
- When the runtime's configured `tool_timeout_seconds` is exceeded, the runtime emits `runtime.tool_timeout` and terminates the run as `failed` (not `interrupted`).

## Background Execution Leases

Background tasks executed under `RuntimeBackgroundTaskSupervisor` (`src/voidcode/runtime/background/supervisor.py`) acquire an execution lease from `EXECUTION_OWNERSHIP` (`src/voidcode/runtime/execution_ownership.py`):

- **Lease Binding**: The lease is bound to the worker thread executing the delegated turn.
- **Revocation on Teardown**: When the runtime drains tasks during shutdown, workers that do not complete before the timeout have their lease revoked.
- **Storage Gate Enforcement**: The SQLite storage gateway asserts write eligibility via `EXECUTION_OWNERSHIP.assert_writes_allowed()`. Writes from revoked leases are blocked (`ExecutionOwnershipRevokedError`), preventing late writes from polluting persisted session truth.

## Checkpoint & Resume Invariants

When resuming an interrupted session:

1. **Authentic Completed Prefix**:
   The runtime restores completed tool call results that were durably recorded in the session checkpoint (`tool_batch_id`, `completed_call_ids`). `replay_policy` is distinct from execution status: any genuine completed result—even for a tool declared with `replay_policy="never"`—remains part of the authentic completed prefix and is preserved on recovery with its original batch and call IDs.
2. **Pending Intent Handling (`replay_policy`)**:
   - Tool intents with `status="pending"` and `replay_policy="safe"` (such as read-only inspection queries) may be replayed from the recorded intent upon resume.
   - Tool intents with `status="pending"` and `replay_policy="never"` (such as state-modifying actions that were in-flight when interrupted) are surfaced as `interrupted` and are never auto-replayed. Recovery restores safe checkpoint state up to the completed prefix and requests a fresh turn from the provider model.
3. **Value Fidelity**:
   Unobserved or absent metrics remain `None`; they are never coerced to synthetic defaults (`0` or `""`).

Provider replay derives assistant responses from `core.response_ready` facts on the selected root-to-leaf path, not the row's latest output. Native manual compaction cuts that path at its own event position; automatic compaction does not discard conversation prefixes. Fork and checkout rebuild native summary metadata from the selected path so abandoned-branch summaries cannot enter provider context.

## Policy Snapshot & Read-Only Governance

Runtime policy resolution enforces immutable security boundaries across turns and session delegations:

1. **Policy Snapshot Authority**:
   The session's frozen policy snapshot (`runtime_policy`) is the authoritative source of truth. Once a session is initialized with `read_only: true`, that stance remains frozen across all subsequent turns. Subsequent follow-up requests cannot weaken or relax the frozen policy, even if request metadata omits `read_only` or explicitly sets `read_only: false`.
2. **Turn-Level Narrowing**:
   A writable session (`read_only: false`) may narrow permissions for an individual turn by providing `metadata={"read_only": True}` on that turn's request. This restricts tool visibility, tool execution, and lifecycle hooks for that turn without corrupting the session's writable snapshot for subsequent turns.
3. **Child Delegation Inheritance**:
   Child sessions inherit the read-only restriction of their parent session's policy snapshot. A child session spawned from a read-only parent cannot expand capabilities or execute mutating actions (`read_only` resolves to `true` in both child snapshot and metadata).
4. **Visibility & Execution Gate Parity (Base vs. Effective Gate)**:
   Base snapshot construction preserves immutable session policy authority via `runtime_read_only_from_metadata`. Runtime tool visibility (`RuntimeToolScopeResolver`), execution authorization (`service.tool_policy_denial`, `resolve_permission`), hook execution policy (`HookExecutionPolicy`), and child delegation inheritance share the authoritative effective gate (`runtime_effective_read_only_from_metadata`), which combines base read-only with dynamic collaboration mode overlays. Mutating tools are filtered out of model-visible schemas, and any direct or unadvertised invocation attempts are denied (`read-only access is active; mutating tools are denied`).
5. **Lifecycle & Tool Hook Protection**:
   Read-only enforcement applies to hook execution (`HookExecutionPolicy`). Any mutating or command hook is skipped under a read-only stance to ensure hooks cannot mutate external or workspace state outside the governed tool boundary.

## Native Collaboration Mode (`/plan`)

The runtime provides native reversible planning collaboration state:

1. **Mode Vocabulary & Reversibility**:
   Modes are `"default"` and `"plan"`. Entering plan mode (`/plan` or `set_collaboration_mode(session_id=..., mode="plan")`) overlays a strict read-only restriction for exploration and planning. Exiting (`/plan off` or `mode="default"`) lifts the mode overlay and restores write capabilities for writable sessions.
2. **Effective vs. Base Read-Only Split**:
   The session's base policy snapshot (`runtime_policy.read_only`) remains immutable and is never mutated to implement plan mode. The effective read-only gate evaluates `base_read_only OR collaboration_mode == "plan"`. Sessions initially created as read-only remain read-only when exiting plan mode.
3. **Durable Persistence & CAS Integrity**:
   Mode transitions append an immutable control event (`runtime.collaboration_mode_changed`) and update session metadata and resume checkpoints atomically in SQLite using compare-and-swap (CAS). Mode transitions are idle-only and reject active running or waiting sessions without partial state writes.
4. **Context Injection**:
   When collaboration mode is `"plan"`, provider context assembly injects authoritative concise plan mode instructions into the prompt assembly plan without altering historical prompt transcripts.
5. **Child Inheritance**:
   Subagent and child sessions inherit the effective read-only restriction of a parent session in plan mode, preventing any mutation bypass through delegation.

## Native Governed Review (`/review`)

The runtime provides governed, read-only review over uncommitted aggregate workspace changes:

1. **Governed Child Review Session**:
   Executing `/review` or `review_session(session_id=..., focus=...)` dispatches a governed read-only child session under the specified parent session (`parent_session_id`). The child session is allocated with the child-only `review` agent preset and requires a terminal invocation of the `yield` tool to return structured findings.
2. **Aggregate Diff Capture & Bounding**:
   Review operates over the uncommitted aggregate workspace diff (including staged, unstaged, and untracked changes). If the workspace is not a Git repository or has no changes (`diff.state == "clean"`), the call rejects immediately with an informative error without dispatching a model turn. Diffs exceeding 262,144 bytes (>256 KiB) are rejected with an explicit actionable error rather than being silently truncated, ensuring review findings never claim full coverage over an incomplete diff.
3. **Parent State Preservation & Concurrency**:
   The parent session must exist and be idle (`ACTIVE_SESSION_REGISTRY.active_run_count == 0` and `status not in ("running", "waiting")`). The child review execution does not overwrite the parent session's output, status, or primary turn history.
