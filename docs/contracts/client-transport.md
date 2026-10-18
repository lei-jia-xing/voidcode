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

## Kernel Transport & Generic Clients (P2 Architecture)

The VoidCode kernel (`src/voidcode/kernel/`) exposes a name-agnostic HTTP/SSE transport (`src/voidcode/kernel/transport/http.py`) designed for zero-coupling with specific product plugins. Clients interact strictly via the command catalog, generic action invocation, bounded declarative UI views, and immutable append-only event streams.

### 1. Authoritative Wire Endpoints

| Method | Path | Summary / Description |
|---|---|---|
| `GET` | `/api/openapi.json` | Authoritative OpenAPI 3.1 specification, embedding all 8 SDK view data models. |
| `GET` | `/api/commands` | Read the admitted command catalog (owner, description, argument schema, effects, capabilities). |
| `GET` | `/api/sessions` | List sessions (`limit` query parameter, defaults to 100). |
| `POST` | `/api/sessions` | Create a new session with optional requested `session_id`. |
| `GET` | `/api/sessions/{session_id}` | Read session record (status, active revision, run ID, turn count). |
| `GET` | `/api/sessions/{session_id}/show` | Inspect durable session state and optionally full historical events (`events=true`). |
| `POST` | `/api/sessions/{session_id}/run` | Execute an agent turn (`prompt`, `session_id`, `max_steps`). |
| `POST` | `/api/sessions/{session_id}/resume` | Resume execution with optional `approval_request_id` and `decision` (`allow`/`deny`). |
| `POST` | `/api/sessions/{session_id}/invoke/{name}` | Invoke an admitted plugin command by catalog name with JSON `arguments`. |
| `POST` | `/api/sessions/{session_id}/confirm` | Resolve a pending confirmation binding stored `request_id` and `decision`. |
| `GET` | `/api/sessions/{session_id}/views` | Render bounded plugin UI descriptors for the session. |
| `POST` | `/api/sessions/{session_id}/continuations` | Drain and execute pending durable continuations with plugin provenance. |
| `GET` | `/api/sessions/{session_id}/usage` | Read truthful session token usage (`since_sequence`, `until_sequence`). |
| `POST` | `/api/sessions/{session_id}/cancel` | Request cancellation for a single session (`{"cancellation_requested": bool}`). |
| `GET` | `/api/sessions/{session_id}/events` | Stream session events over SSE (`after_sequence`, `follow=true/false`). |

**Continuation drive semantics**: durable continuations are executed automatically by the kernel — a command or confirmation that enqueues work while its session is idle drives it in the same call, a completed or stopped run's tail drains at the next safe boundary, and a claimed continuation left by a crash is recovered on the next kernel open against its own durable checkpoint (completed prefix restored, `replay_policy="never"` intents never replayed, tail members never falsely marked executed). The explicit `/continuations` endpoint remains available as a rescue/observability path and returns only what it actually executed.

### 2. The Eight Finite Declarative UI Kinds

Plugins contribute presentation exclusively through 8 strict bounded view descriptors (no remote arbitrary JavaScript execution):

1. **`notice`**: Inline alert message (`NoticeViewData(message, level="info"|"warning"|"error")`).
2. **`markdown`**: Formatted markdown content (`MarkdownViewData(content)`).
3. **`fields`**: Key-value metadata table (`FieldsViewData(rows)` where `FieldRow(label, value)`).
4. **`choices`**: Interactive option picker (`ChoicesViewData(options, action, argument_name="choice")`).
   - Clients display choices; selection dispatches `{ [argument_name]: option.id }` to `action`.
5. **`confirmation`**: Approval gate (`ConfirmationViewData(request_id, message, actions=("allow", "deny"))`).
   - Strictly binds stored `request_id`. Rendering a view cannot gain authority; decisions are submitted via `/confirm`.
6. **`form`**: Interactive multi-field editor (`FormViewData(fields, action, initial_values={}, load_action=None)`).
   - Supports field types: `text`, `multiline`, `integer`, `number`, `boolean`, and `required` validation.
   - When `load_action` is present, clients verify the target command is declared with `effects: ["read"]` before executing it.
   - Preserves unsaved user edits if loaded data arrives asynchronously; displays visible error banners on load failure without truncating or submitting stale text.
7. **`diff`**: Unified code/text diff (`DiffViewData(before, after, path=None)`).
8. **`status`**: State badge indicator (`StatusViewData(label, value)`).

Any unrecognized or malformed view type produces an explicit visible error banner; clients never simulate or pretend success.

### 3. Generic TUI Client (`voidcode.kernel.tui`)

- **Launch Syntax**:
  ```bash
  # Connect to an existing kernel server
  python -m voidcode.kernel tui --server http://127.0.0.1:8000 --token <bearer_token> [--session-id <id>]

  # Standalone embedded mode (starts local server on free loopback port with ephemeral token)
  python -m voidcode.kernel tui [--session-id <id>]
  ```
- **Settled-Prefix Commit-Once Ledger**:
  Rows are committed to the native scrollback exactly once by the main loop, driven by the durable journal (`last_sequence` cursor). Reconnecting SSE streams, follow polling, `SIGWINCH` resizes, and switching sessions never replay, re-render, or clear settled output; only the live region (active overlay or composer) repaints.
