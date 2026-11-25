# Client Transport Contract

VoidCode has one typed `SessionClient` contract over one core authority. The
local TUI calls the core directly. Remote TUI and web clients use JSON-RPC 2.0
on a persistent WebSocket; there are no business HTTP routes or SSE streams.

## Surfaces

| Surface | Transport |
|---|---|
| One-shot CLI | Direct core invocation; rescue `show` can read SQLite without a core. |
| Local TUI | `CoreLocalClient`, implementing the SDK `SessionClient`. |
| Remote TUI | `CoreRpcClient`, implementing the same contract over WebSocket. |
| Web | Browser `CoreClient`, using the same RPC methods and session frames. |
| HTTP | Static assets at `/`, authenticated `/api/health` and `/api/openapi.json` only. |

`serve` binds loopback only, including dynamic port 0 and bracketed IPv6 URLs.
HTTP `/api` requests use `Authorization: Bearer <token>`. The web bundle itself
is public. Business paths under `/api` return 404 and never fall through to the
SPA. Explicit `allow_origin` restrictions apply to both HTTP and WebSocket.

## Core CLI

| Subcommand | Purpose | Exit code |
|---|---|---|
| `run "<prompt>" [--session-id] [--max-steps] [--root-action-budget]` | Execute one agent turn; prints the `CoreResult` JSON. | `0` ok, `1` failed, `130` interrupted |
| `resume <session_id> [--approval-request-id --decision allow\|deny] [--max-steps] [--extend-root-actions]` | Continue a session, optionally resolving approval or adding explicit finite authorization. | same as `run` |
| `create-session [--session-id]` | Create an empty session. | `0` |
| `sessions [--limit]` | List this workspace's sessions. | `0` |
| `show <session_id> [--events]` | Read durable state (and optionally the raw event history) without configuration, runtime, or revision checks. | `0` |
| `catalog` | Print the admitted command catalog (`revision_id` + commands). | `0` |
| `invoke <session_id> <name> --arguments JSON --expected-revision ID` | Invoke one admitted command. | `0` ok, `2` pending confirmation, `1` error |
| `confirm <session_id> <request_id> --decision allow\|deny --expected-revision ID` | Resolve a stored plugin confirmation. | `0` ok, `2` pending confirmation, `1` error |
| `views <session_id>` | Print the rendered plugin views envelope. | `0` |
| `drain <session_id> [--limit]` | Execute pending durable continuations. | `0` |
| `usage <session_id> [--since-sequence --until-sequence]` | Print the truthful session usage report. | `0` |
| *(bare command)* `[--server --token --session-id]` | Start the TUI (direct local core unless `--server` is given). | — |
| `serve [--host --port --bearer-token] [--open]` | Run the WebSocket RPC server plus static web client at `/` (`--open` launches a browser). | — |

Root options: `--workspace PATH` (default: current directory), `--state-dir PATH`. `invoke` arguments must be a JSON object. Every failure (including malformed arguments or a missing config) prints `{"status": "failed", "error": "..."}` and exits non-zero.

`max_steps` caps provider dispatches only. A fresh omitted cap is unbounded;
omitting it on resume retains the checkpoint's existing total cap. An explicit
resume cap replaces that total, so it must exceed already consumed dispatches
to allow progress. `root_action_budget` is separate finite authorization shared
across descendant/continuation actions. `--extend-root-actions` appends an
explicit grant; the CLI supplies its unique grant id. SDK/RPC callers supply
both `extend_root_actions` and idempotent `root_action_grant_id`. Exhaustion is
an interrupted non-success with typed `BudgetExhaustion`, not cancellation.

## WebSocket and JSON-RPC

Connect to `/api/rpc` with subprotocol `voidcode.rpc.v1`. Within ten seconds,
send a request `connection.authenticate` with `{ "token": "..." }`; its result
is `{ "protocol_version": 1 }`. Browser credentials travel in this first message,
never in the WebSocket URL. Authentication failure closes with 1008, without
starting a business call. The same versioned contract applies to Python clients.

