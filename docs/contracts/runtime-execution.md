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

## Tool-Call Concurrency (Fan-Out)

Batches of independent read-only tool calls execute concurrently under `RuntimeHost._maybe_start_fanout` (`src/voidcode/runtime/run_loop.py`); permission checks, persistence, streaming, and reporting remain serial on the main thread in batch order:

1. **Opt-In Flag (Default Serial)**:
   `ToolDefinition.supports_parallel` (`src/voidcode/tools/contracts.py`) defaults to `False`. Flagged tools: `read`, `glob`, `grep`, `lsp` (read-only operations only; see below). Unflagged tools never fan out.
2. **Hard Exclusions & Contiguous Prefix**:
   `question`, `invoke_tool`, `task`, `task_batch`, and `background_process` never fan out. `lsp` calls with `rename`, `codeAction`, or `willRenameFiles` operations never fan out. Fan-out covers only the leading contiguous run of concurrency-safe calls in `state.pending` and requires at least two; the first unsafe call stops the prefix.
3. **Pre-Flight All-Allow Gate**:
   Before submitting, every candidate is evaluated against tool policy, delegation policy, and the permission engine. Any non-`allow` decision or pending approval aborts the whole fan-out and the run proceeds serially.
4. **Main-Thread Ordered Persistence & Streaming**:
   Pool workers execute only the tool core (`_invoke_tool`); started/completed events, SQLite persistence, sequence assignment, and streaming chunks stay on the main thread via `_await_stashed_outcome` in original batch order.
5. **Bounded Pool**:
   A shared module-level `ThreadPoolExecutor` caps concurrency at 10 in-flight tool calls.
6. **First-Failure-Cancels-Rest**:
   An error outcome (raised exception or error-status `ToolFailure`) cancels all remaining stashed futures; late production after cancellation is discarded and later awaited calls observe cancellation instead of worker output.
7. **Abort Discards Late Results**:
   Run abort sets every stashed cancel signal and cancels all futures; outcomes arriving after abort never reach persistence or streaming.
8. **Checkpoint Prefix & Continuation**:
   Fan-out is disabled while a continuation (`ApprovedInvocation` / `AnsweredQuestion`) is active. Resume flows proceed serially and the authentic completed prefix is preserved unchanged.
9. **Engine Semantics Unchanged**:
   `TurnEngine` (`src/voidcode/core/engine.py`) still advances `pending[0]` serially; fan-out is a runtime-host execution detail invisible to turn planning.

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

The runtime provides governed, read-only review over selectable workspace change targets:

1. **Governed Child Review Session**:
   Executing `/review` or `review_session(session_id=..., focus=..., target=...)` dispatches a governed read-only child session under the specified parent session (`parent_session_id`). The child session is allocated with the child-only `review` agent preset and requires a terminal invocation of the `yield` tool to return structured findings.
2. **Target Selection & Diff Bounding**:
   Review operates over one of four target shapes: `uncommitted` (staged, unstaged, and untracked changes), `base_branch` (comparison against named local git branch), `commit` (specific commit SHA), or `custom` (freeform reviewer instructions). Diffs exceeding 262,144 bytes (>256 KiB, `MAX_REVIEW_DIFF_BYTES`) are rejected with an explicit actionable error rather than being silently truncated, ensuring review findings never claim full coverage over an incomplete diff. Clean workspaces or non-Git paths reject visibly.
3. **Model Resolution & Effort Fallback**:
   The review subagent resolves its model by checking `config.review_model`, falling back to the parent session model. When a dedicated review model is configured with differing reasoning effort support, `_fallback_reasoning_effort` maps or clears unsupported effort levels to prevent provider rejections.
4. **Parent State Preservation & Concurrency**:
   The parent session must exist and be idle (`ACTIVE_SESSION_REGISTRY.active_run_count == 0` and `status not in ("running", "waiting")`). The child review execution does not overwrite the parent session's output, status, or primary turn history.

## Native Thread Goals (`/goal`)

The runtime manages durable thread goals persisting across session turns and reloads:

1. **Durable Persistence & Events**:
   Thread goals persist in session metadata (`thread_goal` dictionary) and emit append-only events: `runtime.thread_goal_updated` on creation or status change, and `runtime.thread_goal_cleared` on deletion.
