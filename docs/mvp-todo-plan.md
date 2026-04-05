# VoidCode MVP TODO Plan

This document turns the current roadmap into a delivery checklist for the first real product loop.

Use GitHub issues and milestones as the executable backlog. This document defines the execution shape and acceptance direction; `docs/contracts/` defines normative client-facing contracts.

## Status

VoidCode already has a truthful pre-MVP foundation:

- Python 3.14 repo/tooling baseline
- deterministic CLI → runtime → graph → tool read-only slice
- local session persistence and resume
- Bun frontend shell with mocked state

What it still does **not** have is a concrete, taskable MVP execution plan that connects the runtime, a usable terminal client, and a real web client.

## End-state vision

The longer-term product target is:

1. a highly configurable local-first agent runtime
2. a frontend that can reflect agent activity and, later, agent-to-agent interaction
3. a TUI experience with the same core feel as tools like OpenCode: prompt entry, streaming output, visible tool activity, approvals, persistence, and session recovery

## MVP boundary

### MVP includes

- one reliable **single-agent** end-to-end runtime loop
- real tool execution through the runtime boundary
- approval gates for writes and risky shell actions
- persisted sessions with resume support
- observable runtime events that clients can render
- one usable TUI client backed by real runtime events
- one usable web client backed by real runtime events
- a minimal but real configuration surface for runtime behavior

### MVP does not include

- true multi-agent orchestration
- frontend visualizations focused on interaction between multiple agents
- cloud collaboration or hosted synchronization
- IDE plugins
- plugin marketplace support
- deep MCP ecosystem work beyond what is needed for the main loop
- advanced visual workbench UX beyond one practical task flow

## Scope tension to resolve explicitly

The user-facing end goal includes frontend visibility into **agent-to-agent** interaction, but the current repo architecture and roadmap define MVP as a stable **single-agent** loop first.

This plan keeps that current boundary:

- MVP: render real event flow for one active agent loop
- Post-MVP: introduce true multi-agent execution and dedicated multi-agent visualization

If the project decides multi-agent collaboration is required for MVP, `docs/roadmap.md` and `docs/architecture.md` should be updated first because that changes the current boundary.

## Delivery principles

- tests and contracts come before polish
- every phase must produce observable runtime behavior
- CLI/TUI/web clients must consume runtime boundaries, not bypass them
- configuration must be real enough to prove the runtime is not hard-coded
- event schemas must be stable enough for both TUI and web rendering

## Phase 0 — planning and contracts

Goal: make the remaining work testable before building more product surface.

### TODO

- [ ] define the runtime event schema used by CLI/TUI/web clients
- [ ] define the approval interaction schema for `allow`, `deny`, and `ask`
- [ ] define the client-facing API contract for session list/load/run/stream
- [ ] define the minimal runtime configuration surface for MVP
- [ ] document what counts as MVP done vs. post-MVP expansion

### Acceptance criteria

- every planned client can consume the same event vocabulary
- approval requests and results have explicit payload shapes
- session lifecycle is documented from create → stream → persist → resume
- the config surface is small and concrete, not aspirational

## Phase 1 — stable headless runtime loop

Goal: move from the current deterministic slice to one governed single-agent execution loop.

### TODO

- [ ] expand graph flow beyond the current read-only slice into a real turn loop
- [ ] add built-in search/file/system tools through the runtime pipeline
- [ ] implement the permission engine for writes and risky shell actions
- [ ] implement hook registration and hook execution logging
- [ ] make runtime event emission complete enough for client rendering
- [ ] define failure handling and retry behavior for interrupted turns

### Acceptance criteria

- one development task can execute through runtime → graph → tools → finalize response
- write/risky actions cannot bypass approval
- turns, approvals, tool calls, and failures emit stable events
- runtime persistence survives process restart and session resume
- integration tests cover the governed loop, not just isolated contracts

## Phase 2 — minimal configurability for MVP

Goal: prove the runtime is configurable without exploding the surface area.

### TODO

- [ ] add a config model for runtime defaults (workspace, model/provider, approval mode, hooks)
- [ ] define config precedence (repo file, environment, CLI flags, session overrides)
- [ ] persist session-level settings needed for resume
- [ ] expose config inspection in at least one client-facing path
- [ ] document the MVP config surface clearly

### Acceptance criteria

- a user can change runtime behavior without editing source code
- resume preserves the settings that matter for continued execution
- config precedence is deterministic and documented
- tests cover config loading and override behavior