Requests carry `jsonrpc: "2.0"`, `id`, `method`, and named-object `params`.
Responses carry the same `id` and exactly one of `result` or `error`. IDs are
correlation identifiers, not durable idempotency keys. JSON-RPC notifications
omit `id` and receive no reply, including on error. Batches contain 1..32 calls;
notification entries are omitted from the response array. Calls on a connection
execute concurrently, so a long run never blocks session cancellation.

| Method | Parameters / result |
|---|---|
| `command.catalog` | `{}` → catalog with `revision_id`. |
| `session.list` | Optional `limit` → `sessions`. |
| `session.create` | Optional `session_id` → session record. |
| `session.get` | `session_id` → session record. |
| `session.show` | `session_id`, optional `events` → durable state. |
| `session.run` | `session_id`, `prompt`, optional `max_steps`, `root_action_budget`, `images` → run result. |
| `session.resume` | `session_id`, optional paired `approval_request_id`/`decision`, `max_steps`, paired `extend_root_actions`/`root_action_grant_id` → run result. |
| `command.input` | `session_id`, `name`, `expected_revision`, optional `arguments` → read-only prepared input with its captured revision. |
| `command.invoke` | `session_id`, `name`, `arguments`, `expected_revision` → command result. |
| `workspace.complete_paths` | `session_id`, optional `query`/`mode`/`depth`/`limit`/`include_hidden`/`respect_gitignore` → `{answer}` naming the files plugin's `workspace.path_completion` result; a null answer means the provider knows no matching resource. Missing providers surface `data.code=query_unavailable`; malformed answers `data.code=query_contract`. |
| `action.confirm` | `session_id`, `request_id`, `decision`, `expected_revision` → command result. |
| `session.views` | `session_id` → views with `revision_id`. |
| `session.drain` | `session_id`, optional `limit` → `continuations`. |
| `session.usage` | `session_id`, optional sequence bounds → usage report. |
| `session.transcript` | `session_id`, optional `after_sequence`/`limit` → bounded transcript page. |
| `session.context` | `session_id`, optional `limit` → bounded context prefix. |
| `session.cancel` | `session_id` → cancellation receipt for active run/commands and parked work. |
| `session.subscribe` | Client-chosen `subscription_id`, `session_id`, optional `after_sequence`/`follow` → subscription receipt. |
| `session.ack` | `subscription_id`, `through_sequence` → acknowledged cursor; usually a notification. |
| `session.unsubscribe` | `subscription_id` → `removed`; never cancels the session's execution. |
| `artifact.read` | `sha256`, optional byte `offset`/`limit` → immutable `artifact`, `offset`, base64 `data`, `eof`. |

`workspace.complete_paths` is always answered against the core's workspace, so a
remote client never resolves its own filesystem. `transport/rpc.py` owns the
method registry; `transport/models.py` owns strict parameter models. Unknown fields and invalid types fail at ingress. The schema
served at `/api/openapi.json` is the method table built straight from that
registry: shared SDK DTOs, `x-rpc-methods`, and `x-rpc-notifications`. The
frontend generator derives both DTOs and typed RPC method mappings from it.

Standard errors are -32700 (parse), -32600 (invalid request), -32601 (method),
-32602 (parameters), and -32603 (internal failure). Application refusals use
-32000 with optional `data.code` and HTTP-equivalent `data.status`; the clients
retain these as `CoreClientError.code`/`status_code` and `CoreApiError` fields.
Authentication is -32001. Internal failures never expose raw inputs.

A connection accepts at most 32 ordinary pending calls, with eight additional
slots for cancellation, acknowledgements, and unsubscribe. It permits 16 active
subscriptions. Messages are limited to 4 MiB, the outgoing queue to 64 messages
and 8 MiB. Overflow closes with 1013; malformed/oversized input is refused.

