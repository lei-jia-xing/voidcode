# Client Transport Contract

This document defines how clients (the kernel CLI, the inline TUI, and the web client) talk to a VoidCode kernel. There is exactly one contract: the generic kernel API plus the command catalog, declarative views, and append-only event stream. No client surface has product-specific routes.

## Surfaces

| Surface | Entry | Transport |
|---|---|---|
| Kernel CLI | `python -m voidcode.kernel <subcommand>` (also the `voidcode` console script) | elected image for one-shot commands (re-executed when needed), or HTTP via the TUI's remote mode |
| TUI | `python -m voidcode.kernel [--server URL --token TOKEN --session-id ID]` (bare command) | HTTP/SSE against a supervisor child process by default (same behavior as `--server`), or a remote kernel |
| Web | built React bundle served by the kernel at `/` | HTTP/SSE, same origin; `/api` requires the bearer token |
| HTTP | `python -m voidcode.kernel serve [--host --port --bearer-token --session-id] [--open]` | supervised FastAPI app, loopback bind only; the listener and token survive kernel replacement |

The kernel binds `127.0.0.1`, `::1`, or `localhost` only. Every request under `/api` requires `Authorization: Bearer <token>`; the supervisor generates the token once per `serve` lifetime unless `--bearer-token` supplies one, and it is printed to stdout at startup and nowhere else. Static files outside `/api` are unauthenticated (they are the client bundle), and a request path under `/api` can never be served as a static file. Errors use one envelope: `{"error": "<message>", "code": "<code>"}`.

## Kernel CLI

| Subcommand | Purpose | Exit code |
|---|---|---|
| `run "<prompt>" [--session-id] [--max-steps]` | Execute one agent turn; prints the `KernelResult` JSON. | `0` ok, `1` failed, `130` interrupted |
| `resume <session_id> [--approval-request-id --decision allow\|deny] [--max-steps]` | Continue a session, optionally resolving one native tool approval. | same as `run` |
| `create-session [--session-id]` | Create an empty session. | `0` |
| `sessions [--limit]` | List this workspace's sessions. | `0` |
| `show <session_id> [--events]` | Read durable state (and optionally the raw event history) without configuration, host, or revision checks. | `0` |
| `catalog` | Print the admitted command catalog (`revision_id` + commands). | `0` |
| `invoke <session_id> <name> --arguments JSON --expected-revision ID [--client-capabilities]` | Invoke one admitted command. | `0` ok, `2` pending confirmation, `1` error |
| `confirm <session_id> <request_id> --decision allow\|deny --expected-revision ID [--client-capabilities]` | Resolve a stored plugin confirmation. | `0` ok, `2` pending confirmation, `1` error |
| `views <session_id> [--client-capabilities]` | Print the rendered plugin views envelope. | `0` |
| `drain <session_id> [--limit]` | Execute pending durable continuations. | `0` |
| `usage <session_id> [--since-sequence --until-sequence]` | Print the truthful session usage report. | `0` |
| *(bare command)* `[--server --token --session-id]` | Start the TUI (supervisor child process unless `--server` is given). | — |
| `serve [--host --port --bearer-token --session-id] [--open]` | Run the supervisor: stable endpoint and token across kernel replacement (`--open` launches a browser; `--session-id` attributes a first-adoption transition). | — |
| `upgrade [run\|status] --candidate PATH [--session-id ID] [--timeout S]` | Ask the running supervisor for its status, or upgrade this workspace to a candidate installation (one JSON object; exit `0` only on `upgraded`/`ok`). | `0` ok, `1` refused/blocked/failed |
| `preflight` | Read-only candidate check for this installation against the live state (one JSON object; exit `1` on a named refusal). | `0` ok, `1` refused |

Root options: `--workspace PATH` (default: current directory), `--state-dir PATH`, `--trust-plugin PLUGIN_ID` (repeatable). `invoke` arguments must be a JSON object; `--client-capabilities` is a comma-separated list of capability names. Every failure (including malformed arguments, refused trust, or a missing config) prints `{"status": "failed", "error": "..."}` and exits non-zero.