2. **Objective Spilling**:
   Large goal objectives exceeding 4,000 characters are automatically spilled to `.voidcode/attachments/<uuid>/goal-objective.md` via `spill_goal_objective_if_needed`, storing the file reference in metadata while keeping prompts bounded.
3. **Token Accounting & Budget Invariants**:
   When a goal defines a positive `token_budget`, completed turns increment `tokens_used`. Turns executed under plan collaboration mode (`collaboration_mode == "plan"`) or review children (`review_child == True`) are excluded from accounting. When cumulative usage meets or exceeds the budget, status transitions to `budget-limited` and a steering notice is appended.
4. **Cross-Turn Merging & Monotonic Timestamps**:
   `composition_session_metadata` and SQLite storage mixins merge thread goals across turns using monotonic timestamp tie-breaking (`updated_at`), guaranteeing goal state survives session resume and fresh runs without regressions.

## Native Slash Command Governance & Parity

Native actions, pickers, and modes are governed by a single authoritative specification (`src/voidcode/command/native.py:NATIVE_COMMANDS`):

1. **Single Source of Truth**:
   The HTTP command catalog (`GET /api/commands`), TUI command dispatcher (`app._submit`), and web client (`App.tsx:handleNativeCommand`) derive their native command definitions, argument rules, and busy gates from `NATIVE_COMMANDS`.
2. **Closed Builtin Enum (No Custom Macros)**:
   Slash commands (`/`) form a closed set of built-in native commands (Codex parity). Custom markdown macro commands and prompt command templating are removed; custom capability extensibility is provided via Skills (`$skill_name`) and `SkillRegistry`.
3. **Native Init Parity**:
   The `/init` command is a native client-side prompt submission (Codex parity) that injects the fixed AGENTS.md generation prompt as a normal governed agent turn under standard session tool approval policies, gated when a task is in progress.

## Shell Escape (`!`) & Consent Bypass

The runtime provides immediate shell escape execution bypassing provider turn cycles:

1. **Explicit Consent & Gate Bypass**:
   Because a typed `!<command>` is explicit direct user action, execution intentionally bypasses tool approval checks, permission engine mode gates, and sandboxing—including under read-only or plan collaboration modes.
2. **History Persistence Without Provider Turns**:
   Shell executions land synchronously in the session event log as `runtime.user_shell_executed` events containing command, output, duration, and exit status. No provider turn or model request is triggered. The output is available to subsequent provider turns via context assembly.
3. **Availability Under Concurrency**:
   Shell commands execute both when the session is idle and mid-turn while a primary run progresses, avoiding busy-gate deadlocks.

## Native Side Questions (`/btw`)

The runtime provides zero-write side question evaluation:

1. **Fork Semantics & Replayed Context**:
   Invoking `/btw` evaluates the user's question directly against the session's assembled context prefix (including replayed prior turns) using the session's own configured turn producer.
2. **Zero Tools & Single Turn**:
   The side question request offers zero available tools (`available_tools=()`) and advances exactly one provider turn. If the model attempts a tool call, a direct fallback answer explains that side questions accept direct answers only.
3. **Immutable History Invariant**:
   Side question execution strictly never writes to `session_events`, does not mutate session metadata, and does not advance the session's turn counter or transcript.
4. **Concurrent Execution**:
   Side questions possess no busy gate and can be executed while primary session runs are active.

## Session Lifecycle Actions & Busy Gates (`/new`, `/clear`, `/resume`, `/fork`, `/rename`)

The runtime provides explicit session management primitives governed by active concurrency barriers:

1. **Busy Gate Matrix & Concurrency Boundaries**:
   Operations that mutate active session identity, branch event history, or alter running state require an idle session. When `ACTIVE_SESSION_REGISTRY.active_run_count(workspace, session_id) > 0` or session status is `running` or `waiting`, the runtime rejects the action with `SessionBusyConflictError` (HTTP 409 `session_busy`):
   - **Busy-Gated Actions**: `/fork`, `/resume`, `/new`, `/clear`, `/compact`, `/plan`, `/review`, `/export`.
   - **Concurrent-Safe Actions**: `!<command>`, `/btw`, `/status`, `/copy`, `/context`, and `/rename` execute safely while a session is running.