`artifact.read` uses the authenticated workspace store, not a filesystem path.
Pages default to 64 KiB and permit up to 256 KiB; byte offsets at/past EOF are
empty with `eof=true`. Invalid digest/page parameters, missing objects, and
storage failures remain distinct. Python clients return `ArtifactPage.data` as
bytes, including binary data; see [artifacts](artifacts.md).

## Revision Binding

`command.catalog` and `session.views` return descriptors together with the
revision they were captured from. `command.invoke` and `action.confirm` require
that displayed `expected_revision`; missing values fail ingress and stale
values are refused before dispatch. Clients refresh the catalog/views and let
the user retry; no automatic mutation retry is permitted. Existing approval and
confirmation IDs retain their distinct durable meanings.

## Event Subscriptions and Recovery

A successful subscribe receipt is queued before any notification. Server
notification `session.frame` carries `{subscription_id, frame}`. The first frame
contains `{session, events}`; replay continues in bounded pages/events, followed
by `{kind: "event", session_id, event}` frames for newly committed events. There
is no whole-history allocation. `follow: false` replays through the captured
session tip and ends. `subscription.closed` identifies the ended subscription.

Every subscription has a 64-event credit window. Clients acknowledge only after
applying a frame successfully; acknowledgements must be monotonic and no later
than the sent cursor. Until credit is replenished the server pauses that
subscription, while other subscriptions and RPC calls remain usable. Queue and
message byte bounds apply in addition to event credit. This also bounds replay
into a browser whose JavaScript consumer is slow, independently of TCP buffering.

Clients resume using their own last applied per-session sequence, deduplicate
replayed events, and never advance from an unprocessed frame or a server's
watermark. A projection/read error sends a terminal error frame with safe
sequence/type/exception information; clients surface it and stop rather than
hot-reconnecting on a corrupt cursor. A retained `server_restart` frame shape is
reserved, with no current producer; it never authorizes skipping events.

A disconnected socket closes its subscriptions, not the core's running work.
In-flight operations finish under application ownership and persist their
results. A lost response or timeout is reported as `outcome_unknown`; clients
inspect session history instead of automatically resending the mutation.
Only event subscriptions reconnect automatically. Explicit session cancellation
continues through `session.cancel` and the core's durable interruption rules.

Current session frames contain committed facts. Future transient progress
notifications must remain distinguishable from durable history and must not
advance a session cursor or become required for recovery.

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

Every view data kind carries an optional `style` object (`ComponentStyle`:
optional `tone` ∈ `default`/`info`/`success`/`warning`/`error` defaulting to
`default`, optional nullable `foreground`/`background` as `#RRGGBB` or one of the 16 ANSI names).
Absent or null style (or `tone: "default"` with no colors) renders the
client's built-in default; `ConfirmationRequest.ui_data` (a `confirmation`
view) styles the same way. Precedence: explicit colors > non-default tone >
`notice.level` (notice only) > client default. Colors tint only the owning
component's content and chrome accent — semantic cues (confirmation
allow/deny, diff `+`/`-`, focus, error markers) stay fixed, and unknown
tones/colors degrade to the default. RGB values render as given; ANSI-name
appearance may vary by client. SDK ingress rejects unknown tones, colors,
and keys, so clients never see raw CSS/ANSI/markup in `style`.

An unrecognized or malformed view is reported visibly; clients never simulate success.

## The Two Decision Gates

| Gate | Request fact | Resolution |
|---|---|---|
| Native tool approval | `core.approval_requested` carries the approval `request_id`, the batch/call identity, and the owner; the session parks in `waiting`. | `session.resume` with the exact `approval_request_id` and `decision`; the engine records `core.approval_decided`. |
| Plugin action confirmation | `core.confirmation_requested` carries the SDK `ConfirmationRequest` (`request_id`, `owner`, `command_name`, `arguments`, `ui_type`, `ui_data`, and optionally `expected_state_version`); `core.confirmation_decided` clears exactly that request. | `action.confirm` with the stored `request_id`, the decision, and the revision the client displayed. |

A request id from one gate is never sent to the other endpoint. Both are projected from durable facts, so a client restart or a server restart restores a pending dialog without the command being re-issued, and a replayed decided request never resurrects one.

