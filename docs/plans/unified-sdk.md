# Unified-SDK Plan: Programmatic + TUI Clients Over One Contract

## Goal

One typed client surface that the TUI, the web transport, and future
programmatic callers share — without moving behavior into `sdk/` or
product concepts into `core/`. After this plan:

- `tui/client.py` and `tui/local_client.py` both satisfy one
  `sdk.client.SessionClient` Protocol (conformance-tested, not prose).
- The TUI renders `CommandResult`, gates, transcript pages, and views
  through SDK models with `from_payload` constructors — no bespoke
  `dict[str, Any]` parsing in `terminal_app.py`.
- Transport envelopes (`health`/fence, session rows, SSE frames) are SDK
  types; `transport/projection.py` and `transport/models.py` serialize
  them, never redefine them.
- View action/form dispatch semantics live in importable SDK helpers,
  not in `client-transport.md` prose plus `terminal_app` privates.

## Non-goals

- No programmatic run-loop / agent-session object (pi's `AgentSession`).
  `Core` stays the only orchestrator; the SDK gains a *client* of a
  core, not a second core. Deferred until a headless embedding asks for
  it — say so then.
- No TUI render code in the SDK. SDK owns typed view *models* (including the
  `ComponentStyle` color data each view may carry); the TUI and web clients own pixels (gap 6 decision, below). Historical note: this line once named `tui/theme.py` + `tui/views.py`; the theme catalog has since been deleted — clients render fixed semantics plus plugin-owned `style`, with no theme system.
- No new dependencies. The outer frame envelope is unchanged (kind-less head + `event`/`error`/`server_restart`), but the conversation wire is now canonical: SSE events carry the same discriminated `{kind, body}` transcript as `/transcript` (OpenAPI `SseFrameWire`/`StreamEventWire`/`TranscriptEntryWire`), so the web conversation renderer is generated-typed, not a type-only edit; legacy-shape clients are deleted, not shimmed. OpenAPI stays the wire truth; SDK types serialize to it.
- No `sdk/` behavior: constructors that touch config, storage, sockets,
  or a `Core` live in the outer layer. The direction guard
  (`tests/unit/sdk/test_sdk_direction.py`) must keep passing unmodified.

## Where things live (the split the layer rules force)

| Piece | Home | Why |
|---|---|---|
| `SessionClient` Protocol + envelope/frame/gate/view-interaction types | `sdk/client.py` (new), plus additions to `sdk/commands.py`, `sdk/views.py` | Contracts only: `dataclass`es, `Protocol`s, pure functions over them. stdlib + pydantic/jsonschema, no `core/` import. |
| HTTP + local implementations, `open_session_client()` factory | Outer layer: keep `tui/client.py` + `tui/local_client.py`, add factory to `bootstrap.py` next to `open_core()` | Only the outer layer may import `core`, `config`, `transport`, and `httpx`, and compose them. A factory in `sdk/` would need exactly those imports — that is the violation anti-pattern #1 warns about. |
| Run orchestration (`run`/`resume`/`drain`/`cancel`) | `core/engine.py` (unchanged signatures) | Already exists; the SDK only types its envelopes. |

## Per-gap disposition

| # | Gap | Disposition |
|---|---|---|
| 1 | No session/run orchestration surface in SDK | **Done (Phase 1).** `sdk/client.py` declares `SessionClient` Protocol (`run`, `resume`, `cancel`, `drain`, `invoke_command`, `confirm`, `command_catalog`, `views`, `list_sessions`, `show`, `usage`, `events_stream`, plus `transcript`/`context_history` in Phase 3) typed over SDK models; context-managed `bootstrap.open_session_client()` builds the HTTP or local arm. `Core` methods unchanged. |
| 2 | No client-facing transport/catalog contract | **Done (Phase 2).** SDK `SessionRow`, `HealthStatus` (incl. fence), `ErrorEnvelope`, `SseFrame` union (`head`/`event`/`error`/`restart`); `transport/models.py` + `projection.py` serialize from them; both TUI clients return them. |
| 3 | No transcript/history for clients | **Done (Phase 3).** `Core.transcript_page` / `context_history` already return `sdk.transcript` types on the plugin runtime; add thin `Core`-level pass-throughs (core already imports `sdk.transcript`) and client methods returning `TranscriptPage`/`ContextHistory`. TUI cursor logic consumes `next_sequence`/`has_more` instead of raw `core.*` payloads. |
| 4 | No typed gates for clients | **Done (Phase 4).** `sdk/gates.py`: `ApprovalGate`/`ConfirmationGate`/`PendingGate`, `GateState`, typed `GateChange` inputs, pure `reduce_gates(state, change)` + `pending_gates(state)`; `resume`-with-decision and `confirm` typed on the Protocol. `terminal_app` decodes events/receipts to changes and renders from gate models (keeps the overlay widgets). |
| 5 | No view dispatch helpers in code | **Done (Phase 5).** Pure SDK helpers: `choices_dispatch(view, option_id) -> (action, arguments)`, `validate_form_submission(view, values) -> arguments` (reusing `FormField` semantics), `is_read_only_load(view, catalog) -> bool` (the read-effect rule from §"Declarative Views"). `tui/views.py` + `terminal_app` privates delegate to them. |
| 6 | No typed client-side view models | **Done (Phase 5).** Decision: **SDK owns typed view models, TUI owns pixels.** `from_payload` constructors on the eight `*ViewData` classes + `ViewsSnapshot`/`CommandCatalog`/`CommandResult`; zero render code in SDK. TUI keeps `render_*` + interactive widgets, now fed by models instead of raw mappings. |
| 7 | TUI imports bypass SDK | **Done across Phases 1–4.** `terminal_app` keeps its (legal — outer may import core) `EventType` import for the stream pump, but all payload interpretation goes through SDK models. `transport.projection` is the single shared outer shaper both clients use (local arm included — one legal import, no duplicate paths). Bare `dict[str, Any]` return positions on the two clients are replaced by the Protocol's typed signatures; a conformance test pins it. |

## Phases (smallest diff first; proven by affected suites + pre-commit)

Baseline note (RESOLVED, historical): the full `mise run check` tree was red
on 6 integration failures + 42 cron fixture errors that reproduced identically
on unmodified HEAD (pre-existing, unrelated scope — see triage). Both are
fixed (shared loopback streaming-usage fixture drift; obsolete cron fixture
kwarg); the gate is green. Each phase below was proven by its affected suites,
schema/factory smokes, and `pre-commit run --all-files`, all green.

Status: P1–P5 implemented and verified: typed clients/history/gates/views
landed; affected suites (122 pass, 1 skip), TUI suite (37 pass), schema +
factory smokes, and real local+remote PTY (all markers, durable effects,
restart no-dup) green; `pre-commit run --all-files` all 10 hooks pass.

### Phase 1 — `sdk/client.py`: the Protocol + `open_session_client()` [done]

- **Contracts:** new `sdk/client.py` — `SessionClient` (runtime-checkable
  Protocol), `SessionRow`, `HealthStatus`, `ErrorEnvelope`, `RunSpec`
  (`prompt`/`max_steps`), `ResumeSpec` (`approval_request_id`/`decision`).
  Reuse `CommandResult`, `ConfirmationRequest`, `CommandCatalog`,
  `ViewsSnapshot`, `SessionUsageReport`, `ContinuationResult` unchanged.
  Export from `sdk/__init__.py`.
- **Impl:** `bootstrap.open_session_client(*, workspace | server_url, token, ...)`
  returns `SessionClient` (local arm wraps `open_core()`; remote arm wraps
  `CoreHttpClient`). No `Core` signature changes. Impl files only:
  `src/voidcode/sdk/client.py`, `src/voidcode/sdk/__init__.py`,
  `src/voidcode/bootstrap.py`.
- **Verification:** affected suites green (`tests/unit/sdk/`,
  `tests/unit/test_session_client_conformance.py`, history + TUI suites);
  direction guard untouched; schema smoke validates actual history
  responses against served OpenAPI; factory smoke drives the public
  `open_session_client` local + real-HTTP arms.
- **Implemented:** as specified, plus `RunResult`, `CancelReceipt`,
  `CoreClientError` (SDK-owned `ValueError` with `status_code`/`code` +
  `from_refusal`), neutral `StreamCancellation` handle, and ingress-validated
  `RunSpec`/`ResumeSpec` (prompt ≤65536 chars / `CLIENT_PROMPT_MAX_BYTES`,
  `max_steps` int 1..1024). `open_session_client` is a context manager; the
  local arm builds `CoreLocalClient` from `core.workspace` (single canonical
  identity, relative paths included).

### Phase 2 — Transport speaks SDK types; clients return them [done]

- **Contracts:** `SseFrame` discriminated union + `core_error_status`-shaped
  `ErrorEnvelope` move into `sdk/client.py` as types; `transport/models.py`
  + `projection.py` *serialize from* them. Wire fields are unchanged, but
  component names follow the SDK (`HealthStatus`, `SessionRow`) and the new
  history routes add schemas, so the generated `api.d.ts` changes; the plan's
  earlier identical-output claim is superseded (duplicate schema definitions
  removed instead of aliased).
- **Impl:** `tui/client.py` + `tui/local_client.py` adopt `SessionClient`
  signatures (typed returns; `CoreClientError` moved to the SDK so
  programmatic clients catch it without importing `tui.*`, all imports
  migrated, `tui` re-export removed). `local_client` keeps its single legal
  `transport.projection` import: projection emits the canonical transcript
  via the SDK encoders and all SSE decode runs through
  `StreamEvent.from_payload` + `parse_sse_frame` (no legacy adapter, no
  second shaping path, no move).
  The HTTP arm validates session ids client-side for identical 400s.
- **Verification:** `tests/unit/test_session_client_conformance.py` (parametrize
  local + loopback-HTTP arms over real cores): durable command effects,
  stale-409 no-effect, run→tool→history paging, typed SSE transcript,
  invalid-input parity; public factory smoke + real PTY cover the remaining
  surface; plus `mise run frontend:api:check` clean.

### Phase 3 — Client history reads [done]

- **Contracts:** `TranscriptPage`/`ContextHistory` already exist in
  `sdk/transcript.py`; new explicit-kind wire
  (`encode/decode_transcript_page`, `encode/decode_context_history`) because
  untagged `{}` is ambiguous between `TranscriptInterrupted` and an
  all-default `ProviderTurnResult`. SSE carries the same canonical
  `{kind, body}` transcript (emitted via the SDK encoders, decoded via
  `StreamEvent.from_payload` + `parse_sse_frame`); no legacy adapter.
- **Impl:** `Core.transcript_page` / `Core.context_history` pass-throughs
  (delegating to the existing `PluginRuntime` implementations at
  `core/plugins.py:706-726`); client `transcript()`/`context_history()`
  methods; TUI ledger cursor consumes `next_sequence`/`has_more`.
- **Verification:** extend the Phase-2 conformance test with a
  prompt→transcript round-trip; `tests/integration/test_transcript.py`
  still green.
- **Implemented:** wire models (`TranscriptPageWire`/`ContextHistoryWire` +
  per-kind content wires, discriminated on `kind`) are the single strict
  schema: the same models decode client payloads AND document
  `GET .../transcript` / `GET .../context` in OpenAPI, so the served schema
  describes the exact `{kind, body}` wire including tool-result
  `status`/`type` codec fields. `run`/`resume` document `sdk.client.RunResult`
  (same wire as `CoreResult`). SSE carries the same canonical `{kind, body}`
  transcript; the SDK decoders own it end to end. Programmatic use:
  `with open_session_client(workspace=ws) as client:` then
  `page = client.transcript(sid)` and advance `cursor = page.next_sequence`
  while `page.has_more` (even when `entries` is empty).

### Phase 4 — Typed gates [done]

- **Contracts:** `sdk/gates.py` (landed, UI worker) — actual names:
  `ApprovalGate`, `ConfirmationGate`, `PendingGate`, `GateState`,
  `GateChange = ApprovalRequested | ConfirmationRequested | GateDecided |
  ActivationCommitted | RunTerminated`, `reduce_gates(state, change)`,
  `pending_gates(state)`. The reducer takes typed changes only (no raw
  `core.*` parsing); outer `terminal_app` decodes `StreamEvent`/receipts
  into changes. No `collect_pending_gates` — superseded by the reducer.
- **Impl:** `terminal_app` gate handling (`APPROVAL_REQUESTED` /
  `CONFIRMATION_REQUESTED` / `*_DECIDED` branches) constructs these models;
  `resume`/`confirm` call sites pass `ResumeSpec`/typed ids.
- **Verification:** `tests/unit/test_core_tui.py` green incl.
  replayed-decided-never-resurrects; real local+remote PTY acceptance
  (approval + confirmation flows, durable effects, restart no-dup).

### Phase 5 — View models + dispatch helpers (closes gaps 5+6) [done]

- **Contracts [done]:** `from_payload` static methods on the eight
  `*ViewData` classes, `ViewsSnapshot`, `CommandCatalog`, `CommandDescriptor`,
  `CommandResult`, `ConfirmationRequest`; `parse_view_data(view_type, data)`
  typed accessor (descriptor `.data` stays the owned Mapping); pure
  `choices_dispatch`, `validate_form_submission(view, values, *,
  action_schema)` (nullable rules + native numeric prefill), `is_read_only_load`
  (exactly-`READ` rule).
- **Impl [done]:** `tui/views.py` constructors take SDK models only (no
  `Mapping` overloads retained); `terminal_app` privates and widgets
  delegate validation/dispatch to SDK helpers, keep all pixel code.
- **Verification:** `tests/unit/sdk/` model tests (malformed view fails
  visibly, bad form values rejected, stale-revision dispatch untouched);
  `tests/unit/test_core_tui.py` green; no `sdk/` file gains a non-allowed
  import (guard test pins it); PTY exercises choices/form/confirmation end
  to end.

## Open questions

1. **Settled signal.** pi's `agent_settled` (quiescence past per-run end)
   has no VoidCode equivalent; `RUN_COMPLETED/FAILED/INTERRUPTED` end runs,
   not sessions. Do clients need a derived settled signal (computed, never
   persisted), or is `drain` + run-end enough? Lean: defer until a client
   (TUI spinner, headless waiter) demonstrably reimplements it twice.
2. **Stream pump ownership.** Should SSE reconnect/cursor bookkeeping move
   into a shared outer `follow_events(client, ...)` helper, or stay in
   `terminal_app` + web client separately? Lean: stay; extract on second use.
3. **Images/attachments.** `TranscriptPage` carries provider messages that
   may reference artifacts; client rendering of non-text output is out of
   scope here — confirm no client needs it before Phase 3 (TUI currently
   renders `[mime]` placeholders; keep).
4. **`show --events` raw escape hatch.** `Core.show(events=True)` returns
   raw stored events for rescue reads; keep it untyped (it is a debug/rescue
   path, not a client contract) — confirm no client depends on its shape.