2. **Session Title Persistence vs. Metadata**:
   Session titles (`rename_session`, `create_session(title=...)`) represent client-facing labels rather than agent execution metadata. They are persisted in dedicated columns on the SQLite `sessions` table. Setting or changing a title emits an append-only `session.renamed` event recording both the new `title` and `previous_title`. Renaming does not mutate conversation checkpoints and is permitted concurrently while a task progresses.
3. **Fork Invariants & Capability Inheritance**:
   `fork_session` allocates a new session row and copies the event prefix `1..N` (or up to explicit `at_sequence`) from the source session. The source session is strictly immutable. If the chosen sequence boundary splits a tool call from its matching completion, the operation fails visibly with `RuntimeSessionForkBoundaryError`. The forked session inherits the source session's `agent_capability_snapshot` (execution model, provider, reasoning effort) and records `forked_from_session_id` and `forked_at_sequence`.
4. **Resume & Replay Semantics**:
   Invoking `/resume` or `resume_stream` on an idle, completed session without approval parameters reconstructs the conversation view from the durable event log and replays stored chunks without initiating a new turn step. When resuming an interrupted or failed session, the runtime reconciles background task state and enters checkpoint recovery.

## Native Session Export (`/export`) & Approval-Free Persistence

The runtime compiles conversation history into portable Markdown without model tool intervention:

1. **Event Log Projection**:
   `export_session_markdown` iterates over the authentic completed event sequence (`core.request_received`, `core.model_turn`, tool executions, user shell commands, and completions) to generate structured Markdown (`# Session`, `## User`, `## Assistant`, code blocks, and tool results).
2. **User-Initiated Consent Bypass**:
   Similar to typed shell escapes (`!`), typing `/export` or invoking `write_session_export` constitutes an explicit, user-directed operational command rather than an autonomous tool invocation. Writing the exported file to disk bypasses model tool permission evaluation and interactive approval barriers.
3. **Workspace Path Confinement**:
   All export destinations must resolve strictly within the workspace directory (`candidate.is_relative_to(workspace.resolve())`). Path traversal sequences (`../`) or absolute targets outside the workspace are rejected with `ValueError("export path must stay inside the workspace")`.
4. **Idle-Only Invariant**:
   Session export requires a consistent, settled snapshot of conversation history and rejects active sessions (`SessionBusyConflictError`) if a run is in progress.

## Context Window Projection (`/context`)

The runtime exposes real-time context token utilization derived directly from turn engine prompt assembly:

1. **Zero-Turn Model-Free Inspection**:
   `/context` inspects token metrics by passing current session state through `PromptAssemblyPlan` and `assemble_provider_context` without advancing a provider turn, invoking completions, or modifying session history.
2. **Architectural Segment Breakdown**:
   Context consumption is partitioned across five architectural categories: `System prompt`, `Agent prompt`, `Tools`, `Goal steering`, and `Conversation history`.
3. **Ruler Consistency & Bounded Discrepancy**:
   Headline token usage is calculated by applying the provider model's authoritative tokenizer to the joined prompt payload. Individual segment token counts are bounded such that `abs(used_tokens - sum(segment.tokens)) <= len(segments)`.
4. **Concurrent Availability**:
   Context projection reads session state non-destructively, acquires no write locks, and is available idle or mid-run without blocking on busy gates.

## Model Selection & Reasoning Effort Semantics (`/model`)

The runtime provides dynamic model and reasoning effort selection persisting per session:

1. **Immediate Metadata Persistence**:
   Executing `/model` or `set_session_model(session_id=..., model=..., reasoning_effort=...)` immediately updates `runtime_config["model"]` and `runtime_config["reasoning_effort"]` in SQLite session metadata. Subsequent turns in the session advance using the updated model parameters.
2. **Effort Normalization & Retention**:
   Reasoning effort levels are validated and normalized via `normalize_reasoning_effort` (`"low"`, `"medium"`, `"high"`, `"none"`). If reasoning effort is omitted when switching models, any previously configured effort level is preserved in `runtime_config`.
3. **Catalog Query & Discovery**:
   Bare `/model` queries active provider catalogs via `list_available_models()` (`list_provider_summaries` and `provider_models_result`), returning provider names, model IDs, descriptions, reasoning capability flags, and supported effort choices.
4. **Concurrent Safety (No Busy Gate)**:
   `/model` specifies `busy_gate=False`. It executes safely mid-run without blocking on active runs or corrupting in-flight turn progression.