## TUI Client

- Launched embedded against a direct in-process core (`CoreLocalClient`: no socket, same refusals and frames as HTTP) or remote with `--server`/`--token` (`CoreRpcClient`). Both implement the typed `SessionClient` Protocol (`sdk/client.py`); programmatic callers open either arm with `bootstrap.open_session_client()`.
- Inline scrollback with a **commit-once ledger**: rows are committed to the native scrollback exactly once by the main loop, driven by a durable `last_sequence` cursor. Committed text wraps to terminal cell width without dropping long output; live input/dialog geometry remains bounded. Reconnects, follow polling, resizes, and session switches never replay or clear settled output; only the live region repaints.
- Renders conversational rows from the canonical transcript (`core.user_message`, `core.model_turn`, `core.tool_completed`/`core.tool_interrupted`) plus the direct `CommandResult` of core read-only commands (`help`, `status`, `show`), which have no command event; command/control payloads (`core.command_completed`, gates, progress) remain dynamic. Plugin and run/resume results remain event-projected; one shared formatter renders each result once, preserving multi-line output and falling back to JSON for data-only receipts. Every model- and plugin-provided string is escaped for terminal control bytes.
- User text is shown verbatim without role labels; thinking is muted, without a
  `Reasoning>` heading. System context, dispatch metadata, and child-session ids
  stay out of the chat view but remain in recorded history and model context.
  Explicit plugin notices and decision gates remain visible.
- Startup, connect, reconnect, session switch, and capability activation never fetch or display optional plugin views: the TUI reconciles health, the catalog, durable session state, and the event-projected decision gates only. `/tui views [name]` explicitly fetches a revision-bound views envelope and displays passive content inline and borderless — passive field values wrap rather than ellipsize (deliberate plugin-level previews/limits still apply) — or an interactive form/choices/confirmation as a framed dialog. The two decision gates above remain event-projected and are never closed or denied by a refresh.
- The default composer uses two horizontal rules with no vertical padding, prompt marker, or placeholder. Input grows with its content. `tui.border` also accepts `box` and `borderless`; `--no-border` selects borderless. The footer context uses the terminal default foreground for readability.
- Footer context row (server truth only): `workspace · provider/model · effort` (`no model` when unset; the effort segment hides when `reasoning_effort` is null — unknown, unset, non-reasoning, and numeric-budget configs show no marker). No run-state and no session id by default (`--show-session` adds the short id). The composer top border carries `<spinner> working` while a run is active (animating on the render tick; clean when idle); gates, errors, and transient notices render on a separate row below that `footer=off` never hides. Idle chrome carries no Attached/Created banners; explicit `/status`, `/show`, and `/tui sessions` keep diagnostics and ids.
- Typing `/` opens command suggestions automatically; edits filter the current
  catalog plus `/tui` controls, one namespace level at a time, without requests per keystroke. Command entries include their descriptions. Up/Down selects,
  Tab or Enter fills a partial command, and Enter submits an exact selected
  command. Tab on a parent enters its subcommands; Enter on a namespace without
  its own command does the same. Bare commands with required scalar arguments
  open a revision-bound form in the TUI; explicit arguments retain their existing
  parsing. Esc closes the popup without cancelling the run. Command arguments
  and ordinary multiline text do not trigger name completion. Catalog refreshes
  update an open popup; dispatch still uses the displayed revision. Overlays
  capture their rendered revision; Esc on confirmation submits an explicit deny.
- Local controls that are not admitted commands: `/tui help|sessions|session <id>|new|views [name]|model [provider/model]|thinking [level|off]|cancel|quit` (bare forms work when no admitted command owns the name). `/tui <control>` always reaches the client control, so a plugin cannot shadow navigation. `/tui model`/`/tui thinking` with an argument rewrite the workspace `model` section and invoke `core.reload` once (embedded, idle sessions only; refused visibly when remote or busy). `/tui thinking` lists current + supported levels; numeric budgets stay valid backend config but are hidden in this UI.
- All blocking client calls happen on worker threads; only the main loop writes rows.

