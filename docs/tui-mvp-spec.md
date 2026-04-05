# TUI MVP Specification

Source issue: #18

This document defines the minimum interaction model and canonical smoke flow for the VoidCode TUI MVP.

It is intentionally narrow. The goal is to specify one terminal client that can drive the existing runtime boundary with keyboard-first interaction, runtime-owned approvals, ordered event rendering, and persisted session replay.

## Relationship to the current CLI

The TUI does **not** replace the existing CLI contract work and does **not** bypass it by calling tools directly.

For MVP, the TUI should be treated as a **new runtime client** that sits alongside the current text CLI:

- the CLI remains a valid text-first client and verification surface
- the TUI consumes the same runtime concepts as the CLI
- both clients rely on the runtime for execution, events, approvals, and persistence

This keeps the client boundary consistent with `docs/architecture.md` and `docs/contracts/client-api.md`.

## Minimum interaction model

The TUI MVP should support exactly these interaction capabilities.

### 1. Prompt entry

- one focused prompt input for entering a task request
- keyboard submission for starting a run
- a visible busy/idle state so the operator can tell whether the active turn is still running

The MVP does not require advanced prompt editing beyond what is needed to submit one task reliably.

### 2. Activity timeline

- one primary timeline pane that renders ordered runtime events for the active session
- timeline entries driven by runtime event data rather than inferred from plain text alone
- live activity visibility while a run is in progress
- replay of the stored event sequence when a prior session is resumed

This timeline should be sufficient to show the current implemented event progression documented in `docs/contracts/runtime-events.md`.

### 3. Tool activity grouping

- tool-related events should appear as grouped activity within the timeline
- grouped tool blocks may be collapsible, truncated, or otherwise compact, as long as the operator can inspect the result
- the TUI must not require raw shell output inspection to understand whether a tool request started or completed

### 4. Approval interaction

- if the runtime enters an approval-required state, the TUI should present a focused approval interaction without requiring the operator to drop back to raw shell commands
- approval decisions are submitted back to the runtime as runtime-owned actions using the contract in `docs/contracts/approval-flow.md`
- the TUI must be able to distinguish pending approval from resolved approval when replaying stored session state

The TUI spec does not define a new approval schema. It consumes the existing one.

### 5. Session list and resume

- the operator can list persisted sessions inside the TUI
- the operator can select a stored session and resume or replay it using runtime-owned session operations
- the resumed session view must be derived from persisted runtime state, not reconstructed from private TUI memory

### 6. Keyboard-first focus model

- all core actions needed for the MVP flow must be possible from the keyboard alone
- the TUI should make focus movement explicit between at least: prompt input, activity timeline, and session list/resume surface
- approval interactions should capture focus until resolved or dismissed according to runtime state

This document does not lock in final keybindings. It only requires a keyboard-first flow.

## Runtime boundary rules

The TUI MVP must obey these rules:

1. **Runtime as system boundary**
   - the TUI talks to runtime-owned operations for run, list, load, resume, and approval resolution
   - the TUI does not call tools directly

2. **Event-driven rendering**
   - timeline state is derived from ordered runtime events and final output
   - the TUI should not invent private state transitions that cannot be recovered from persisted runtime data

3. **Persistence-owned replay**
   - session history comes from runtime persistence
   - resuming a session must replay persisted runtime state rather than reconstructing UI history from ad hoc client caches

4. **Approval ownership stays in runtime**
   - the TUI captures user intent and sends an approval decision back to the runtime
   - the runtime remains responsible for validating and applying the decision

## Live stream and replay expectations

The TUI MVP must support the same observable model in two modes:

- **live activity** during an active run
- **persisted replay** when resuming a prior session

The rendered meaning of an event should remain consistent across both modes. A resumed session should not require separate client logic that interprets the same runtime history differently.

This follows the expectations in `docs/contracts/stream-transport.md`.

## Canonical smoke flow

This smoke flow is the minimum demonstration that the TUI is usable for MVP work.

### Flow

1. Launch the TUI for a workspace and show the default prompt-focused screen.
2. Submit one read-only request from the prompt input.
3. Observe ordered runtime activity in the timeline while the request is running.
4. Inspect the grouped tool activity for the request and confirm the final output is visible in the TUI.
5. Trigger one approval-required request from the same session.
6. Resolve the approval from inside the TUI.
7. Observe the resulting runtime state transition in the timeline.
8. Exit the TUI, reopen it, list the stored session, and replay or resume that session from persisted runtime state.

### Expected observable results

- the operator can submit a request without leaving the TUI
- the operator sees ordered runtime progress instead of a frozen terminal while work is happening
- approval-required execution pauses visibly and can be resolved inside the TUI
- the stored session can be found again and replayed using runtime-backed state
- replayed history preserves the meaning and ordering of the original observable flow

## Acceptance criteria

- a TUI MVP spec exists and is specific enough to slice follow-on implementation issues without ambiguity
- the TUI is defined as a runtime client, not as a direct tool executor
- the minimum interaction set is explicit: prompt entry, timeline rendering, grouped tool activity, approval handling, session list, and resume
- the canonical smoke flow covers one read-only request, one approval-required request, and persisted session replay
- the TUI behavior is aligned with runtime-owned approvals, runtime event ordering, and runtime persistence contracts
- the MVP remains keyboard-first and does not depend on mouse interaction or shell fallback for core task completion

## Non-goals

This specification does **not** define:

- deep visual polish, theming, or animation work
- multi-agent terminal UX
- a new event schema, approval schema, or wire protocol
- a full in-TUI configuration editor
- advanced terminal multiplexing beyond what is needed for the MVP flow

## Follow-on implementation slices

At minimum, implementation work after this spec should be sliceable into separate issues for:

- TUI shell and focus management
- active timeline rendering from runtime events
- grouped tool activity rendering
- approval interaction handling
- session list and resume flows
- TUI smoke or end-to-end verification

## References

- `docs/mvp-todo-plan.md` (Phase 3)
- `docs/contracts/runtime-events.md`
- `docs/contracts/approval-flow.md`
- `docs/contracts/stream-transport.md`
- `docs/contracts/client-api.md`
- `docs/contracts/runtime-config.md`
