# Runtime-Owned Scheduler Design

## Status

- Status: proposed
- Scope: design-only
- Target repo: `voidcode`
- Date: 2026-04-12

## Context And Motivation

VoidCode already has a clear runtime-centered execution boundary: `VoidCodeRuntime` owns run/stream/resume entrypoints, approval continuity, event emission, provider fallback, and session persistence. SQLite-backed storage already acts as the local truth source for sessions, event replay, pending approvals, and resume checkpoints.

That makes scheduler work a runtime concern, not a client concern.

The goal of this document is to define a first-phase scheduler design that feels closer to the product shape people expect from Claude Code style scheduled runs, while still respecting VoidCode's existing architecture:

- clients stay thin
- execution still flows through the runtime boundary
- session truth remains local and replayable
- approval and recovery semantics remain runtime-owned

This is not a proposal to add a separate cloud scheduler, a new generic background-task framework, or a daemon-first rewrite.

## Goals

Phase 1 scheduler work should achieve the following:

1. Add a runtime-owned scheduling model for local scheduled runs.
2. Keep schedule dispatch on the existing runtime execution path instead of inventing a parallel execution surface.
3. Persist schedule definitions and scheduler state locally in the same workspace-owned truth domain as sessions.
4. Preserve current session, replay, approval, and checkpoint semantics.
5. Keep the first implementation small enough to land without rewriting `runtime/`, `graph/`, or client contracts.

## Non-Goals

Phase 1 explicitly does **not** aim to provide:

- cloud or distributed scheduling
- a generic job queue or background-task platform
- graph-owned scheduling
- client-owned scheduling in CLI, Web, or TUI
- direct UI-to-tool execution
- backlog catch-up and replay of all missed fires after downtime
- overlapping concurrent runs for the same schedule
- reuse of a single long-lived session across recurring scheduled runs by default
- daemon-first or multi-process scheduler coordination as a baseline requirement

## Current Runtime Baseline

The scheduler design must fit the current codebase, not an aspirational rewrite.

### Runtime ownership that already exists

`src/voidcode/runtime/service.py` already centralizes:

- `run`, `run_stream`, `resume`, and `resume_stream`
- permission resolution and approval pauses
- hook execution
- event emission and event renumbering
- provider fallback and runtime config application
- session persistence through `SessionStore`

### Storage truth that already exists

`src/voidcode/runtime/storage.py` already uses SQLite under `.voidcode/` as the local truth source for:

- session metadata
- session event history
- pending approval state
- resume checkpoints

This matters because the scheduler should not introduce a separate truth source for execution history. It can add schedule-specific state, but actual run truth should remain session-scoped.

### Client boundary that already exists

CLI and HTTP transport currently consume the runtime boundary. They do not own execution semantics. The scheduler should preserve that model by dispatching normal runtime runs rather than bypassing the runtime through transport-specific code paths.

## Scheduler Ownership Boundary

The scheduler should be **runtime-owned**.

That means:

- schedule definitions, due-run decisions, and dispatch policy belong to the runtime layer
- scheduled execution must enter the system as a normal runtime request
- session persistence, approval handling, replay, and checkpoints stay under runtime ownership

That also means the scheduler should **not** be owned by:

- `graph/`
- CLI command handlers
- HTTP handlers
- Web or TUI clients
- an unrelated generic worker subsystem

### Important nuance

Runtime-owned does **not** mean every `VoidCodeRuntime` instance should embed a long-lived timer loop.

The runtime boundary should own scheduler semantics and persistence, while the local host process that ticks the scheduler can be thin. A future `serve`-adjacent host loop or dedicated local scheduler entrypoint may drive polling, but the authoritative scheduling model still belongs to the runtime layer.

## Phase 1 Shape

The recommended first slice is **internal-scheduler-first**.

That means Phase 1 should include:

- persistent schedule definitions
- a local poller that detects due schedules
- runtime dispatch of scheduled runs
- minimal run indexing from schedule to sessions
- explicit overlap and missed-fire policy

Phase 1 should avoid:

- building a generalized async work platform
- building a permanently detached daemon model first
- supporting multiple local scheduler hosts against the same workspace as a normal supported mode

## Scheduling Lifecycle

The Phase 1 scheduling lifecycle should be:

1. A schedule definition is stored in workspace-local scheduler state.
2. A local scheduler host polls for due schedules.
3. When a schedule becomes due, the scheduler creates a normal `RuntimeRequest` with schedule metadata.
4. `VoidCodeRuntime` executes the request through the existing run path.
5. The resulting run is persisted as a normal session with normal events, output, approval state, and checkpoint behavior.
6. Scheduler state records the outcome at the schedule/run-index level without replacing session truth.