### TUI footer and `health.model`

The footer reads the admitted generation DTO only — `GET /api/health` — refreshed on reconcile and on `core.activation_committed`. An edited-but-unreloaded `voidcode.json` never displays. `health.model` is `{raw, provider, model, reasoning_effort, reasoning_levels}`: `reasoning_effort` is the supported level (clamped from config; `null` hides the segment), `reasoning_levels` the DTO's level list (`[]` when unknown, never null). Budget-backed tiers display their level name (high stays high, never `budget 16384`); wire fields and numeric budgets are not exposed. The workspace segment is the server's `health.workspace` (full path; local-only `~`-shortening), never the client cwd, so remote footers name the server workspace. Narrow widths degrade: crop workspace → drop session → drop workspace → `model · effort` → `model`.

## Typed Programmatic Clients

Both arms satisfy `sdk.client.SessionClient` (runtime-checkable Protocol) and
return the same SDK types; only the transport differs. Failures raise
`sdk.client.CoreClientError` (`str` = server message, `.code` machine code,
`.status_code` HTTP-equivalent application status).

```python
from voidcode.bootstrap import open_session_client
from voidcode.sdk.client import ResumeSpec, RunSpec

with open_session_client(workspace=ws) as client:  # or server_url=..., token=...
    session = client.create_session()
    catalog = client.command_catalog()
    result = client.run(session.session_id, RunSpec(prompt="hello"))
    page = client.transcript(session.session_id)
    while page.has_more:  # advance even when entries is empty
        page = client.transcript(session.session_id, after_sequence=page.next_sequence)
```

History comes in one shape. Every session event carries the canonical `transcript`
projection (`TranscriptEntryWire | null`, OpenAPI `StreamEventWire`), the same
discriminated `{kind, body}` the `session.transcript` and `session.context` methods return
(`TranscriptPageWire`/`ContextHistoryWire`). `transcript: null` means genuine
bookkeeping only; a recognized conversation event that fails projection ends
the stream with a terminal `error` frame carrying sequence + event type +
exception class (never raw input). Clients stop on it without advancing their
applied cursor or hot-reconnecting. SDK `StreamEvent.from_payload` +
`parse_session_frame` own all session-frame decoding; web consumes the generated
`SessionFrameWire`/`StreamEventWire` union and narrows the kind-less head via
`'events' in frame`.

## Tool Icons

Each tool declares its own display identity (`Icon(glyph)` on
`ToolDefinition`; `None` means generic). The core snapshots the birth icon
as an `icon` key (`{glyph} | null`) on the `core.tool_intent`
`core.tool_completed`/`core.tool_interrupted` payloads, and projects it to
`TranscriptTool.icon` (`TranscriptToolWire.icon` on the wire, same
optional value). Clients render the recorded glyph — generic mark when
absent — and never map tool names to glyphs or re-resolve history from the
live catalog. No font detection is promised: UTF-8 proves bytes travel, not
that the font has the codepoint.

## Web Client

The built bundle is served by the core at `/` (an exact file when present, otherwise `index.html`, so client-side routes survive a reload). During frontend development, Vite serves the app on `127.0.0.1:5173` and proxies `/api` to the core (`VITE_BACKEND_URL`, default `http://127.0.0.1:8765`).

The core app (`frontend/src/core/`) renders the same catalog and views with pure components in `frontend/src/core/views/`; markdown goes through `react-markdown` + `remark-gfm` and never evaluates raw HTML. Each view applies its own `style` as a validated inline `style` prop via the one shared converter (`componentStyle.ts`): color/border/background only, scoped to that component, no global CSS, no classnames from data, no separate UI protocol. Every response, view render, form draft, and SSE frame is bound to the session that issued it, and revision binding follows the HTTP rules above.

TypeScript types are generated from the core transport's OpenAPI document:

```bash
mise run frontend:api:generate
mise run frontend:api:check
```

