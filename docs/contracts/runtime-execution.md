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
