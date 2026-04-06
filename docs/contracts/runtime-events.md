# Runtime Event Schema

Source issue: #13

## Purpose

Define the MVP event vocabulary emitted by the runtime for client rendering.

## Status

This schema documents the current single-agent MVP contract. It is intentionally narrower than any future multi-agent protocol.
The deterministic fallback sequence remains canonical for the current runtime, while future graph modes may add ordered events between existing phases without changing current fallback behavior.
For both fresh runs and approval resumes, the runtime renumbers graph finalization events into the active runtime sequence so graph-side fixed sequence values cannot collide with inserted runtime events.

## Canonical envelope

Current in-code shape from `src/voidcode/runtime/events.py`:

```python
EventEnvelope(
    session_id: str,
    sequence: int,
    event_type: str,
    source: Literal["runtime", "graph", "tool"],
    payload: dict[str, object],
)
```

## Field rules

- `session_id`: required; identifies the owning session
- `sequence`: required; monotonically increasing within a session response or replay
- `event_type`: required; string identifier for the event kind
- `source`: required; one of `runtime`, `graph`, or `tool`
- `payload`: required as a field, may be an empty object

## MVP invariants

- events are session-scoped
- events are ordered by `sequence`
- clients must preserve event order when rendering a turn or replay
- clients must tolerate unknown `event_type` values by rendering them generically rather than failing
- clients must treat `payload` as extensible

## Known event types emitted today

From `src/voidcode/runtime/service.py` and the stable single-agent loop:

- `runtime.request_received`
- `runtime.skills_loaded`
- `graph.loop_step`
- `graph.model_turn`
- `graph.tool_request_created`
- `runtime.tool_lookup_succeeded`
- `runtime.approval_requested`
- `runtime.approval_resolved`
- `runtime.permission_resolved`
- `runtime.tool_completed`
- `runtime.failed`
- `graph.response_ready`

All events emitted during a turn, including those from the graph, are re-sequenced by the runtime into a single monotonically increasing sequence per response or replay.
This ensures that graph-local sequence values cannot collide with runtime-inserted events across approval-resumes.

## Additive vocabulary for future multi-agent modes

These shared event names are defined in `src/voidcode/runtime/events.py`, but they are not emitted by the current single-agent loop yet:

- `runtime.memory_refreshed`

## Current single-agent loop event sequence

The runtime and integration tests assert this ordered sequence for a turn with a single approved tool:

1. `runtime.request_received`
2. `runtime.skills_loaded`
3. `graph.loop_step`
4. `graph.model_turn`
5. `graph.tool_request_created`
6. `runtime.tool_lookup_succeeded`
7. `runtime.approval_requested` (for `ask` policy) OR `runtime.approval_resolved` (for `allow`/`deny` policy) OR `runtime.permission_resolved` (for read-only)
8. `runtime.approval_resolved` (only if resumed after `ask`)
9. `runtime.tool_completed`
10. `graph.loop_step`
11. `graph.response_ready`

This sequence is the most concrete client-visible MVP event flow implemented today.
Future graph modes may add ordered events between these phases, but this fallback order remains the canonical deterministic sequence.

## Current payload expectations

### `runtime.request_received`
- source: `runtime`
- current payload:
  - `prompt: str`

### `runtime.skills_loaded`
- source: `runtime`
- current payload:
  - `skills: list[str]` sorted ascending by skill name
- emitted for every new run, including when no skills are discovered (`{"skills": []}`)

### `graph.tool_request_created`
- source: `graph`
- current payload:
  - `tool: str`
  - `path: object`

### `runtime.tool_lookup_succeeded`
- source: `runtime`
- current payload:
  - `tool: str`

### `runtime.permission_resolved`
- source: `runtime`
- current payload:
  - `tool: str`
  - `decision: str`

### `runtime.tool_completed`
- source: `tool`
- current payload:
  - tool-defined result data

## Client rendering requirements

- CLI may render events as formatted lines
- TUI and web clients should render the ordered stream as timeline/activity data
- clients should not infer approvals, failures, or tool completion from text output alone when event data is available

## Non-goals

- multi-agent event semantics
- token/cost telemetry schema
- provider-specific model reasoning events

## Acceptance checks

- a client can replay a persisted session using only the stored event sequence and output
- event ordering is sufficient to show request → skills loaded → tool request → permission → tool completion → response ready
- adding a new event type does not break older clients that use generic fallback rendering