## HTTP API

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/openapi.json` | The API as OpenAPI 3.1, including the eight view data models. |
| `GET` | `/api/commands` | Admitted command catalog: one envelope (`revision_id` + `commands`) captured from one immutable snapshot. |
| `GET` | `/api/sessions?limit=N` | List sessions for this workspace (default 100). |
| `POST` | `/api/sessions` | Create a session (optional requested `session_id`); `201`. |
| `GET` | `/api/sessions/{session_id}` | Session record: status, active revision, run id, turn, last event sequence. |
| `GET` | `/api/sessions/{session_id}/show?events=true` | Durable session state plus optional full event history. |
| `POST` | `/api/sessions/{session_id}/run` | Execute an agent turn (`prompt`, `session_id`, `max_steps`). |
| `POST` | `/api/sessions/{session_id}/resume` | Resume execution, optionally with `approval_request_id` and `decision` (`allow`/`deny`). |
| `POST` | `/api/sessions/{session_id}/invoke/{name}` | Invoke an admitted command with JSON `arguments` and the required `expected_revision`. |
| `POST` | `/api/sessions/{session_id}/confirm` | Resolve a stored confirmation by `request_id`, `decision`, and the required `expected_revision`. |
| `GET` | `/api/sessions/{session_id}/views?client_capabilities=` | Render plugin views: one envelope (`revision_id` + `views`); `client_capabilities` is a comma-separated list of capability names. |
| `POST` | `/api/sessions/{session_id}/continuations` | Drain and execute pending durable continuations. |
| `GET` | `/api/sessions/{session_id}/usage?since_sequence=&until_sequence=` | Truthful token usage for the session. |
| `POST` | `/api/sessions/{session_id}/cancel` | Request cancellation for one session. |
| `GET` | `/api/sessions/{session_id}/events?after_sequence=&follow=` | Replay (and optionally follow) the session event stream over SSE. |
| `GET` | `/api/health` | Kernel health and identity: fence, stage, revision, SDK/image pins, generation, active upgrade. |

## Revision Binding

A command advertised by one capability generation is never executed by another:

- `GET /api/commands` and `GET /api/sessions/{id}/views` each return their descriptors together with the `revision_id` they were captured from; the two are separate envelopes.
- `POST /invoke/{name}` and `POST /confirm` require `expected_revision`. A missing or empty value fails ingress validation (`400`, `code: "invalid_params"`); a value that is not the active revision is refused before any lookup or dispatch with `409`, `code: "stale_revision"`. There is no "use whatever is current" default and no automatic retry: the client refreshes and the user retries.
- Every rendered form, choice, command, and read-only `load_action` dispatches with the revision of the envelope that rendered it; a pending confirmation keeps the revision it was displayed under.
- `kernel.reload` (a reserved core command) activates the configured sources on explicit user request and emits `kernel.activation_committed` with `{request_id, source, from_revision_id, to_revision_id, migrated_owners, deactivated_owners}`. Clients drop anything captured under another revision. The web client refetches the catalog and the views together; the TUI refetches the catalog without fetching views it does not display (an explicit `/tui views` envelope binds its own revision). `kernel.activation_requested`, `kernel.activation_failed` (nothing changed), and `kernel.activation_blocked` (with `blockers`) complete the family.

## Event Stream (SSE)

`GET /api/sessions/{id}/events` returns `text/event-stream`. Each frame is `data: <json>\n\n`.

- **Head frame** (always first): `{"session": <session record>, "events": [<stored event>, ...]}` — the session record plus everything already appended after `after_sequence` (default `0`; negative values are rejected).
- **Follow frames** (`follow=true` only): `{"kind": "event", "session_id": ..., "event": {<stored event>}}` as new events are appended. A backend failure during follow yields `{"kind": "error", "session_id": ..., "error": "..."}` and ends the stream.
- **Restart frame** (after a successful upgrade drain only): `{"kind": "server_restart", "session_id": ..., "cursor": <last sequence this stream delivered>}`, following a flush of the final backlog. Clients reconnect with `after_sequence` set to their own applied cursor (never advancing it from the frame) and never resend a prompt; a failed drain leaves the stream connected with no frame.
- **Stored event fields**: `session_id`, `sequence` (monotonic within the session), `type` (for example `kernel.user_message`, `kernel.model_turn`, `kernel.tool_completed`, `kernel.approval_requested`, `kernel.confirmation_requested`, `kernel.command_completed`), `run_id`, `revision_id`, `source`, `actor`, `payload`.

`follow=false` (default) replays and closes. `follow=true` crosses run boundaries: `completed`, `failed`, and `interrupted` end a *run*, not the session, so the stream ends only when the client disconnects, a terminal error frame is sent, or a successful upgrade drain ends it with the restart frame above. While a kernel is fenced for an upgrade, business writes answer `503` with `code: "upgrade_pending"` and reads keep working; `cancel` stays allowed because it can only reduce work. The inline TUI owns a cancellable connection to this stream and shuts it down on quit before joining its reader and before an embedded supervisor stops the kernel; idle follow streams need no heartbeat and are woken by that socket shutdown.

## Declarative Views

Plugins contribute presentation exclusively through eight bounded descriptors; clients never execute plugin-supplied markup or script:

1. `notice` — inline alert (`message`, `level` ∈ `info`/`warning`/`error`).
2. `markdown` — formatted markdown content.
3. `fields` — key/value rows (`FieldRow(label, value)`).
4. `choices` — option picker (`options`, `action`, `argument_name`); selection dispatches `{ "<argument_name>": <option id> }` to `action`.
5. `confirmation` — approval gate bound to a stored `request_id`; decisions go through `/confirm`, never a local claim.
6. `form` — multi-field editor (`fields`, `action`, optional `initial_values` and `load_action`); field types `text`, `multiline`, `integer`, `number`, `boolean`, with `required` validation. A `load_action` is only auto-invoked when the catalog declares that command with exactly the `read` effect.
7. `diff` — unified diff (`before`, `after`, optional `path`).
8. `status` — state badge (`label`, `value`).

An unrecognized or malformed view is reported visibly; clients never simulate success.

## The Two Decision Gates

| Gate | Request fact | Resolution |
|---|---|---|
| Native tool approval | `kernel.approval_requested` carries the approval `request_id`, the batch/call identity, and the owner; the session parks in `waiting`. | `POST /resume` with the exact `approval_request_id` and `decision`; the engine records `kernel.approval_decided`. |
| Plugin action confirmation | `kernel.confirmation_requested` carries the SDK `ConfirmationRequest` (`request_id`, `owner`, `command_name`, `arguments`, `ui_type`, `ui_data`, and optionally `expected_state_version`); `kernel.confirmation_decided` clears exactly that request. | `POST /confirm` with the stored `request_id`, the decision, and the revision the client displayed. |

A request id from one gate is never sent to the other endpoint. Both are projected from durable facts, so a client restart or a server restart restores a pending dialog without the command being re-issued, and a replayed decided request never resurrects one.

## TUI Client

- Launched embedded (spawns a supervisor child process and talks to it over HTTP exactly like `--server` mode, so a kernel replacement never kills the UI) or remote with `--server`/`--token`.
- Inline scrollback with a **commit-once ledger**: rows are committed to the native scrollback exactly once by the main loop, driven by a durable `last_sequence` cursor. Committed text wraps to terminal cell width without dropping long output; live input/dialog geometry remains bounded. Reconnects, follow polling, resizes, and session switches never replay or clear settled output; only the live region repaints.
- Renders durable event payloads (`kernel.user_message`, `kernel.model_turn`, `kernel.tool_completed`, `kernel.command_intent`/`kernel.command_completed`) plus the direct `CommandResult` of core read-only commands (`help`, `status`, `show`), which have no command event. Plugin and run/resume results remain event-projected; one shared formatter renders each result once, preserving multi-line output and falling back to JSON for data-only receipts. Every model- and plugin-provided string is escaped for terminal control bytes.
- Startup, connect, reconnect, session switch, and capability activation never fetch or display optional plugin views: the TUI reconciles health, the catalog, durable session state, and the event-projected decision gates only. `/tui views [name]` explicitly fetches a revision-bound views envelope and displays passive content inline and borderless — passive field values wrap rather than ellipsize (deliberate plugin-level previews/limits still apply) — or an interactive form/choices/confirmation as a framed dialog. The two decision gates above remain event-projected and are never closed or denied by a refresh.
- Idle chrome carries no session, revision, or request hashes and no Attached/Created banners; the transient status line shows meaningful progress, errors, gates, fence state, and command status only. A real command completion updates it from its actual `status`; explicit `/status`, `/show`, `/session_status`, and `/tui sessions` keep diagnostics and ids.
- Slash input resolves against the displayed catalog and echoes its revision; overlays capture the revision that rendered them. `Esc`/`Ctrl+C` on a confirmation overlay submit an explicit `deny`, never a local "cancelled".
- Local controls that are not admitted commands: `/tui help|sessions|session <id>|new|views [name]|cancel|quit` (bare forms work when no admitted command owns the name). `/tui <control>` always reaches the client control, so a plugin cannot shadow navigation.
- All HTTP work happens on worker threads; only the main loop writes rows.

## Web Client

The built bundle is served by the kernel at `/` (an exact file when present, otherwise `index.html`, so client-side routes survive a reload). During frontend development, Vite serves the app on `127.0.0.1:5173` and proxies `/api` to the kernel (`VITE_BACKEND_URL`, default `http://127.0.0.1:8765`).

The kernel app (`frontend/src/kernel/`) renders the same catalog and views with pure components in `frontend/src/kernel/views/`; markdown goes through `react-markdown` + `remark-gfm` and never evaluates raw HTML. Every response, view render, form draft, and SSE frame is bound to the session that issued it, and revision binding follows the HTTP rules above.

TypeScript types are generated from the kernel transport's OpenAPI document:

```bash
mise run frontend:api:generate
mise run frontend:api:check
```

Do not hand-edit `frontend/src/lib/kernel/generated/api.d.ts`.
