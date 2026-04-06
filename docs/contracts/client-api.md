# Client-Facing Runtime API Contract

Source issue: #14

## Purpose

Define the MVP contract between clients and the headless runtime for running requests, listing sessions, loading session state, resuming sessions, and subscribing to event streams.

## Status

The current codebase exposes this contract concretely through the CLI and runtime methods, not through HTTP yet.

## Current runtime request/response shapes

From `src/voidcode/runtime/contracts.py`:

```python
RuntimeRequest(
    prompt: str,
    session_id: str | None = None,
    metadata: dict[str, object] = {},
)

RuntimeResponse(
    session: SessionState,
    events: tuple[EventEnvelope, ...] = (),
    output: str | None = None,
)
```

## Session shapes

From `src/voidcode/runtime/session.py`:

```python
SessionState(
    session: SessionRef(id: str),
    status: Literal["idle", "running", "waiting", "completed", "failed"],
    turn: int,
    metadata: dict[str, object],
)

StoredSessionSummary(
    session: SessionRef(id: str),
    status: SessionStatus,
    turn: int,
    prompt: str,
    updated_at: int,
)
```

## MVP client operations

### Run request

Input:
- `prompt`
- optional `session_id`
- optional client/runtime metadata

Output:
- final `session`
- ordered `events`
- final `output`

Current implementation surface:
- runtime: `VoidCodeRuntime.run(request)`
- CLI: `voidcode run <request> [--workspace] [--session-id]`

### List persisted sessions

Output:
- tuple/list of `StoredSessionSummary`

Current implementation surface:
- runtime: `VoidCodeRuntime.list_sessions()`
- CLI: `voidcode sessions list [--workspace]`

### Resume persisted session

Input:
- `session_id`

Output:
- stored `RuntimeResponse` for that session replay

Current implementation surface:
- runtime: `VoidCodeRuntime.resume(session_id)`
- CLI: `voidcode sessions resume <session_id> [--workspace]`

## Session lifecycle

MVP lifecycle:

1. client submits a run request
2. runtime creates or reuses a session id
3. runtime emits ordered events during the turn
4. runtime finalizes a response
5. runtime persists session summary, events, and output
6. client may later list or resume the session

## Current persisted session behavior

The current implementation persists enough data for:

- `sessions list` to return `StoredSessionSummary`
- `sessions resume <id>` to replay the stored response

Current integration tests verify that resume returns the stored output and the stored event sequence for the session.

## API invariants

- clients must treat the runtime as the system boundary
- clients do not call tools directly
- clients do not invent private session state that diverges from persisted runtime state
- resume returns a replayable stored response, not an inferred reconstruction from UI state
- clients must preserve ordered runtime events as delivered, including additive future event types inserted by later graph modes between existing phases
- clients must tolerate additional ordered additive events without assuming the deterministic fallback event list is exhaustive

## Future HTTP/streaming mapping

When the HTTP layer exists, it should preserve these same operation boundaries:

- run/create session
- list sessions
- load/resume session
- subscribe to or receive ordered runtime events

This document intentionally defines the contract independently of FastAPI/Starlette routing details.
The deterministic fallback event sequence remains canonical today, while future graph modes may add ordered events between existing phases without changing these API boundaries.

## Non-goals

- full transport implementation
- post-MVP multi-agent session topology
- provider-specific request formats

## Acceptance checks

- TUI and web clients can be implemented without bypassing runtime methods or concepts
- persisted sessions can be listed and resumed using a stable session summary and stored response shape
- future API routes can map directly onto these operations without changing semantics