## Permissions Presets & Policy Mapping (`/permissions`)

The runtime implements governed approval policies configurable via user presets or direct approval modes:

1. **Preset-to-Mode Mapping Matrix**:
   Preset tokens map deterministically to VoidCode approval modes:
   - **`read-only`** (aliases `readonly`, `ask`) &rarr; **`ask`** mode: Read-only operations in the workspace are auto-allowed; workspace mutations (writes, edits) and execution tools (shell commands) require explicit approval.
   - **`auto`** (aliases `default`, `write`) &rarr; **`write`** mode: Workspace read and mutating operations (writes, edits) are auto-allowed; execution tools require approval.
   - **`full-access`** (aliases `full_access`, `full`, `yolo`) &rarr; **`yolo`** mode: Read, write, and execution operations are auto-allowed without prompting.
   Direct approval mode tokens (`ask`, `write`, `yolo`) are accepted interchangeably with preset labels.
2. **Dynamic Policy Updates**:
   `set_session_permissions` immediately replaces the active `_permission_policy` on the runtime and persists `runtime_config["approval_mode"]` into session metadata in SQLite.
3. **Effective Read-Only Invariance**:
   Switching permissions presets governs approval thresholds for mutating tools but cannot relax an externally frozen base read-only session. `runtime_effective_read_only_from_metadata` continues to enforce read-only denials regardless of the selected approval mode.
4. **Concurrent Safety (No Busy Gate)**:
   `/permissions` specifies `busy_gate=False`. Approval modes can be adjusted dynamically mid-run or while execution is paused.

## Pending Approval Queue & Approve-One Semantics (`/approve`)

The runtime manages pending tool approval requests across primary sessions and background delegations:

1. **Unified Approval Aggregation**:
   `list_pending_approvals` aggregates pending approvals from the primary session row (`pending_approval_json`) and any child sessions spawned by delegated background tasks (`list_background_tasks_by_parent_session`).
2. **Empty Queue Behavior**:
   When no approvals are pending, `approve_pending_action` returns `{approved: False, reason: "no_pending_approvals"}` without failing or mutating state.
3. **Approve-One Semantics**:
   When one or more actions are queued, bare `/approve` (or omitting `request_id`) selects and claims exactly the first pending approval via `_claim_pending_approval`, returning `{approved: True, request_id: ...}`.
4. **Explicit Request Targeting**:
   Supplying an explicit `[request_id]` claims that specific approval. If the requested ID is not present in the pending queue, it returns `{approved: False, reason: "not_found"}`. In the interactive TUI, multiple pending approvals open `ApprovePickerOverlay` for unambiguous selection.
5. **Concurrent Safety (No Busy Gate)**:
   `/approve` specifies `busy_gate=False`, allowing operators to approve pending actions immediately to unblock paused runs.

## Runtime Inspection Projections (`/mcp`, `/hooks`, `/skills`, `/agents`, `/usage`)

The runtime provides five native read-only inspection projections exposing live system state:

1. **Zero-Turn Non-Blocking Invariant**:
   All five projection commands define `busy_gate=False`, advance zero provider turns, append no events to SQLite session history, and execute safely mid-run without acquiring exclusive write locks.
2. **Authoritative State Projection Sources**:
   - **`/mcp` (`session_mcp_inventory`)**: Projects active Model Context Protocol servers and registered tools from `current_mcp_state()`. Compact mode returns server names, status, and tool counts; `verbose` mode exposes commands, URLs, environment configurations, and transport types.
   - **`/hooks` (`session_hooks_listing`)**: Projects runtime lifecycle hooks from `RuntimeHooksConfig` across event surfaces (`session_start`, `pre_tool`, `post_tool`, `session_end`), returning command lines, tool match patterns, and enabled state.
   - **`/skills` (`session_skills_listing`)**: Projects registered skills discovered by `SkillRegistry` (`list_skill_summaries`) across workspace, user, and plugin paths, sorted alphabetically with descriptions and argument hints.
   - **`/agents` (`session_agents_overview`)**: Projects active session records from SQLite storage, running subagent delegations from `RuntimeBackgroundTaskSupervisor`, and primary agent preset configurations (`list_agent_summaries`).
   - **`/usage` (`session_token_usage`)**: Projects cumulative token consumption (input, output, cache read/write), USD cost, and turn counts from `session.metadata["provider_usage"]`. Estimation is measured-provider-usage-first with exact per-model tokenizer fallback (superior to byte/4 heuristics; byte/4 only for unknown models).