Do not hand-edit `frontend/src/lib/core/generated/api.d.ts`.

The TUI renders committed failures/interruption from durable events only, in transcript order. Run receipts update activity state without duplicating history; transport/admission failures without a run id display a local error. Failures show their reason once in history; terminal status never lingers in the footer. Esc cancels an active run (completion popups consume Esc first); idle cancellation is silent. Ctrl+C clears the draft and arms exit for 1.5 seconds; a second Ctrl+C exits, including while a dialog is open. Ctrl+C does not decide an approval or confirmation. Ctrl+D quits on empty input and otherwise deletes at the insertion point. Submitting another prompt during a run preserves it as a draft and offers Esc to interrupt.

Run failures show `Error:` with the recorded reason once. Deliberate user/owner
cancellation is `Operation aborted`, not a provider error; limits show `Stopped:`
with their reason, while other interruptions retain their details. A provider
that cancels without a user stop is still an error. Terminal stream/projection
errors remain visible and stop the reader; reconnection never resends mutations.

Tool intent uses the declared icon and name, followed by at most two physical rows of compact JSON arguments. Successful results show at most eight physical rows, without a redundant success header. Omitted rows are labeled explicitly and `/show {"events":true}` exposes the recorded arguments/results. Tool errors remain complete; source partial/truncated bounds remain visible separately from the display preview. Streaming prose shares settled markdown geometry, without an extra spinner prefix. Completion popups follow selection in a six-row window; input space is reserved on short terminals.
Empty successful tool results add no `Done` row. Tool failures keep their complete
details without repeating the tool name already present in the compact header.

### Command presentations

Successful direct command receipts may carry a bounded `presentation` using
existing declarative view kinds. Interactive choices and forms mount under the
invocation's captured revision, session, and transport scope. Historical replay
renders the receipt (or passive presentation) without reopening interactions.
A later receipt must not overwrite a pending approval, confirmation, or newer
editor. Web command panels become unavailable on a session, transport, or
revision change. Core command invocation and governed confirmation remain the
only mutation paths; UI presentations never authorize an effect by themselves.

### Prepared command input

`command.input` takes `session_id`, `name`, `expected_revision`, and optional
`arguments` (default `{}`), and returns `CommandInputSnapshot {revision_id,
presentation}`. It is a read, not invocation: preparation runs under strictly
read-only services and creates no command event. Only catalog commands
advertising `has_input` support it. Preparation validates supplied keys/types
against the command schema with top-level `required` removed, so bare `{}` still
opens required-field forms; unknown or wrongly typed arguments are rejected
before the handler runs. Both SessionClient implementations expose
`command_input(arguments=...)`; the web client exposes `commandInput`.

`CommandPresentation` may carry advisory `refresh_after_ms` (250..60000): only
presentations carrying it refresh, and only while their view is open. `Choices`
data may set `prepare_input: true` so a selection routes back to `command.input`
instead of invoking; markdown `ViewAction` may combine `prepare_input` with
`arguments` for read-only navigation (Back/Next page/Refresh). Catalog
descriptors may carry `default_shortcut` (canonical `modifier+...+base` chord,
e.g. `alt+a`). The TUI binds non-reserved, uniquely claimed chords; a collision
visibly disables the chord for every claimant. Chromium exposes accessible
action buttons rather than taking browser/editor shortcuts; those buttons
remain usable when their advisory chords conflict. Plugins declare catalog
metadata, not raw keyboard callbacks or a separate keybinding registry.

A bare slash command uses this optional callback before schema-generated forms.
The UI captures session/transport/generation and a request serial, refuses a
mismatching returned revision, and ignores superseded replies. Editing another
draft or closing/replacing an editor invalidates its outstanding presentation;
a late accepted action may close only the exact editor instance that submitted.
Loading failures are visible, and no prepare failure falls through to invocation.
Explicit arguments use the existing command path. Catalog `text_argument`
metadata allows natural trailing text for a declared string field while keeping
explicit JSON objects available.
