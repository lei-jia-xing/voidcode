# Approval Flow Contract

Source issue: #15

## Purpose

Define the MVP governed-execution contract for approval decisions around write-capable or risky actions.

## Status

The current deterministic runtime emits a permission event with `decision=allow`, but the full governed approval flow is not implemented yet.

## Current code anchors

- permission responsibility is assigned to the runtime in `docs/architecture.md`
- current runtime emits `runtime.permission_resolved` in `src/voidcode/runtime/service.py`
- current payload includes:
  - `tool`
  - `decision`

## MVP decision vocabulary

- `allow`: execution proceeds
- `deny`: execution does not proceed
- `ask`: execution pauses until an explicit client or operator decision is recorded

## Approval request contract

An approval request must be representable with at least:

- `session_id`
- `sequence`
- `tool`
- `reason` or risk context
- proposed arguments or target summary
- current policy context

This should be emitted as a runtime event rather than as client-only UI state.

### Planned approval request shape

The MVP contract should support an approval-request event payload with at least:

```json
{
  "request_id": "approval-1",
  "session_id": "session-123",
  "sequence": 4,
  "tool": "write_file",
  "decision": "ask",
  "arguments": {
    "path": "README.md"
  },
  "target_summary": "write README.md",
  "reason": "write-capable tool invocation",
  "policy": {
    "mode": "ask"
  }
}
```

Field intent:

- `request_id`: stable identifier for later resolution and replay
- `session_id`: owning session
- `sequence`: ordering marker in the event stream
- `tool`: tool name awaiting approval
- `decision`: `ask` for pending approval requests
- `arguments`: proposed tool arguments or a redacted equivalent
- `target_summary`: human-readable target summary for clients
- `reason`: why approval is required
- `policy`: policy context relevant to the decision

## Approval resolution contract

An approval resolution must be able to record:

- `session_id`
- the request being resolved
- `decision`: `allow` / `deny`
- optional operator note
- timestamp or ordering marker sufficient for resume/replay

### Planned approval resolution shape

The MVP contract should support a resolution payload with at least:

```json
{
  "request_id": "approval-1",
  "session_id": "session-123",
  "decision": "allow",
  "note": "approved from tui",
  "resolved_sequence": 5
}
```

Field intent:

- `request_id`: correlates the resolution with the original approval request
- `session_id`: owning session
- `decision`: final decision, either `allow` or `deny`
- `note`: optional operator or client note
- `resolved_sequence`: ordering marker sufficient for replay and resume

### Client-to-runtime decision submission

Clients should return approval decisions to the runtime as a runtime-owned action, not as direct tool execution.

The minimum client submission shape should be:

```json
{
  "request_id": "approval-1",
  "decision": "allow",
  "note": "approved from web"
}
```

The runtime is responsible for validating that:

- the request still exists
- the request belongs to the active session
- the request has not already been resolved
- execution resumes or terminates according to the recorded decision

## MVP invariants

- approval state belongs to the runtime, not the client
- write/risky tool execution may not bypass the approval contract
- `ask` requires a resumable paused state
- clients must be able to render pending approval vs resolved approval distinctly

## Current vs planned behavior

Current deterministic behavior:
- read-only flow emits `runtime.permission_resolved` with `decision=allow`
- there is no persisted approval request queue yet
- there is no `ask` or `deny` execution path implemented yet

Planned MVP behavior:
- runtime can pause on `ask`
- clients can resolve approvals against runtime state
- persisted sessions can replay approval history and resume correctly

## Persistence and resume expectations

The persisted session state must be able to preserve:

- unresolved approval requests
- resolved approval history
- the final decision associated with each `request_id`
- enough ordering information to replay approval history in sequence

Resume behavior must support both cases:

- unresolved approval request: session resumes in a waiting state and clients can still act on the pending request
- resolved approval request: session replay shows the decision as part of the historical event stream

## Related clients

- CLI may display approval events in text form
- TUI should support direct approval interaction
- web client should render approval state from runtime events and persisted state

## Non-goals

- multi-user approval workflows
- role-based policy systems
- advanced post-MVP approval policy matrices

## Acceptance checks

- a write-capable request can be represented as pending approval before execution
- resumed sessions preserve unresolved or resolved approval state accurately
- clients do not need custom per-client approval logic to interpret the runtime state