## Scheduled Task & Cron Execution Contract

The runtime manages periodic and deferred task execution through a dedicated scheduler thread and transactional SQLite persistence:

1. **Schedule Store & Schema Persistence**:
   Scheduled tasks persist in the workspace SQLite database (`scheduled_tasks` table via `SqliteSessionStore` / `ScheduledTaskRepository`). Every row stores schedule identity (`id`), workspace path, canonical schedule expression (`schedule_text`), kind (`every`, `at`, `cron`, `once`), dispatched prompt, next trigger epoch (`next_fire_at`), optional parent session ID, enablement flag (`enabled`), running task pointer (`running_task_id`), creation epoch (`created_at`), and expiration epoch (`expires_at`).
2. **Atomic CAS Claim Semantics**:
   When evaluating due schedules, `claim_due_schedules(now)` executes an atomic compare-and-swap update in an immediate transaction:
   `UPDATE scheduled_tasks SET running_task_id = ? WHERE workspace = ? AND enabled = 1 AND running_task_id IS NULL AND next_fire_at <= ?`.
   A temporary unique claim token (`claiming-<uuid>`) is set, guaranteeing that across concurrent scheduler threads or processes, exactly one claimant wins each due firing slot without races.
3. **Deterministic Tick Lifecycle**:
   `SchedulerThread` executes periodic deterministic ticks (default cadence `SCHEDULER_TICK_SECONDS = 5.0`):
   - **Cleanup**: Evaluates expired recurring tasks whose final execution has finished (`expires_at <= now` and `running_task_id IS NULL`) and deletes them via `delete_expired_scheduled_tasks`.
   - **Overlap Detection**: Inspects due schedules where `running_task_id IS NOT NULL` and logs debug skip notifications.
   - **Atomic Claim**: Claims due schedules matching `enabled = 1`, `next_fire_at <= now`, and `running_task_id IS NULL`.
   - **Task Dispatch**: For each claimed schedule, dispatches a background task through `supervisor.start_background_task` and replaces the temporary claim token with the authoritative background `task.id`. If dispatch fails, the claim token is released back to `NULL`.
4. **Fire-to-Background-Task Mechanics**:
   Scheduler firings synthesize an asynchronous `RuntimeRequest` carrying the scheduled `prompt`, linked `parent_session_id`, and metadata flags (`schedule_id` and `scheduled_task_id`). Execution proceeds through `RuntimeBackgroundTaskSupervisor` as an independent background task; terminal results and state transitions flow through standard background worker channels.
5. **Overlap Skip & Missed-Fire Catch-Up**:
   If a previously fired task remains running when its next scheduled interval arrives (`running_task_id IS NOT NULL`), the schedule cannot be claimed and the interval fire is skipped (skip-if-running). When the running task finishes (`on_task_terminal`), `next_fire_at` is recomputed from the current timestamp (`now`) forward (`next_fire_after(expr, now)`), ensuring past missed slots are never backlogged or replayed.
6. **Expiry & Deletion Rules**:
   - **Recurring Schedules (`every`, `cron`)**: Default to a 7-day expiration (`DEFAULT_RECURRING_EXPIRY_SECONDS = 604,800`) unless an explicit `expires_at` is configured. Upon task completion after expiration has passed, the schedule is automatically deleted from the database.
   - **One-Shot Schedules (`at`, `once`)**: Do not auto-expire (`expires_at = None`). When the spawned background task completes with status `completed`, the schedule is deleted from the store. If the task fails (`error`, `cancelled`, or `interrupted`), the schedule remains persisted with `enabled = 0` (disabled) and `running_task_id = NULL` for post-mortem inspection (OpenClaw parity).
7. **Immediate-Fire Capability**:
   `runtime.fire_schedule(schedule_id=...)` bypasses the tick wait loop, directly dispatching the schedule's prompt as an active background task and binding `running_task_id` immediately. The `/loop` native command and HTTP `/api/sessions/{session_id}/loop` endpoint invoke immediate-fire so that newly scheduled recurring jobs execute turn 1 without delay.