## Phase 3 — TUI MVP client

Goal: ship a keyboard-first client with the same core interaction shape users expect from terminal-native coding agents.

This phase implements the **TUI MVP Spec** in [`docs/tui-mvp-spec.md`](./tui-mvp-spec.md). Follow-on implementation tasks should be sliced directly from that spec.

### TODO

- [ ] finalize the TUI MVP specification in `docs/tui-mvp-spec.md`
- [ ] implement the terminal layout with distinct prompt and activity zones
- [ ] render streaming runtime events into a scrollable, readable timeline
- [ ] implement collapsible tool call and result blocks in the activity feed
- [ ] integrate interactive approval prompts for risky tool executions
- [ ] implement session management (list, load, resume) directly in the TUI
- [ ] add automated smoke tests for the canonical TUI smoke flow defined in `docs/tui-mvp-spec.md`

### Acceptance criteria

- **Single-task completion**: a user can successfully finish one "read and edit" task using only the TUI
- **Activity visibility**: streaming events (turns, tool starts, tool outputs) are visible without UI flickering or blocking
- **Approval flow**: risky actions pause execution and display a clear TUI prompt that accepts immediate user input
- **Persistence parity**: the TUI correctly lists and resumes sessions created by other clients (CLI) using shared runtime storage
- **Spec alignment**: the final implementation meets the acceptance criteria defined in `docs/tui-mvp-spec.md`

## Phase 4 — web client MVP alignment

Goal: convert the current mock-backed frontend into a real runtime client.

### TODO

- [ ] add a real backend API layer for the frontend to consume
- [ ] add streaming transport for runtime events to the frontend
- [ ] replace mocked task/activity/session data with real session state
- [ ] render runtime/tool/approval/timeline events in the web UI
- [ ] expose session list, load, and resume in the frontend
- [ ] connect the frontend config surface to the real runtime config model
- [ ] add frontend integration tests around session rendering and streaming

### Acceptance criteria

- frontend state is driven by real runtime/API data, not mocks
- a user can observe the active agent turn, tool activity, and approvals live
- the frontend can resume a prior session from persisted runtime state
- the frontend and TUI are consuming the same underlying runtime concepts

## Phase 5 — integration and demo readiness

Goal: make the product demonstrable and repeatable for contributors.

### TODO

- [ ] define one canonical demo flow covering runtime + TUI + web client
- [ ] add full-stack test coverage for the primary product loop
- [ ] document operator workflows for common failures and recovery
- [ ] add observability hooks/logging needed for debugging live sessions
- [ ] update README and contributor docs to reflect the real MVP path

### Acceptance criteria

- one canonical task flow works repeatedly across fresh runs
- failures are observable enough to debug without guesswork
- docs match the actual product behavior and setup sequence
- the repo exposes a clear contributor path for continuing MVP work

## Post-MVP expansion

These belong **after** the single-agent MVP is stable:

- true multi-agent orchestration and delegation
- frontend visualization focused on interaction between multiple agents
- richer approval policies and more granular governance controls
- more advanced TUI polish and parity work
- cloud/shared session collaboration
- IDE integrations and external plugin ecosystem work

## Verification policy

Each phase should define its verification up front.

### Required layers

- unit tests for contracts, config, permissions, and event shapes
- integration tests for runtime loop, persistence, hooks, and approvals
- client smoke tests for CLI/TUI/web session flows
- manual QA for streaming, tool visibility, and approval UX

### Preferred language for acceptance criteria

- Given a persisted session, when the user resumes it in TUI or web, then the same turn history and result are rendered
- Given a write-capable tool request, when approval mode is `ask`, then execution pauses until an explicit decision is recorded
- Given a streamed runtime event sequence, when a client renders it, then tool progress remains visible throughout the turn

## Suggested execution order

1. event/API/config contracts
2. governed single-agent runtime loop
3. minimal config surface
4. TUI MVP client
5. web client live integration
6. integration/demo hardening

## Immediate next TODOs for the repo

- [ ] write an event schema doc for runtime/client streaming
- [ ] write an API contract doc for frontend/TUI integration
- [ ] break issue #7 into concrete frontend integration tasks
- [ ] open a TUI-specific issue/epic with acceptance criteria
- [ ] add configurability design notes for runtime/session settings
- [ ] convert the current roadmap epics into linked executable issues