- **Canonical Event Rendering**:
  The TUI renders only the persisted SDK event payloads, never provider-native wire shapes:
  - `kernel.user_message` → `payload.content` (single source; the composer never commits the prompt optimistically).
  - `kernel.model_turn` → `payload.result.output` / `payload.result.reasoning` / `payload.result.tool_calls[].tool_name|arguments`.
  - `kernel.tool_completed` / `kernel.tool_interrupted` → `payload.call.tool_name` + `payload.result.output.text` (+ `payload.result.error`).
  - `kernel.command_intent` / `kernel.command_completed` → `payload.name` + `payload.result.{status,output,error,data}` (the neutral `CommandResult` envelope; clients render `status` verbatim and show `unknown` rather than inventing a placeholder).
  Every model- and plugin-provided string is passed through `escape_terminal_controls` before styling, so untrusted text cannot clear scrollback or switch buffers.
- **Interactive Overlays**:
  Choices, forms (full multiline/Unicode editing, `load_action` prefill, strict numeric literals, omitted-blank optionals) and confirmation prompts borrow the live region. A pending confirmation is only ever resolved through `POST /confirm` with its stored `request_id`: `Esc` and `Ctrl+C` on a confirmation overlay submit an explicit `deny`, never a local "cancelled" claim.
- **Async I/O**:
  All HTTP calls run on worker threads and hand results back through the UI queue; only the main loop writes rows. `/session <id>` and `/new` rebind the follower (fresh cursor, drained queue, new SSE generation), so rows from a previous session never leak into the new one. Local controls (`/help`, `/views`, `/session`, `/new`, `/cancel`) never shadow an admitted command with the same name.
- **Markdown**:
  Markdown views render through the same `rich.markdown.Markdown` primitive the transcript uses (headings, lists, tables, code), after control-byte escaping.

### 3b. Native Tool Approval vs. Plugin Action Confirmation

Two distinct gates reach clients over the same event stream and must not be conflated:

| Gate | Event | Resolution |
|---|---|---|
| Native tool approval (core permission gate) | `kernel.approval_requested` → `{request_id, batch_sequence, call:{tool_name,arguments,tool_call_id}, owner}`; session becomes `waiting` | `POST /api/sessions/{id}/resume` with the exact `approval_request_id` + `decision` (`allow`/`deny`); the engine also records `kernel.approval_decided` |
| Plugin Action confirmation | `kernel.confirmation_requested` / a `pending_confirmation` command result carrying `ui_type` + `ui_data` | `POST /api/sessions/{id}/confirm` with the stored `request_id` |

Both clients keep them separate: an approval request id from the core table is never sent to `/confirm`, and a plugin confirmation request id is never sent to `/resume`. Interactive traffic (approval, confirmation, cancel, form load, run) always leaves the UI thread and returns through the UI queue, so only the main loop writes rows.

### 3c. Proof Scope

The client surfaces were proven against real transports, a real browser and a real PTY with the full bundled plugin set (`files` + `goal`) and native approvals:

- Browser (Chromium, real uvicorn, external fixture plugin, bundled `files` and `goal`, real loopback provider): all eight view kinds with real markdown, slash dispatch, choices, stored plugin confirmation allow **and** deny, native ASK approval allow (real write effect) **and** deny (no effect, failed tool result), tri-state boolean submission read back from the durable command result, the long-Unicode goal objective pre-filled through its declared read action with no retry, an untouched submit keeping the finite budget, and a deliberate blank submitting `token_budget: null` on the real request body while the goal keeps its id, creation record and attached objective.
- PTY (real pseudo-terminal, real kernel server): the same view kinds, form prefill and submit, SIGWINCH resize and server-restart reconnect with zero settled replays, sequential prompt identities, native approval allow/deny, stored confirmation allow/deny, cancel over HTTP, A→B→A with zero replayed rows, and the real Goal objective/budget flow.

The throwaway harnesses are kept outside the repository (evidence and copies under `/tmp/voidcode-kernel-evidence/`); nothing in this document depends on them.

### 4. Generic Web Client (`frontend/src/kernel/`)

- **Coexistence**:
  Accessible at `http://127.0.0.1:5173/?kernel` or `/kernel`. Coexists alongside legacy `App` until the P4 cutover.
- **Components**:
  Pure React components in `frontend/src/kernel/views/` (`NoticeView`, `MarkdownView`, `FieldsView`, `ChoicesView`, `ConfirmationView`, `FormView`, `DiffView`, `StatusView`, `PluginViewRenderer`).
  - Markdown renders through the same `react-markdown` + `remark-gfm` pipeline the transcript uses (raw HTML is never evaluated).
  - Client chrome, button labels and error text use the shared i18n resources (`kernel.*` keys in `frontend/src/i18n/locales/{en,zh-CN}.json`); plugin-provided business data is rendered verbatim.
  - A form's `load_action` is only auto-invoked when its catalog entry declares exactly the `read` effect.
  - Blank values follow the target command's own `argument_schema`: a field the command declares nullable submits `null` (so an existing value can be cleared), an optional field without `null` is omitted, and a required field is rejected. Numeric text is validated, never truncated or coerced to `0`.
  - Every response, view render, form draft and SSE frame is bound to the session that issued it; switching sessions resets the transcript, pending confirmation and run state, and remounts forms keyed by session.
- **Type Generation**:
  Authoritative TypeScript definitions are generated from the kernel transport's live OpenAPI document:
  ```bash
  mise run frontend:kernel-api:generate
  mise run frontend:kernel-api:check
  ```