This keeps the scheduler as an initiator of runtime runs rather than a second execution engine.

## Persistence Model

Phase 1 should use the existing workspace-local SQLite truth domain.

The persistence split should be:

- **sessions remain execution truth**
- **schedules store future intent and dispatch state**

Conceptually, schedule-oriented state needs to cover:

- schedule identity
- user-defined prompt or request payload baseline
- schedule expression or interval policy
- timezone policy
- enabled/disabled state
- next due time
- last attempted fire time
- last successful fire time
- overlap policy state for single-flight enforcement
- links from a schedule to emitted session ids or recent run summaries

The scheduler must not treat schedule records as a replacement for session event history. If a scheduled run needs replay, inspection, approval resolution, or debugging, the source of truth remains the session record and its events.

## Session Semantics For Scheduled Runs

Phase 1 should default to **a fresh session for every schedule occurrence**.

This is the right default because current storage, replay, and approval semantics are session-centric. Reusing one session for recurring runs would immediately complicate:

- replay boundaries
- approval continuity
- event ordering
- checkpoint meaning
- failure diagnosis across multiple firings

### Required default behavior

- each due schedule occurrence creates a new `session_id`
- session metadata records schedule provenance, such as `schedule_id`, trigger timestamp, and initiator
- replay stays session-scoped
- session resume stays session-scoped

### Explicitly deferred behavior

The idea of “recurring work continuing the same long-lived conversational session” is deferred. If that becomes a product requirement later, it should be treated as a higher-complexity design problem rather than silently folded into Phase 1.

## Approval, Resume, And Replay Semantics

Scheduled runs should remain normal runtime runs.

Therefore:

- if a scheduled run reaches an approval boundary, it enters the existing `waiting` session state
- pending approval is stored through the same runtime persistence path
- approval resolution happens through existing resume/approval flows
- replay remains the existing replay of session events

Phase 1 should **not** invent a second approval or background-run model just for scheduled work.

### Practical implication

If a scheduled run is blocked on approval, the scheduler should not automatically "push through" that boundary. The run has already become a normal session and should be resolved using the same runtime-owned approval semantics as any other session.

## Overlap Policy

Phase 1 should use **single-flight overlap semantics per schedule**.

Recommended rule:

- if the prior run for a schedule is still `running` or `waiting`, the next due tick for that same schedule is skipped or coalesced instead of launching a second concurrent run

This keeps the initial model understandable and protects the current runtime/session path from schedule-level concurrency explosions.

Phase 1 should not support:

- unbounded backlog queues for one schedule
- concurrent same-schedule runs by default
- implicit merging of two due occurrences into one session lineage

## Missed Fires, Downtime, And Clock Behavior

Phase 1 needs explicit policy here, even if it stays intentionally narrow.

### Missed fires while the process is down

Phase 1 should **not** guarantee catch-up replay of all missed scheduled occurrences after downtime.

Recommended default:

- when the scheduler host restarts, it recalculates the next valid due run from persisted schedule state
- missed occurrences during downtime are not replayed as a backlog by default

This keeps Phase 1 small and avoids smuggling in a queueing system.

### Timezone policy

Schedule definitions should carry an explicit timezone interpretation or clearly documented default timezone behavior. The implementation should not rely on hidden process-local assumptions.

### Clock jumps

Clock jumps and local time discontinuities should be treated as a correctness concern for the eventual implementation. Phase 1 should define behavior conservatively and avoid making strong guarantees beyond local best-effort polling.

## Retry And Failure Policy

Scheduler failure policy should stay narrow in Phase 1.

### Dispatch-level failures

If schedule dispatch fails before a session is created, the scheduler should record the failure in schedule-oriented state so the failure is visible and diagnosable.

### Runtime-level failures

If a session is created and the run later fails, failure belongs to the normal session lifecycle:

- the session becomes `failed`
- failure details live in normal runtime events and session output/history
- schedule-level state may record a summary pointer, but not replace session truth

### Retry scope

Phase 1 may support a minimal retry/backoff policy for scheduler dispatch failures, but should avoid complex policy matrices. It does not need full queue semantics, arbitrary retry orchestration, or durable worker leasing.

## Event And Observability Expectations

Scheduled runs should preserve the current runtime event model rather than forcing a premature contract rewrite.

For Phase 1:

- scheduled runs should emit the same core session-scoped runtime events as normal runs
- session metadata should be sufficient to identify scheduled provenance
- additive scheduler-specific observability can begin as runtime-internal or design-level guidance before it becomes part of stable client-facing contracts

This is important because `docs/contracts/runtime-events.md` currently defines a session-scoped stable event vocabulary. The scheduler design should stay compatible with that model in its first slice.

## Client And Transport Relationship

Clients remain consumers of runtime truth.

That implies:

- CLI, Web, and TUI should observe scheduled runs as ordinary sessions
- clients may later surface schedule metadata and schedule administration features
- clients should not own timer loops or direct execution behavior

The HTTP layer and CLI may eventually expose schedule management surfaces, but they should remain transport/control entrypoints, not the scheduler's source of truth.

## Interaction With Execution Engines

The scheduler should dispatch into the runtime boundary and remain agnostic about the concrete execution engine selected by the resolved runtime config.

In other words:

- the scheduler should not special-case `deterministic` vs `single_agent` in its ownership model
- engine selection remains runtime config behavior
- scheduled execution still uses the normal runtime-governed tool, permission, hook, and persistence path

This keeps the scheduler aligned with the existing post-MVP direction where the runtime governs multiple execution engines without letting those engines take over product control-plane responsibilities.

## Configuration And Policy Inputs

The scheduler should consume **resolved runtime policy**, not raw client input scattered across transports.

That means the eventual implementation should follow the same ownership rules already used elsewhere in the runtime:

- runtime defaults
- user config
- project config
- environment overrides
- request or command overrides

Schedule definitions may carry schedule-specific execution defaults, but those should still be interpreted through runtime-owned config resolution rather than inventing a parallel config system.

## Unsupported Phase 1 Multi-Host Behavior

Phase 1 should treat **multiple local scheduler hosts targeting the same workspace database** as unsupported.

Without a claim/lease model, multiple hosts would risk duplicate firing, overlapping ownership, or inconsistent scheduler state. A future phase can add lightweight coordination if multi-process attachment becomes a real requirement.

For Phase 1, one local scheduler host per workspace is the safe and honest boundary.

## Proposed Out-Of-Scope Follow-Ups

These are reasonable future follow-ups, but should not be folded into this first design slice:

- schedule CRUD transport and client UX
- durable leases or leader election for multi-host coordination
- catch-up backlog policy after downtime
- recurring runs that intentionally continue one long-lived session lineage
- richer scheduler-specific event vocabulary in stable client contracts
- daemonized always-on local scheduling detached from the caller lifecycle
- remote or cloud-managed scheduling

## Verification Plan For Future Implementation

This PR is design-only, but the design should still define what correctness means.

Future implementation work should verify at least the following:

1. A due schedule dispatches a normal runtime run and persists a new session.
2. Scheduled runs emit the normal session-scoped runtime event sequence in order.
3. A scheduled run that requests approval becomes a normal `waiting` session and can be resumed through the existing approval path.
4. A failed scheduled run becomes a normal `failed` session without corrupting scheduler state.
5. Single-flight overlap policy prevents concurrent same-schedule runs.
6. Restarting the local scheduler host does not replay an unbounded backlog of missed fires by default.
7. Session replay remains sufficient to inspect scheduled execution without requiring a separate scheduler-specific replay pipeline.
8. Scheduler behavior remains compatible with current execution engine selection rather than binding scheduling to one engine.

Appropriate verification homes would likely include runtime-focused unit tests plus integration coverage around storage, dispatch, approval continuity, and replay.

## Open Questions

The following questions are intentionally left open for later implementation work or follow-on design:

1. What exact schedule expression format should Phase 1 support first?
2. Where should the local scheduler host entrypoint live: a dedicated command, a `serve`-adjacent mode, or another runtime-owned host surface?
3. How much scheduler-specific metadata should become part of stable client contracts in the first implementation slice?
4. Should dispatch-level scheduler failures surface only through schedule state, or also through additive runtime events?
5. When multi-host or daemonized operation becomes necessary, should SQLite-based claims be sufficient, or will a stronger coordination model be required?

## Summary

The correct first scheduler design for VoidCode is a **runtime-owned, internal-scheduler-first** model that dispatches **normal runtime runs** into **fresh sessions**, while keeping approvals, replay, checkpoints, and execution truth exactly where they already belong: inside the runtime/session boundary.

That keeps the design close to the current architecture, avoids inventing a second execution model, and creates a credible path toward Claude Code style scheduled runs without breaking the control-plane principles already established in this repository.
