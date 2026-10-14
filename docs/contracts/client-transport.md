# Client Transport Contract

This document defines the transport layer contracts connecting clients (CLI, TUI, Web, ACP) to the VoidCode runtime.

## Transport Overview

VoidCode exposes its runtime service through multiple client adapters:

- **CLI (`voidcode run`, `sessions`, `tasks`)**: Synchronous command-line execution and state management.
- **TUI (`voidcode tui`)**: Interactive terminal interface with native scrollback.
- **HTTP Server (`voidcode serve`) / Web Launcher (`voidcode web`)**: FastAPI-based server in `src/voidcode/runtime/transport/http.py`.
- **ACP Facade (`voidcode acp`)**: External stdio JSON-RPC facade for editor integrations.

## Server-Sent Events (SSE) Protocol

The runtime streams execution events using Server-Sent Events over HTTP. Each frame is formatted with standard SSE framing:
```text
data: <json_payload>\n\n
```

### 1. Active Run Streaming: `POST /api/runtime/run/stream`
Initiates or continues an execution turn. Each frame conforms to `RunStreamFrameBody` (`src/voidcode/runtime/transport/http_models.py`):
- `kind: "event"`: Contains an `event` field with the serialized `EventBody`.
- `kind: "output"`: Contains text content emitted incrementally as chunks or as final text (`output: str | null`). The output frame itself carries text content and does not indicate turn or run completion.

Request payload fields (`_RunStreamRequestPayload` in `src/voidcode/runtime/transport/http_contract.py`):
- `prompt`: Required non-empty string.
- `session_id`: Optional string targeting an existing session.
- `parent_session_id`: Optional parent session identifier.
- `metadata`: Optional dictionary of request metadata.
Note: Extra properties (such as `workspace`) are rejected by schema validation. Workspace context is bound by the HTTP server at startup.

### 2. Session Replay & Live Following: `GET /api/sessions/{session_id}/events`
Streams the event history for a persisted session. Each frame conforms to `SessionEventFrameBody`:
- **Initial Frame**: Sent with `kind: "session"`, carrying session metadata, workspace, status, and turn counter.
- **Subsequent Frames**: Sent with `kind: "event"`, carrying `EventBody` items (`output` is `null`).
- **Follow Modes**:
  - `follow=false` (default): Emits initial session state and replays stored event history, then closes the connection.
  - `follow=true`: Replays stored history and then continues polling, streaming newly appended events until the session reaches terminal status.
  - `after_sequence=N`: Filters events to those with sequence numbers strictly greater than `N` (defaults to `0`; negative values are rejected).

### EventBody Structure

Every event payload in an event frame contains these core properties:

| Field | Type | Description |
|---|---|---|
| `session_id` | `string` | The session identifier. |
| `sequence` | `integer` | Monotonic event sequence number within the session. |
| `event_type` | `string` | The event type (e.g., `runtime.request_received`, `core.model_turn`, `core.response_ready`, `runtime.tool_started`, `runtime.tool_completed`). |
| `source` | `string` | Originating subsystem (`runtime`, `core`, or `tool`). |
| `payload` | `object` | Event-specific data dictionary. |
| `delegated_lifecycle` | `object` | Optional delegation correlation metadata (null for top-level events). |

### Example SSE Frame Sequence

```text
data: {"kind":"event","event":{"session_id":"sess_123","sequence":1,"event_type":"runtime.request_received","source":"runtime","payload":{"prompt":"Inspect README.md"},"delegated_lifecycle":null},"output":null}

data: {"kind":"output","event":null,"output":"I will inspect "}

data: {"kind":"output","event":null,"output":"the README file."}

data: {"kind":"event","event":{"session_id":"sess_123","sequence":2,"event_type":"core.response_ready","source":"core","payload":{"output_preview":"I will inspect the README file."},"delegated_lifecycle":null},"output":null}
```

Run completion is not signaled by an artificial completion event in the stream; clients determine terminal completion from the persistent session status (`completed`, `failed`, `interrupted`) via `GET /api/sessions/{session_id}` or when following finishes at terminal status.

## Lifecycle & State Distinctions

Client integrations must observe these technical distinctions:

1. **`core.response_ready` vs. Run Completion**:
   `core.response_ready` signals that a single model generation turn finished. It is *not* run completion; the runtime may proceed to evaluate tool calls and execute further turns.
2. **Output Frames vs. Run Completion**:
   Arrival of an output frame (`kind: "output"`) represents a streamed text chunk, not run completion.
3. **SSE Connection EOF vs. Success**:
   A closed SSE connection indicates that the HTTP response stream ended (due to completion, disconnect, or timeout). Run success is determined solely by the persistent session status.
4. **Terminal Session States**:
   A run is complete only when the session reaches a terminal status:
   - `completed`: Successfully finished its plan.
   - `failed`: Terminal execution failure.
   - `interrupted`: Terminated prior to normal completion (such as user cancellation, process exit, or session overlap).
5. **`waiting` Status Semantics**:
   When an execution encounters an approval gate (`runtime.approval_requested`) or question (`runtime.question_requested`), the session pauses in the `waiting` status. It remains in `waiting` until resolved via approval or question answer endpoints.

## Source-Generated Wire Types

The live OpenAPI specification is served at:
```text
GET /api/openapi.json
```

Frontend TypeScript types are automatically compiled from this document:

```bash
# Regenerate TypeScript wire models
mise run frontend:api:generate

# Verify committed types match backend models
mise run frontend:api:check
```

Do not edit `frontend/src/lib/runtime/generated/api.d.ts` manually.

## Common Client Interaction Flows

### 1. Execute and Stream a Run
```bash
curl -N -X POST http://127.0.0.1:8765/api/runtime/run/stream \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Summarize README.md"}'
```

### 2. Replay and Follow Session Events
```bash
# Replay stored events and disconnect
curl http://127.0.0.1:8765/api/sessions/<session_id>/events

# Stream live events following sequence 0
curl -N "http://127.0.0.1:8765/api/sessions/<session_id>/events?follow=true&after_sequence=0"
```

### 3. Resolve Pending Approval
When a session is `waiting` for tool execution approval:
```bash
curl -X POST http://127.0.0.1:8765/api/sessions/<session_id>/approval \
  -H "Content-Type: application/json" \
  -d '{"request_id": "<request_id>", "decision": "allow"}'
```

### 4. Resume an Interrupted Session
```bash
curl -X POST http://127.0.0.1:8765/api/sessions/<session_id>/resume
```
