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

From `src/voidcode/runtime/service.py` and the deterministic read-only slice:

- `runtime.request_received`
- `runtime.skills_loaded`
- `graph.tool_request_created`
- `runtime.tool_lookup_succeeded`
- `runtime.permission_resolved`
- `runtime.tool_completed`

Additional graph finalization events may be emitted by the graph layer and are part of the same ordered stream.
When that happens, the runtime assigns their final `sequence` values after the preceding runtime event rather than preserving any graph-local hardcoded sequence numbers.

## Frozen additive vocabulary for future prototype graph modes

These shared event names are frozen now in `src/voidcode/runtime/events.py`, but they are not emitted by the current deterministic fallback runtime yet:

- `graph.model_turn`
- `graph.loop_step`
- `runtime.memory_refreshed`

Future graph modes may add ordered events between existing phases. The deterministic fallback sequence below remains canonical for current behavior.

## Current integration-test event sequence

The current deterministic read-only integration tests assert this ordered sequence:

1. `runtime.request_received`
2. `runtime.skills_loaded`
3. `graph.tool_request_created`
4. `runtime.tool_lookup_succeeded`
5. `runtime.permission_resolved`
6. `runtime.tool_completed`
7. `graph.response_ready`

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
