# Changelog

Notable changes to the VoidCode runtime, grouped by milestone.

## Unreleased

### Declared child output schemas with structured delivery
- `spawn_child` accepts an optional `result_schema` (JSON Schema for one JSON object), validated before any spawn; the core carries the canonical schema on the birth record and spawn payload and appends one fixed delivery instruction to the child prompt. No product `yield` builtin, no fixed reminder/retry counts, no accepted-yield marker on `CHILD_FINISHED`.
- `StructuredChildResult` (`data`/`parse_error`/`unavailable`) is built consumer-side only by `validate_child_text`: `task`/`task_batch` surface validated data plus parse causes on the result's first-line JSON, workflows read the same contract through `agent_structured` and settled-node validation. `ChildOutcome` gains no structured fields; cancelled/budget/failed outcomes carry no structured keys while partial results stay readable.
- Schema mismatch refuses keyed retries in both directions; undeclared spec hashes match the legacy eight fields. Declared schemas join the workflow effective-input fingerprint, so a changed contract never reuses another contract's settled result.
- Verification: 43 passed across 5 new files (`test_child_schema` 11, `test_child_structured` 6, `test_task_structured` 13, `test_workflows_structured` 12, `test_child_structured_e2e` 1); 4 `test_subagents_plugin.py` assertions migrated to first-line structured JSON; phase-1 selector 260 passed; `mise run lint` + `mise run typecheck` green.
- Limits: validation never gates the commit path — an invalid child result is reported invalid at read time, never silently successful and never lost work. The three `tests/integration/test_subagents_plugin.py` failures (`test_parent_spawns_a_real_child_and_receives_its_output` KeyError 'operation'; `test_profile_child_inherits_parent_scope_or_uses_explicit_narrowing[None]` and `[scope1]` KeyError 'subagent_type') reproduce identically at HEAD and are pre-existing, unrelated to this slice.

### Behavioral find: prescreen, judge, validate
- New `find` tool (`READ`): a natural-language behavior description plus optional path scope returns ranked locations with path, real line range, evidence excerpt, why-matched rationale, coverage, and cost. Bounded lexical prescreen (one shared-`grep` call per keyword, total matches capped at grep's own `MAX_MATCHES`), then exactly one structured judge call over prescreened candidates only, then line validation plus evidence excerpts through the shared `read` tool. Every line is checked against a real file read; verdict path/line/rationale violations are rejected visibly in `coverage.rejected`.
- Strictly distinct typed statuses: `ok`/`no_hit`/`partial` arrive as tool success (`ok` never ships empty; `partial` covers any truncation while keeping verified hits); `timeout`/`judge_unavailable`/`judge_error` arrive as typed tool failures with the same body plus diagnostics. Literal queries keep working with zero model calls (`mode: "literal"`, plus auto-mode grep fallback).
- Verification: `tests/unit/plugins/test_find_plugin.py` 17 passed, including read-verified evidence, hallucinated-line rejection, per-branch status tests, and a real-Core loopback judge round-trip.
- Limits: judge-prompt snippet caps (12 matches/file, 500 chars/line) are signaled per file in `coverage.snippet_cuts`, not silent. No vector DB, no daemon index, no background threads; model access only through `model_query_structured` with an explicit per-call budget. Symbol navigation stays with LSP.

### Structured tool-result contracts and model queries
- `ToolDefinition.output_schema` declares a tool's success-body JSON Schema contract (`None` = undeclared/unknown); catalog admission validates the schema and refuses invalid contracts. `sdk/result_schema.py` (`resolve_output_schema`/`validate_tool_body`/`try_validate_tool_body`) lets unrelated consumers validate a peer's decoded body with no peer imports, second JSON body, or parallel registry. `shell_exec` pilots the first declared schema across every capture branch.
- Definitions without a contract encode byte-identically to before; legacy payloads without the key decode to undeclared; declared schemas round-trip.
- `PluginServices.model_query_structured` runs a tool-free side turn through the same governed dispatch/abort/usage/tool-calls path as `model_query` (shared `_model_query_turn`), parsing the reply against a caller-supplied result schema. `StructuredModelResult` keeps the verbatim `raw_text` with exactly one of validated `data` or `parse_error` (`invalid_json`/`schema_violation`); cancellation, budget exhaustion, and provider errors propagate as exceptions, never as data.
- Verification: 29 passed across 4 new files (`tests/unit/sdk/test_result_schema.py` 8, `tests/unit/sdk/test_tool_definition_codec.py` 4, `tests/unit/core/test_model_query_structured.py` 7, `tests/unit/plugins/test_shell_exec_schema.py` 10); narrow refresh `tests/unit/sdk` + structured file 45 passed; full phase-1 selector 251 passed; `mise run lint` + `mise run typecheck` green.
- Limits: validation is consumer-side only — nothing on the commit path gates a producer result, so a producer schema bug surfaces at validation time, never as lost work. No fence-stripping or reply repair: fenced/verbose provider JSON yields `invalid_json` with the full verbatim reply kept in `raw_text`.

### Non-root delegation policy cleanup
- Removed the dead `DELEGATION_SPAWN_BUDGET=4` declaration and its engine re-export: a repo-wide search found zero production consumers (enforcement is `ExecutionConfig.max_root_children`). Delegation tests now pin the true quota owner. No behavior change.
- Verified the `CHILD_INLINE_OUTPUT_BYTES` (32 KiB) definition comment already states the true rationale (inline representation policy: artifact identity + bounded preview); no stale "1 MiB stdio frame" justification remains in code. Threshold untouched.
- Verification: `test_delegation.py` + `test_background_actions.py` + `test_spawn_budget_exhaustion_fails_visibly` 21 passed; `test_collaboration.py` 32 passed.

### Known-red baseline burn-down
- Product fixes: unknown child births read as absent instead of raising `KeyError` (`_birth_record` guard); reserved-child claims report `cancelled` through the claim-event fallback; `enqueue_continuation` stamps the lease's captured root action identity so plugin-staged rows drain. Legacy rows without a captured root stay undrainable and fail visibly — no migration, no backfill, no fabricated grant.
- Stale-test updates: result/process envelope unwraps (16 across artifacts, background processes, and process lease); session reads driven with real `root_action_budget` values; oversized child-history pages and shell stdout read through their artifact references; refreshed TUI terminal stub and stream session ids; agent surface expects the `wait` tool; media tests assert the small-result reference contract.
- Verification: full suite green — `python -m pytest tests/unit tests/integration/test_review_plugin.py -q -p no:cacheprovider --tb=no -rf` → 1850 passed, 1 skipped, 0 failed. The `test_p2a_durable_continuation_executes_real_governed_run_and_recovers` spec passes unmodified. The retired 33-ID baseline list lived in docs/testing.md.

### Explicit budgets and immutable result recovery (BREAKING)
- Fresh runs no longer receive an implicit 32-provider-turn cutoff. Optional `max_steps` remains a provider-turn cap; a separate finite `root_action_budget` is an operator grant shared by captured descendant/follow-up work and durable across restart. Resume extensions are explicit, idempotently keyed grants. Only committed `user_stop`/`owner_cancel` child stops map to `cancelled`; budget, shutdown, error, and unknown stops remain interrupted.
- Legacy pending schema-v6 work without a captured root authorization (child births, checkpoints, queued rows, or wakeups) is refused visibly; no migration, inferred grant, or fresh root is fabricated.
- `shell_exec` stdout/stderr now use typed artifact references even for an empty captured stream; the former inline-string/alias contract is removed. The reference preserves captured text, not byte-identical raw subprocess data.
- Core persists complete canonical tool results before omitting machine bodies. `TextOutput.text` is the selected model page; redundant structured bodies remain available through the canonical `result_reference`. Structured-only `EmptyOutput` bodies stay inline, while explicit textual artifact references (including JSON/XML) use bounded paging. Shell/web captured text and file/raster originals retain typed artifact identities.
- Core and local/authenticated RPC clients expose strict byte-offset artifact pages (base64 on the wire, bytes in Python). This does not claim raw binary fidelity for shell subprocess capture or adaptive per-dispatch model-window sizing; those exact SDK gaps remain documented in the Core/SDK TODOs. Viewer and live-progress work is tracked separately from this Core/API foundation.
- Verification recorded in the Core/SDK TODOs: uncapped real CLI drain beyond 32 dispatches, explicit/shared/restart budgets including independent children, valid decoded PNGs through real Core/native provider SDK routes, full >4 MiB captured-text readback, canonical result reopen, selected-file coverage/read→edit, and JSON web content in the real Core/provider projection (targeted proof: 2 passed in 5.56s). No blanket “all MAX” or all-plugin-audit claim.

### Typed child budget outcomes
- Optional `ChildOutcome.budget_exhaustion` carries committed dimension/source/counters through child joins. Recoverable budget pauses are read from the exact run's `RUN_INTERRUPTED` event, remain `interrupted`, and publish no terminal `CHILD_FINISHED`; the observed public budget-stop resume starts a distinct run id, whose later terminal fact does not inherit the old pause's budget data or current root counters.
- `task`/`task_batch`, review, and workflow consumers preserve the typed state; workflows park the node as `interrupted`, not completed or cancelled. Read-only child snapshots remain separate and do not consume notices.
- Focused verification: the Core exact-stop/reopen/resume selector passed (1 passed, 7.60s) after anchoring the terminal fact to the public resumed run id; review, workflow, and real-Core task-output selectors passed in the preceding narrow batch (3 passed; its sole failure was the superseded same-run assertion).

### First-settled child waits and batch result delivery
- `PluginServices.wait_children(child_session_ids)` now waits on the existing Core condition/event cursors for the first durable result of selected owned children, draining older undelivered runs before binding outstanding reservations/current runs. It preserves exact `(child, run)` identities even when a newer run is active; descendant current/pending status is separate.
- The Subagents plugin exposes no-argument `wait` over its parent-owned child IDs. `task_batch.output` checks reportable current-run outcomes without blocking, otherwise waits first-settled; both surfaces return actual result/output references plus separate pending states. Long/inline pages continue through the immutable output URI and byte cursor. `notify_on_completion=False` does not hide a durable result.
- The shared artifact pager depends only on `read_artifact`, so resource readers and ordinary plugin tools share the same UTF-8-safe cursor path without widening either service contract.
- Exact source-stream event receipts and pending-notice cancellation commit atomically with a valid tool result. Read-only snapshots do not consume. A callback-returned valid `ToolFailure` (`status="error"`) commits explicitly staged delivery; thrown exceptions, invalid results, abort, revocation, or shutdown discard it. User steering interrupts without consuming a racing result; approval parking is never success or autoapproval.
- Verification: fresh-process real-Core proof exited 0 with three gated children, 52,000 exact UTF-8 output bytes, visible immutable URI/page/read/reread, no duplicate result, and empty reopen state. Focused Core tests passed for parent/child ledger races, reservation binding, failed-child delivery, approval resume, steering, cancellation, shutdown, foreign-owner refusal, and exact pause receipts. `tests/unit/plugins/test_subagents_plugin.py`: 68 passed.
- Limits: this is not a generic peer/process wait or a full job manager; top-level `task_batch` shared context and accepted-yield semantics remain open. Single-current-run `join_child` polling is fixed by the 'Event-driven single-run child join' subsection below; producer `output_error`/`history_error` exposure is recorded in the following subsection.

### Typed child output/history capture errors
- `ChildOutcome` gains optional `output_error`/`history_error`: the exact run's verbatim committed capture cause, or `None` for no committed failure (including historical facts). `output_preview` is now emitted independently of `output_sha256`, so preview-only outcomes survive joins and waits; completion notices carry the same cause hints.
- Review derives its data from the outcome, pages long output through the shared artifact pager with exact run + immutable `artifact://` URI, and fails visibly while keeping status `completed` when full capture is unavailable. `task`/`task_batch`/`wait` propagate both causes without inventing URIs or relabeling the run; `agent://` shows only the publication run's own errors; workflow nodes persist preview-only facts and persist terminal state before raising failure.
- Verification: `tests/unit/core/test_child_capture_errors.py` 6 passed; `tests/unit/plugins/test_review_plugin.py` 49 passed; `tests/unit/plugins/test_subagents_plugin.py` 71 passed; `tests/integration/test_review_plugin.py` 10 passed, including real-Core long-review full-byte reassembly and old-URI immutability across a follow-up.
- Limits: `ChildSessionSnapshot` still pairs only the latest eligible publication with current status; a current run's capture failure has no snapshot representation and is never attached to a prior publication. Single-run `join_child` polling is fixed by the 'Event-driven single-run child join' subsection below. The three failures formerly listed here are fixed (park expectation corrected; notice root stamped — see below).

### Child-completion notice root identity
- Terminal publication stamps each notice continuation with the birth-captured chain-shared root (`_publish_child_terminal` resolves it via `_captured_child_root_id`); rootless notices could never drain, which surfaced as restart-recovery notice-delivery timeouts and `failed` notice statuses.
- Legacy rule: a birth without captured root identity raises `StoreConflict` at resolution, so the fact publishes with notify off and no `notice_id` while the wake-up is suppressed for that publication only; the result stays readable via join/wait. No migration, no fresh budget, no retry.
- Verification: 184 passed across `tests/unit/core/test_collaboration.py` (30), `tests/unit/core/test_child_capture_errors.py` (6), `tests/unit/plugins/test_workflows_nodes.py` (18), `tests/unit/plugins/test_review_plugin.py` (49), `tests/unit/plugins/test_subagents_plugin.py` (71), `tests/integration/test_review_plugin.py` (10).

### Event-driven single-run child join
- Single-current-run `Core.join_child` no longer polls: it waits on the existing child condition with generation discipline (a notify landing before the wait is never slept through) plus a parent-fact cursor (past the first scan, only new parent events trigger a rescan), sleeping until a state notify or the deadline instead of `Condition.wait(min(remaining, 0.2))`.
- Same observable semantics: identical return shapes at identical decision points; timeout still returns the live running/waiting outcome, never an error; waiting still returns immediately; terminal-without-fact still waits (deadline reports running, not completed/output=None); current-run-only answers; close/revision still unchecked mid-wait.
- New wake sites preserve promptness where the old poll quantum used to discover silent transitions: `drain_continuations` notifies when the drained session is a child (parked follow-up runs commit no fact), reserved-child claims notify, and `cancel_child` re-notifies after its follow-up sweep.
- Verification: `tests/unit/core/test_collaboration.py` 32 passed (incl. 2 new: gated child completing mid-timeout returns promptly with generous upper bound; 3s-timeout expiry returns live running then completes after release); `tests/unit/core/test_child_capture_errors.py` 6 passed; join consumers `tests/unit/plugins/test_workflows_nodes.py` + `tests/unit/plugins/test_subagents_plugin.py` 89 passed combined.

### Adaptive per-dispatch projection headroom
- Tool-text projection is now adaptive per dispatch: engine `_messages` renders at the `TOOL_OUTPUT_BYTES` ceiling, measures, and re-projects within the allocated budget; per-tool floor fallback keeps the ceiling render and counts `ineffective_tools`; the intent records `projection_allocation` and `RequestWindow` measures the final messages including images. Complete-resource identities survive shrinking; dispatch is never refused.
- The `coverage.py` allocator derives `effective_input_limit_tokens` and `measured_tokens_per_character` from committed same-model zero-image dispatch history (reopen-safe pairing) and shrink-only `allocate_tool_text_budget` under the ceiling. Limits come from the new neutral `ModelContextLimits` DTO + `ModelLimitsProvider` probe (openai/anthropic/google adapters + routing + `RegisteredProvider` delegation via `catalog_model_limits`); window image inventory is `None`-poisoned (`None` = unknown, never zero) with a transcript wire mirror. No schema migration, no adapter wire changes, no tokenizer fallback.
- Unknown matrix: unknown limits, unknown usage, or any present/unknown image inventory keeps bytes identical to the fixed ceiling render — never inferred from base64 or JSON byte counts.
- Verification: `tests/unit/core/test_projection_headroom.py` 15 passed (A–G: adaptive shrink with intact URIs + full recovery, unknown-limits/usage byte-identical, images fixed + intact base64, same-model pairing with polluted/legacy skip, floor fallback).
- Limits: even-split unfairness across tools; newest-only measured rate (drifts with model behavior); images force fixed projection; cold sessions without committed same-model zero-image history report unknown. The image-test failures and 33-failure baseline formerly noted here are resolved by the burn-down above.


### Full artifact viewers and live tool progress
- TUI and Web clients page immutable workspace-scoped artifacts without preview-as-full fallback. A real 346,073-byte, 384×300 decoded PNG passed Kitty PTY protocol/reflow/reopen proof; unsupported-terminal explicit open/export used mode 0600 and cleaned its temporary copy. Kitty proof is headless, so physical terminal pixels are not claimed; no Sixel/JPEG/WebP/GIF terminal support is claimed. Authenticated Chromium artifact/image surface: 1 passed/14.1s; Kitty CLI surface group: 6 passed/17.70s.
- A real CLI PTY ran Core with a loopback provider and a socket-gated `shell_exec`: tool identity and independent stdout/stderr markers appeared before gate release; a narrow-viewport draft survived completion; the settled screen showed the provider result and artifact summary. Durable history contained exactly one tool intent and one completion; `Core.read_artifact` returned the exact full stdout/stderr bytes, and the draft did not create an extra provider request. The temporary smoke script was removed.

### Governed background tool waits
- Background actions can now await their own staged `ToolInvocationReceipt` through `BackgroundContext.wait_tool` after the short callback lease returns. The exact owner/action/request/session/revision identity gates a typed `ToolAwaitResult`; completed calls return the actual canonical-hydrated `ToolResult`, while never-executed or lost requests carry no invented result.
- Ordinary dispatch, captured origin visibility/stance/root authorization, current narrowing, schema and approval gates remain authoritative. Approval waits release the session lock; queued same-action requests are notified after earlier approvals settle. Active leased callbacks are refused rather than allowed to self-deadlock. Stale or legacy missing authority fails visibly. Close and safe-boundary retirement wake affected waits; public activation remains refused while its session/claimed-work blockers are present. Exact action cancellation and lost-driver recovery disarm only the bound request/action and never replay a completed mutation.
- The SDK still has no foreground-suspend API for ordinary callbacks, and full `eval`/remote-resource product work remains P1; typed child budget propagation is recorded above.
- Verification: `uv run pytest -q tests/unit/core/test_background_tool_wait.py` — 12 passed in 44.88s, including a real external SDK read→branch→edit with a >65,536-byte canonical result and a genuinely killed subprocess with no pending-edit replay. Existing nested approval/native pairing compatibility: 9 passed in 21.08s.

### Effectiveness plugin removed (BREAKING)
- The bundled plugin and `/effectiveness` command are removed. Before upgrading from a build that bundled it, set `plugins.effectiveness.enabled` to `false` and run `/core reload` while still on the old build; restart restores the recorded revision, and no ledger/config conversion is performed.

### Subagent monitor (TUI Alt+A + web action)
- Hidden live view over the bound session's descendant subtree: searchable roster with stable selection and viewport, scrollable detail with Back/Refresh/Cancel actions, and explicit subtree cancellation through the durable confirmation gate. Browsing and refresh are read-only `command.input` polls only while the view is open; duplicate chords bind nobody with a visible conflict; disabled delegation removes the binding and drops open views without retry.
- Proven on real PTY (80x24, 40x18, embedded and remote) and real Chromium against served cores with loopback providers: gated live child/grandchild/sibling plus running root, deny preserving work, allow settling `interrupted`/`owner_cancel` with requested wording, draft/caret restoration, and reload/disabled invalidation.
- Cancellation is run-bound through active signals, parked commits, and queued-work disarming. Old SDK invocations cannot register late child work after cancellation returns; completion notices do not automatically revive interrupted parents. Later explicit work remains legal.

### Model-only compaction (BREAKING)
- The deterministic rendering path is deleted: every projection's replacement is the model's summary text, and a failed, blank, or unshrinkable summary fails loudly instead of falling back. The `summary_enabled` config key is removed (strict parsers refuse it); old stored `deterministic` rows still validate but nothing writes that kind again. `CompactionConfig` parsing is now pydantic (`strict=True`, `extra="forbid"`).

### Extensible resource readers (BREAKING)
- The `tool://`, `artifact://`, `transcript://` renderers moved out of `read_tool.py` into shared capability modules registered by the files plugin (`capabilities/files/resource_readers.py`, sharing one bounded stored-artifact pager in `capabilities/resource_pages.py`); `read` imports no reader and holds no scheme table. Admission rejects duplicate schemes naming both owners; disabling a provider removes its scheme with no fallback.
- Error taxonomy: unregistered scheme raises `ResourceUnavailable` (`read` maps it to `unsupported resource URI` — wording preserved); provider `None` is unknown-resource; malformed pages raise `ResourceContractError`; handler bugs and backend failures (including handler-raised `QueryUnavailable`) propagate visibly. `read_artifact` raises `FileNotFoundError` only for genuinely absent digests; permission/storage failures propagate.
- `ResourceReaderServices` exposes caller-scoped child snapshots and owner/session-scoped process status/log reads without general `PluginServices`; process projections bypass service-read cancellation/effect staging.
- Exec's `proc://` projection exposes caller-visible process status/output without private spool paths or process controls; a resource read does not bind cancellation or stage effects. Disabled providers remove their schemes without fallback.
- `agent://<child_session_id>` is registered by `subagents`, is emitted separately from `transcript://` in task receipts, and reads only visible descendants without joining or acknowledging notices. Current status/run and latest eligible published run stay distinct; failed/interrupted/cancelled/empty/preview-only follow-ups preserve the prior completed nonempty result.
- Child output, including previously inline publications, uses the shared immutable artifact pager. Core materializes inline committed bytes as a deduplicated `artifact://` resource on snapshot read without rewriting `CHILD_FINISHED` or changing join output. Normal line/UTF-8 byte selectors continue through the artifact URI. `agent://` has no JSON subpath/query cursor; VoidCode still lacks an OMP accepted-yield marker. Reference comparison: [OMP v18.20.2 source commit `7c49c11aa5bbaf3f418406583096168ad5b8c6dc`](https://github.com/can1357/oh-my-pi/tree/7c49c11aa5bbaf3f418406583096168ad5b8c6dc).

### `web_fetch` page snapshots and `web://` reader
- New `web_fetch` tool (`READ` + `NETWORK`, never replayed): fetches a credentialless http(s) URL (static HTML only, 5 redirects, 5 MiB cap, bounded Markdown preview) and spills the full rendered bytes to an artifact, returning a `web://<sha256>` snapshot URI plus the canonical `artifact://` link for recovery.
- New `web://` resource reader owned by the web plugin: `read` pages fetched snapshots like any stored artifact through the shared pager, with no files-plugin dependency and no network at read time. Disabling `web` removes only the alias; the canonical artifact read still works.

### Configurable Unicode TUI banner
- Empty sessions show a centered, gradient-colored V built from Unicode blocks. Replayed sessions remain quiet; settled scrollback is not rewritten.
- `tui.banner.text` and `tui.banner.colors` customize the artwork and its local palette; `tui.banner: null` disables it. Relative spacing, blank lines, narrow terminals, and `NO_COLOR` retain Unicode without restoring a theme system.

### Environment-discovered provider catalogs
- Built-in providers are discovered from their declared credential environment variables, including OpenRouter and DeepSeek, without requiring a `providers` entry or vendor `base_url`. Explicit configuration wins; disabled/replaced provider plugins do not gain hidden capabilities.
- Credentials and provider identities are resolved at live admission and stored privately. Restart, replay, and generation adoption keep the recorded values; `/core reload` explicitly picks up changed environment credentials.
- `/model openrouter` reads the configured `/models` endpoint through the existing SDK client when no shipped catalog exists. Listing makes no inference request or retry; errors stay visible and credentials stay out of public output.

### Space-separated slash subcommands (BREAKING)
- TUI and web use `/core reload`, `/goal set`, `/skills show`, and `/workflow status`; dotted slash input is no longer accepted. Completion, help, and command labels use the same catalog-derived spelling.
- The longest registered path wins and argument text keeps its dots and whitespace. Machine command IDs, RPC/CLI invocation names, captured revisions, and decision gates are unchanged.

### Minimal TUI transcript and automatic slash completion
- User prompts render verbatim without role badges; system context and internal
  scheduling metadata stay out of the chat view without changing stored history
  or provider context. Thinking and tool output stay compact, without Pi-style
  cards or an added renderer framework.
- Errors retain their reason once; deliberate cancellation is distinct from
  provider failure and limit interruption. Routine success/status labels are
  omitted, while decision gates and terminal stream failures stay visible.
- Typing `/` opens live catalog suggestions; editing filters them, arrows select,
  Tab/Enter completes, and Esc closes without cancelling. Exact selected commands
  submit with one Enter; argument entry and multiline input are left alone.

### Glyph-only tool icons (BREAKING)
- Tool icons are one Unicode glyph (`Icon(glyph)`); the ASCII `fallback` field, TUI `--icons` flag, `tui.icons` config key, and web `iconStyle` prop / `?icons=ascii` param are removed with no aliases. Strict ingress rejects the old shapes; clients render the recorded glyph (`None` = generic mark).
- The TUI is Unicode-only chrome: rounded box borders, a Unicode prompt, and a Braille working spinner (proven under a real PTY).

### WebSocket JSON-RPC transport (BREAKING)
- Remote business communication now uses JSON-RPC 2.0 on `/api/rpc` with subprotocol `voidcode.rpc.v1`; business HTTP routes and SSE are removed. HTTP remains for static assets, health, and the generated contract document.
- Remote TUI and web clients multiplex calls and event subscriptions. Per-session replay uses applied sequence cursors, acknowledgements, bounded event credit, and bounded output queues. Local TUI retains direct `SessionClient` calls.
- Disconnect does not cancel running work. Lost mutation responses are reported as unknown outcomes and never resent automatically; explicit cancellation remains available during long calls. Revision-bound commands and both durable decision gates retain their semantics.
- The schema generator derives DTOs plus typed method/notification mappings from one backend registry. RPC, cross-client conformance, and browser reconnect tests cover the cutover.

### Scripted workflows plugin
- New bundled `workflows` plugin: explicitly configured trusted `.py` scripts (`plugins.workflows.config.workflows: {name: path}`) run `async def run(ctx, task)` over core-owned children via `ctx.agent(...)`. Source is snapshotted at start and the recorded bytes execute once after a digest check; changed sources refuse resume before executing anything.
- One model tool (`workflow_run`, governed `EXECUTE+SPAWN+SESSION`, never replayed), five commands (`workflow.run/status/pause/resume/cancel` with shared `/workflow.*` client syntax), and two generic views. Terminal runs optionally hand their bounded summary back to the originating session as a durable follow-up turn; failures fail both the run and the background action so `invoke --wait-background` exits nonzero.
- CLI `invoke`/`confirm` gain `--wait-background` (`--wait-timeout` seconds): after the command commits, the same process waits for the receipt's background action (or the session's active ones) and reports settled states in the result JSON. Timeouts, stale claims, and failures are never green.
- `policy.max_root_children` (1..64, default 4) is the single governed child-session quota per chain root, replacing the fixed internal budget. Runnable reference: `examples/workflows/`.
- Restored the pending command-confirmation gate in generation adoption: a pending confirmation blocks `adopt_generation` until it is decided.

### Canonical SSE transcript cutover (BREAKING)
- Every SSE event now carries the canonical `transcript` projection (`TranscriptEntryWire | null`): the same discriminated `{kind, body}` the `/transcript` route serves. Untagged-shape adapters, `payload.result` fallbacks, and duplicate JSON decode are deleted on both clients; no compat shims. `transcript: null` means genuine bookkeeping only.
- SSE frames are SDK-owned wire types (`SseFrameWire`: kind-less head + `event`/`error`/`server_restart`), registered in OpenAPI; web consumes the generated union and narrows the head via `'events' in frame`.
- A recognized conversation event that fails projection ends the stream with a terminal `error` frame carrying sequence + event type + exception class only (never raw input/secrets). Both clients surface it once and stop — no applied-cursor advance, no hot reconnect on the corrupt cursor. Web tool rows show a one-line cue for unconfirmed stops / unknown side effects.

### Single `/status` owned by the core (BREAKING)
- Core `/status` is the one session-status entry: it keeps every existing `show` key and adds `event_counts` (one SQL aggregate), `usage`, and `budgets` for all owners. The plugin `session_status` command is deleted with no alias.
- Prune verbose plugin comments (todo/goal/delegation), no behavior change.

### Plugin-owned view colors (ComponentStyle)
- Every view data kind (`notice`, `markdown`, `fields`, `choices`, `confirmation`, `form`, `diff`, `status`) plus `ConfirmationRequest.ui_data` carries an optional `style` object: optional `tone` (`default`/`info`/`success`/`warning`/`error`, default `default`), optional nullable `foreground`/`background` as `#RRGGBB` or one of the 16 ANSI names. SDK ingress rejects unknown tones/colors/keys — no raw CSS/ANSI/markup channel. Unstyled views render the client's built-in default.
- Both clients render the same precedence (explicit colors > non-default tone > `notice.level` > built-in default), scoped to the owning component only; semantic cues (confirmation allow/deny, diff `+`/`-`, focus, error markers) stay fixed. The TUI theme catalog is deleted (no theme command/names/modes); web renders validated colors via one shared converter as a scoped inline style.

### Tool-owned display icons (Icon)
- Tools declare their own identity: `Icon(glyph, fallback)` on `ToolDefinition` (`None` = client generic). Built-in glyphs are ordinary Unicode symbols with explicit ASCII fallbacks (fallback validated printable-ASCII at construction); plugins may use NerdFont PUA glyphs. All 24 bundled tools plus `core_reload` and the tally example declare icons; the MCP proxy declares one generic icon.
- The core snapshots the birth `icon` onto tool intent/result events and `TranscriptTool.icon`; history keeps it across reloads/removals, clients never map names or re-resolve. Modes are explicit (`glyph | ascii`, default glyph): TUI `--icons glyph|ascii`, web `iconStyle` prop / `?icons=ascii`. The TUI tool-name→glyph map is deleted. No font detection: UTF-8 proves bytes, not coverage.

### Transparent sandbox exec backend
- New optional top-level `sandbox` section in `voidcode.json` (`mode`, `network`, `writable_roots`): when present, every registry child (shell, background, user-shell, LSP services) runs under OS-enforced confinement via the native `voidcode-sandbox-helper` (public `mxc-sdk` engine: bubblewrap/Seatbelt/ProcessContainer). Absent section means unconfined host exec, byte-identical to today.
- Confinement is transparent: plugins make zero sandbox changes. `SandboxPolicy` follows the `ExecutionPolicy` neutral pattern; `core/sandbox.py` builds helper argv + explicit env + protected paths; spool-only logs under `state_dir/process-spool/`; no silent degrade (missing backend refuses open loudly).
- Observed true effects: the confined supervisor snapshot-diffs writable roots and reports creates/updates/deletes/moves; `RuntimeServices` stages them onto the lease and the engine merges them into `result.changes` for success AND failure (prefix rule), deduped by `(path, operation, old_path)`, capped at 256 per result. Background reads/cancels stage the cumulative sidecar; services never stage.
- Honest V1 boundary: hooks executor, review git, and MCP stdio stay host-side with zero plugin changes and are never claimed confined. File tools stay host-side and self-report. No reload gate (new spawns use current policy; old jobs keep birth enforcement). `danger-full-access` observations are labeled best-effort. No seccomp filter on the bubblewrap path (namespace+mount isolation only).

### Streaming turns end-to-end (P1)
- The engine prefers `stream_turn` when the selected provider offers it, aggregating text/reasoning/tool deltas into the same turn shape `propose_turn` returns; non-streamable providers keep the `propose_turn` fallback. Single-attempt by contract: no retry once bytes flowed.
- Observational `core.provider_progress` events (dispatch-correlated text fragments) flow through the existing control-event/SSE path ahead of the committed `core.model_turn`.
- SSE follow poll tightened 1.0s -> 0.1s (measured 2026-10-26: commit-to-delivery 0.1-0.9s down to 0.007-0.09s).
- The TUI renders the in-flight token buffer as partial markdown in the live region with a semantic run spinner, settling to committed markdown on `core.model_turn`; the statusline shows spinner + run state.
- Tab completes `/-command` names from the catalog plus `/tui` controls: a single match completes inline, multiple matches open a chrome popup above the composer (Tab cycles, Enter accepts, Esc cancels).
- The statusline is segmented: short session id, run-state with spinner, plus the current notice.
- The spinner advances once per frame while a run is active and stays static when idle, reusing the existing run-active tracking.

### SDK strengthening (BREAKING)
- `voidcode.sdk` now exports the full unified surface (`Plugin`, `StateSchema`, `PluginQuery`/`PluginQueryCaller`, tools, providers, commands, `PluginServices` + value types, views including `ComponentStyle`, hooks/facts, `ToolContext`, codec, `json_values`, `limits`, and history/transcript types). Submodule paths keep working; no circular imports; the SDK direction guard stays green.
- `PluginServices.get_state_schema_version` and `clear_window_projection` are deleted (zero in-repo consumers; test-only references migrated). The orphaned `store.plugin_state_meta` and `store.clear_window_projection` go with them. All other 40 protocol members keep at least one caller in `capabilities/**` or `plugins/**`.
- `QueryContractError` moves from `voidcode.sdk.queries` into `voidcode.sdk.services` beside `QueryUnavailable` and the `query()` transport; `sdk/queries.py` is deleted. The Unavailable-vs-ContractError distinction is documented once on the error: `QueryUnavailable` = no admitted provider (take the optional path), `None` = provider knows no matching resource, `QueryContractError` = malformed answer, a provider bug. Import sites in `workspace_queries`, `read_tool`, and `apply_workspace_edit` are migrated.

### Runtime-owned provider retry (tenacity)
- `PluginRuntime.propose_turn` now retries adapter-approved transient failures in-process: an explicit `retryable is True`, or a `rate_limit` the adapter left undecided. Structured `retry_after` is honored (over-long waits refuse the retry); otherwise a jittered exponential backoff. Bounded at 4 attempts; backoff polls the run's abort signal and raises `cancelled` on it. Each call carries its 1-based `request.attempt`.
- New dependency `tenacity>=9.1.4` (already in the lockfile via `google-genai`).

### Minimal core cutover, outer product layer layout, schema v6 (BREAKING)
- **Minimal core decoupling**: Core now takes neutral `ExecutionConfig` and `ExecutionPolicy` (`approval_mode` and `read_only`), removing `CoreConfig` and product responsibilities. Product configuration schema, `voidcode.json` validation, bundled plugin discovery, and provider credential resolution live in the outer layer (`src/voidcode/config.py` and `src/voidcode/bootstrap.py`).
- **Plugin-only runtime**: `PluginRuntime._register_builtins` and builtin providers are deleted. Core no longer imports `capabilities`; files, execution, and providers are ordinary plugins with zero fallback. A zero-plugin core manages sessions and SQLite persistence only.
- **Outer product layer & CLI cutover**: Outer adapters are organized as `src/voidcode/cli.py`, `src/voidcode/config.py`, `src/voidcode/bootstrap.py`, `src/voidcode/transport/`, and `src/voidcode/tui/`. Entry points `python -m voidcode` and console script `voidcode` point to `voidcode.cli:main`; `voidcode.core.__main__`, `voidcode.core.bootstrap`, and `voidcode.sdk.config` are removed.
- **Storage schema v6**: `chain_step` is removed from checkpoints and engine cursor; `action_budget_remaining` is renamed to `initial_root_budget` preserving seed semantics. Storage schema is bumped to v6; legacy v5 databases are refused read-only with no automatic migration or silent deletion.
- **Removal of `migrated_owners`**: `StateCopyPlan.migrated_owners` and event passthrough are deleted. Plugin state schema mismatches are explicitly rejected.
- **Transport & TUI improvements**: Uvicorn server dynamically binds via `Config.bind_socket()` with bracketed IPv6 URLs; manual socket creation is eliminated. TUI SSE streaming uses `httpx.AsyncClient` with asyncio task cancellation, and `InvokeRequest`/`ConfirmRequest` strictly forbid extra fields.
- **Typed history services**: raw `read_child_events` is deleted; plugins read bounded Markdown pages via `read_child_history` / `read_session_history`. Config parser `hide_input_in_errors` prevents credential echo.
### Web tools: `web_fetch` removed, `web_search` fixed provider order (BREAKING)
- The `web_fetch` tool is gone: there is no bundled page fetcher anymore. Ask the model to search instead, or fetch pages through your own command or script.
- `web_search` tries its search backends in a fixed order (Exa first) and uses the first one with a credential set — one attempt, no silent hop to the next backend on failure. Set any one of the search keys below and search works; with none set it fails with a visible message naming the credentials instead of silently falling back to a weaker engine. The search-engine scraping fallbacks are deleted, so results come from a real search API or the call fails explicitly.
- The `research` and `plan` agent profiles no longer offer `web_fetch`; their web access is `web_search` only. The agents→delegation migration guidance in this file still applies: delete stale `plugins.agents` sections, profile discovery roots are unchanged.

### Plugin deactivation lifecycle, agents→delegation merge, stable compaction fact (BREAKING)
- Every state-owning bundled plugin now declares `prepare_deactivate`: compaction, cron,
  delegation, exec, plan, skills, todo join goal. Disabling a used owner no longer refuses the
  reload: cron schedules park disabled with no armed wakeup, plan mode parks off, todo keeps
  its list with reminder counters zeroed, and compaction/skills/delegation/exec keep their
  history untouched for re-enable. Live work still refuses first through the core's session
  blockers, naming the session, before any transform runs.
- The activation transaction now cancels a dropped owner's pending wakeups and narrows overlay,
  projection, continuation, and budget cleanup to the switching workspace; claimed wakeups keep
  settling through their own run. Budget clearing covers cancelled owners plus still-enabled
  owners the caller explicitly names.
- The `agents` plugin id is removed: its profile registry (`/agents`, `/agents.show`) now ships
  inside `delegation`, which also owns `task`/`task_batch`. Delete stale `plugins.agents`
  sections (they fail closed); profile discovery roots (`.voidcode/agents/`, user agents dir)
  are unchanged. `subagent_type` resolves through the one registry, so workspace custom
  profiles execute via `task`/`task_batch`; unknown ids fail at ingress, and a batch with any
  bad id is refused whole with zero spawns. Builtin read-only stance lives on the profile
  record; custom profiles run writable unless the parent lease is read-only.
- `plugin.compaction.context_compacted` is the stable cumulative compaction fact (one event per
  applied projection); `/effectiveness` counts it plus the legacy `runtime.context_compacted`
  history. Compaction no longer drops `todo` results by tool name: every covered result keeps
  its bounded preview.

### Product rename: kernel → core; formatter hook deleted (BREAKING)
- The runtime package is `src/voidcode/core/` (`Core`, `open_core`, `CoreResult`, `CoreConfig`,
  `create_core_app*`, `python -m voidcode.core`, console script `voidcode.core.__main__:main`); the
  web client is `frontend/src/core/` (`CoreApp`, `CoreClient`, `CoreApiError`, `core.*` i18n keys,
  `core-app` testid) with `e2e/core.spec.ts`; docs and scripts follow (`core-upgrade.md`,
  `feat(core):` scope). The reserved names are `core.reload`/`core_reload`, health reports
  `core_version`, new revision pins record `core_version`, stdout reads
  `core endpoint:`/`core bearer token:`/`core ready:`, and the 503 drain message is `core is
  draining…`. No aliases or compatibility importer: old names are refused visibly, never mapped.
- Deliberately unchanged so sessions survive: `kernel.*` event types, the `kernel`
  `source`/`actor`/`owner` values, `/api/kernel/*` control routes, `kernel-v1/`,
  `kernel-v1.sqlite3`, `kernel-images/`, the schema magic, `launch_protocol`, and old stored
  pins. Children launch through the root facade (`-m voidcode`), so a new supervisor serves
  the elected old image; an old supervisor cannot launch a new image and fails preflight
  visibly — stop it first, restart under the new build, then `upgrade run` (procedure and the
  formatter-config recovery in `docs/contracts/core-upgrade.md`).
- The `formatter` bundled plugin (post-write formatting hook only — never a tool, command, view,
  or state owner) is deleted with its `format_on_write`/`languages` configuration; writes, edits,
  and patches now commit exactly the bytes the tool produced. Run the project's own formatters
  (`mise run format`, Ruff, frontend Prettier) explicitly. A workspace that still configures
  `plugins.formatter` is refused with guidance until the entry is removed or disabled.

### Trusted in-process plugins without manifests, pins, or host RPC (BREAKING)
- A plugin is now a Python package directory whose `__init__.py` exports
  `create_plugin(config) -> Plugin`. `voidcode-plugin.json` is gone, and with
  it the identity generator (`scripts/generate_plugin_identities.py`, the
  `plugins:identities:*` tasks, and the pre-commit hook), `--trust-plugin`,
  `capabilities` grants, hash pinning, artifact copies, and declaration-drift
  checks. Plugins are trusted code equal to the application; there is no
  replacement sandbox or marketplace. No compatibility importer: old pinned
  state is refused with guidance, never silently converted.
- `Plugin` carries the runtime metadata directly: `observed_facts`,
  `pre_tool_match`/`pre_tool_mutating`, `post_tool_match`/`post_tool_mutating`,
  and `state=StateSchema(version, schema)`. `sdk/manifest.py` and the
  `tool_definition`/`command_definition` helpers are deleted;
  `ToolContext.capabilities`, `CommandDefinition.capabilities`, and the codec
  `capabilities` field are gone, as is the `direct_user` command trust
  classification: all commands follow the same effects/read-only rules with no per-command exemption.
- The out-of-process plugin host is gone with the RPC: `core/host.py` and
  the whole `plugin_host` package are deleted, with the host-only wire codecs
  (`encode/decode_context`, provider-error/command/view codecs) and the
  `sdk_protocol` health field. `core/plugins.py:PluginRuntime` loads each
  plugin package in-process under a unique namespace per generation (bytecode
  caches bypassed, so reloads never read stale `.pyc`) and dispatches every
  call by direct synchronous invocation: cancellation is cooperative, and the
  engine error surface is plain `ValueError`/`RuntimeError` instead of
  `HostUnavailable`/`HostTimeout`. `Core.plugin_runtime` replaces
  `Core.host`. There is no generic outer runtime-timeout API
  (`RuntimeToolTimeoutError`, `RuntimeTimeoutAwareTool`,
  `invoke_with_runtime_timeout` are deleted with the retired execution
  backend): a non-cooperative callback that never returns blocks its dispatch
  until the core restarts. Native child-process, provider-IO, MCP/LSP
  request, child-join, and supervisor/drain timeouts keep their real consumers.
- The core injects the resolved providers map into every factory as the
  reserved `config["providers"]` key, which plugin-owned config must never
  use. A restart imports the current source with the recorded configuration;
  a revision is an opaque UUID naming the recorded config/composition, with
  the private snapshot digest remaining for config integrity. A plain start
  against a schema-changed source refuses instead of reinterpreting stored
  state; a dead core's orphan `running` session is interrupted only inside
  the controlled adoption holding the exclusive workspace lease
  (`kernel.run_interrupted` at the last committed boundary, cursor kept, still
  explicitly resumable, never replayed). Kept: lifecycle/cancellation,
  `core.reload`, state migration/deactivation, stale-revision checks, durable
  events, private credentials, and the core-image upgrade path. Runnable
  reference: `examples/tally-counter/` (now a package).

### Agent collaboration: steering, notices, and readable transcripts
- Delegated children are core-owned. `kernel.child_spawned` now persists
  `notify_on_completion`, so a restart rebuilds a queued completion request from durable facts
  instead of a child thread's local argument. `kernel.child_finished` carries the run id, status,
  stop cause, inline-or-spilled output (bounded preview plus `output_sha256`), and the immutable
  history snapshot, exactly once per `(child, run)`.
- `task(operation="send")` adds an attributed follow-up in the same child session (queued behind
  running or parked work, consumed at a committed batch boundary) and `task(operation="history")`
  pages the readable transcript through an `(after_sequence, history_offset)` cursor. Joins are
  run-scoped: a follow-up can never be satisfied by an older terminal fact. Only the direct
  parent may steer, and a read-only parent cannot steer a child that is not durably read-only.
- Completion notices are staged atomically with the child fact and keyed per run.
  `acknowledge_child_completion` stages consumption on the invocation lease, so it commits with
  the tool result that carried the outcome (a crash cannot lose or duplicate the notice). Startup
  recovery republishes a terminal child run whose parent fact a crash never committed, without
  rerunning the child, and delivers its notice.
- `transcript://<child>` and `task(operation="history")` render readable payloads
  (user, assistant with tool calls, tool results, child facts, progress, plugin events) instead of
  a payload-stripped skeleton. Pages are bounded by 40 KiB, split oversized blocks on UTF-8
  boundaries without clipping, and stay under the core's 256 KiB context bound; output pages
  remain reachable through the saved artifact. Contract: `docs/contracts/agent-collaboration.md`.

### Providerless first boot without `voidcode.json`
- A truly fresh state directory (no database, no adopted revision, no legacy file) now opens with
  empty in-memory defaults instead of failing `core configuration is required`: bundled plugins,
  commands, tools, sessions, `/help`, and `/status` work, and no `voidcode.json` is created. The
  human `/status` output reports `model: not configured`; model turns and governed model queries
  fail with one actionable message naming `model.model` as `<provider>/<model>`, the root
  `providers.<provider>.base_url`, and `core.reload` after a valid file is created. Only model
  execution is unavailable; no provider, key, network call, or file is synthesized.
- Every other start remains strict: an existing database (including one with no adopted workspace
  revision), a foreign schema, a legacy `.voidcode.json`, a missing config on reload, a
  trusted-plugin flag without a config, and malformed/unknown/oversized files keep their existing
  refusals. Existing revisions restore from their private snapshot without reading the live file,
  and reload after deleting the config fails without moving the active revision.

### Supervisor control socket: bounded per-user address
- Default startup no longer fails with `socket_bind_failed: ... AF_UNIX path too long` followed by
  the embedded TUI's "supervisor did not report its endpoint": the old default control path,
  `~/.local/state/voidcode/kernel-v1/<workspace-sha256>/supervisor.sock`, exceeds the fixed
  `sun_path` limit. The socket now lives at
  `/tmp/voidcode-<uid>/<sha256(canonical state dir)>.sock` in a private `0700` per-user runtime
  directory (socket `0600`), so a supervisor and a separate CLI process derive the same bounded
  address without stored metadata; live-owner refusal, stale-socket replacement, same-OS-user
  permissions, and shutdown unlink are unchanged. The durable state path and workspace identity
  are neither shortened nor moved.
- Platforms without POSIX ownership/mode semantics refuse to host the supervisor
  (`control_socket_unsupported`) instead of using a weaker, unbounded fallback.

### TUI quit closes its owned event stream
- Quitting the TUI no longer leaves an idle follow connection open while the embedded supervisor
  stops the core child, which made the server wait out its graceful-shutdown timeout and log a
  cancelled ASGI task. The TUI owns a cancellable handle to its SSE response, duplicates the
  response's public file descriptor, and shuts that connection down before joining the stream
  worker and before the embedded supervisor is stopped. Remote mode closes the same connection and
  leaves its server serving. Idle streams still need no heartbeat, timeout, or reconnect change,
  so an older core image without heartbeats keeps working.

### Bare `voidcode` entry and user-facing help
- `voidcode` and `python -m voidcode.core` with no command open the inline TUI; the `tui`
  subcommand is removed (no alias), while `serve`, `run`, `resume`, `show`, `preflight`,
  `upgrade`, and the client commands keep their grammar. Root client flags (`--server`, `--token`,
  `--session-id`) work on the bare entry, durable root options are accepted before or after a
  command, and bare-only flags combined with a command fail visibly.
- Root help groups client and workspace options and orders the commands by use; each subcommand
  explains its own scope and arguments, with copyable examples on the commands that need them, and
  the supervisor's internal `serve` flags stay hidden. Help exits 0 without reading config, state,
  or a host.

### TUI quiet startup: no automatic views, no idle hashes
- Startup, connect, reconnect, session switch, and activation never fetch or display optional plugin views. `/tui views [name]` (for example `/tui views goal.details`) fetches one revision-bound envelope: passive content renders inline and borderless, a form/choices/confirmation renders as a framed dialog.
- Idle chrome carries no session/revision/request hashes and no Attached/Created banners; explicit `/status`, `/show`, `/session_status`, and `/tui sessions` keep diagnostics and ids. Committed text wraps to terminal cell width without dropping long output; live input/dialog geometry remains bounded. Passive field values wrap rather than ellipsize (deliberate plugin-level previews/limits unchanged). No SDK, protocol, or plugin change.

### TUI views and command results
- Core `help` and `status` now return human `CommandResult.output` (structured `data` unchanged). The TUI renders core read-only receipts and plugin `kernel.command_completed` results through one formatter: multi-line output is preserved (no literal `\x0a`), data-only receipts fall back to JSON without dropping `{}` or nested falsy values, and a real completion clears the stale `Dispatching...` status without overwriting a pending gate or fenced state. Plugin and run/resume results stay event-projected, so no row is rendered twice.
- No SDK field, wire protocol, or plugin contract change.

### P5 controlled kernel upgrade: new builds install as new generations
- Kernel images are content-addressed, immutable installation snapshots (`<state>/kernel-images/<digest>/root`, bundle included); children launch only from an image with a pinned, provenance-verified environment, and the elected image digest is pinned into every revision (`image: {digest, kernel_version, launch_protocol}`), retained across later plugin-only activations.
- `voidcode upgrade run --candidate PATH [--session-id ID] [--timeout S]` asks the running supervisor for an upgrade and `upgrade status` reports it; `preflight` prints the read-only candidate check. Preflight verifies schema transition, SDK/launch protocols, recorded revision, pinned artifacts, plugin state versions, and the target generation diff, then executes admission, state migrations, and a real host load in a throwaway scratch tree — refusing unsupported generations before anything stops and without touching live state.
- The supervisor owns one loopback listener and one bearer token across replacements, fences admission and wakeup claims, drains admitted work at committed batch boundaries (pausing active runs with a durable, exactly-once resume intent when asked), and elects the candidate only after health checks and one atomic adoption transaction move the schema, the revision pointer, every session and continuation ref, every resumable checkpoint, and every state copy. Parked approvals and stored confirmations block a revision-changing upgrade before downtime instead of being rebound; a failed drain leaves streams connected and the old kernel serving.
- A hard-killed child is never silently replaced: the supervisor exits visibly, the next launch cold-starts the elected image, and an explicit `resume` reports each parked batch member as `kernel.tool_interrupted` before continuing from the durable cursor. Same session id, append-only events, and no replay of completed side effects across the handoff; `kernel.revision_adopted`, `kernel.upgrade_paused`, and the generalized `kernel.continuation_consumed` (with its real `activation`/`upgrade` cause) record the transition. `GET /api/health` reports fence, stage, revision, SDK/image pins, generation, and the active upgrade; a successful drain ends followed streams with `server_restart` plus the last delivered cursor, and fenced business writes answer `503 upgrade_pending`.
- `serve` runs the supervisor (with `--session-id` attribution for a first adoption); the bare command without `--server` spawns a supervisor child and behaves like remote mode; ordinary one-shot commands execute inside the elected image. The single supported schema transition is 4 → 5 inside the adoption transaction; anything else refuses read-only, and `show` stays a config-free rescue read on either version.

### Tool contracts: derived views, document guards, managed language servers
- `read` serves its resource URI views end to end: `tool://<name>` renders the active
  revision's dispatch definition with this session's scope/stance applied (plugin and reserved
  core tools, no second catalog); `transcript://<session>` composes a bounded,
  payload-stripped page for the caller's own session or its own descendants; `rule://`
  is answered by the skills plugin's rulebook semantics. URI failures return model-visible text.
- `PluginServices` gains neutral `query`/`tool_definition`/`spawn_service_process` (plus typed
  `QueryUnavailable`); `Plugin` gains host-local read-only `queries`. Child-event reads are
  narrowed to true descendants (siblings, parents, unrelated roots, foreign workspaces refused).
- `apply_workspace_edit` checks `expectedVersion` against the LSP plugin's opened-document
  record, failing closed with a visible diagnostic when nothing is open; stale version/hash and
  encoding mismatches return typed actionable diagnostics, and hash-only edits work with the LSP
  plugin disabled.
- Language servers run as supervised host-generation service processes (shared host group,
  descendant-tree cancellation, foster-parent reaping, core group sweep on hard host death);
  the `lsp` tool declares `read`+`execute`, so read-only policy refuses it and ask/write modes
  approve it. SDK source hash changes by design (P5 controlled upgrade).

### Docs and comment corrections (no behavior change)
- Comments, docstrings, and guides no longer describe deleted pre-cutover trees or promise
  unshipped work: the SDK-hash mismatch path is documented as it behaves today (activation
  refuses a hot switch; changed SDK bytes are crossed only by the controlled 1→1 core
  upgrade described in `docs/contracts/core-upgrade.md`), `read_only` is documented as
  refusing `WRITE`/`EXECUTE` only (`SPAWN` stays available to a restricted owner because the
  child inherits the restriction), the client diagrams state that the CLI drives the core
  in-process while the TUI/web client use the HTTP/SSE transport, and the README no longer
  claims session data never leaves the machine.
- Removed the unused `configure_openai_provider` helper from `tests/provider_loopback.py` (it
  wrote a pre-cutover `.voidcode.json`) and its now-unused `pathlib` import; the loopback
  server and `openai_response` helpers are unchanged.

### Provider reasoning replay and P6 authoring
- Tool-call replay no longer hard-fails when a reasoning-locked provider (DeepSeek) returns a
  genuine zero-reasoning tool turn: authentic `reasoning_content` is replayed verbatim when the
  core persisted it, omitted (never synthesized) when the vendor produced none, and prior
  reasoning on assistant turns without tool calls is preserved whenever the request carries
  tools. The guard's old "must fail before HTTP" assumption contradicted the real wire (a
  zero-reasoning tool turn is accepted upstream) and made the vendor's own turn unreplayable.
- `scripts/generate_plugin_identities.py` accepts repeatable `--plugin-dir` with `--check`, and
  the runnable `examples/tally-counter/` plugin plus `docs/contracts/plugin-authoring.md` cover
  the author → pin → verify → reload loop. Provider plugin identity was regenerated by that
  canonical generator, never hand-edited.
- The generator forgets a deleted non-Python resource only via explicit repeatable
  `--drop-resource` (exactly one `--plugin-dir`); the unused `ast_grep.txt` was removed from the
  `ast_grep` manifest that way, with its seven `.py` pins and tool declaration unchanged.

### P4 corrections
- Internal SESSION work survives a read-only stance: a lease whose tool declares `SESSION`
  (`goal`, `todo`, `compaction`, `cron`) stages state, events, budget and continuations under
  it, exactly as `docs/contracts/README.md` documents SESSION as never refused — window
  projection and wakeup scheduling follow the same rule, since the stance forbids external
  mutation only. Spawn authority is unchanged (a read-only lease still only spawns read-only
  children), and `KernelHost.propose_turn` no longer carries a default provider name.

### P4 cutover: the kernel + plugin architecture is the product
- Removed the legacy runtime stack: `VoidCodeRuntime`/`run_loop`, `core/`, `tools/`, `provider/`,
  `common/`, `command/`, `hook/`, `formatter/`, `agent/`, `skills/`, `mcp/`, `lsp/`, `acp/`,
  `doctor/`, the legacy `cli/` and the legacy HTTP/SPA server. `voidcode` (and
  `python -m voidcode.kernel`) is now the single product CLI: `run`, `resume`, `show`,
  `create-session`, `sessions`, `catalog`, `invoke`, `confirm`, `views`, `drain`, `usage`, `tui`,
  `serve`, with `--workspace`, `--state-dir`, and `--trust-plugin`.
- Configuration is `voidcode.json` only (kernel `schema_version`, `model`, `policy`, `providers`,
  `plugins`). A legacy `.voidcode.json` is rejected explicitly; no conversion is performed, and the
  configuration CLI (`config show/schema/init`) and generated config schema are gone.
- State is per-workspace: `$XDG_STATE_HOME/voidcode/kernel-v1/<workspace-sha256>/`, holding
  `kernel-v1.sqlite3`, pinned plugin `artifacts/`, and the 0600 `private/` configuration snapshot.
  Legacy session databases are neither imported nor auto-deleted: removing historical data is a
  manual action.
- ACP and doctor are deleted with no replacement (plugin inventories `/mcp` and `/lsp` cover part
  of the capability report). The HTTP surface is the generic kernel `/api` contract: no legacy
  REST routes, no session fork/undo/checkout/import, no `sessions rewind` family.
- The human task surface is removed: delegation is agent-facing through the `task`/`task_batch`
  tools only, and `/effectiveness` is session-scoped rather than a workspace-wide rollup.
- All product behavior ships as bundled plugins (providers, files, exec, lsp, mcp,
  web, ast_grep, session_info, todo, plan, skills, agents, review, delegation, btw, cron,
  compaction, hooks, formatter, user_shell, effectiveness, goal) admitted by manifest identity and
  running in a separate plugin host process.

### P4 kernel/SDK seams (S5)
- Session facts and child tails: `PluginServices.session_info()` reads status, run/revision,
  lineage, pending approval, owner budget and root-chain accounting in one SQL row. Bounded
  `read_child_events(child_session_id, after_sequence, limit)` pages are admitted by the kernel's
  persisted own-descendant lineage (narrowed from same-root in the tool-contracts change),
  never by a plugin's claim about its children.
- Same-session tool requests: `services.invoke_tool(name, arguments, expected_revision=...)`
  returns a staged `ToolInvocationReceipt` immediately. The request commits with its caller's
  result, then runs after the callback through the ordinary scope, schema, stance, pre-tool,
  effects and durable approval pipeline. Approvals park the session instead of blocking a
  service RPC; nested depth is kernel-owned and every request spends the same root action
  budget. Hook leases cannot enqueue work, and interrupted mutations are never replayed.
  Deferred results remain attributed user-level data notes with tool, request and invocation
  identities, counted in the ordinary request window; they never fabricate provider calls or
  promote tool output to system authority.
- Committed read coverage: tools attach neutral `ReadObservation` metadata to `ToolSuccess`.
  The kernel derives `ToolContext.read_paths`, `read_lines`, `read_whole_files` and `read_hash`
  from durable, model-visible results after the shared output bound. Partial, truncated or
  clipped results grant nothing; content versions and sessions never merge, and reopening
  the host rebuilds the same coverage without an in-memory ledger.
- Provider-specific configuration stays opaque: `api_key` and `base_url` remain typed while
  unknown provider fields are delivered as private, flat vendor options. The private 0600
  snapshot restores them on restart; revisions and session events never contain them.
- Tool progress: the SDK context's `emit_tool_progress` reports over the existing host channel.
  Bounded, lease-attributed `kernel.tool_progress` events reach the existing session SSE stream
  while the invocation is still running; reporting is observational and never waits for an ack.
- Bundled admission boots the whole product set — `providers`, `files`, `exec`, `mcp`, `lsp`,
  `web`, `ast_grep`, `session_info` and every product plugin — as one generation. Package entry
  modules have per-plugin identities, so bundled `__init__` entries do not collide.
- Lease-scoped processes: `PluginServices.run_process(argv, cwd, env, stdin, timeout_seconds,
  max_output_bytes, on_output)` runs one child of the plugin host in its own process group and
  returns `ProcessResult{exit_code, stdout, stderr, duration_ms, truncated, timed_out, cancelled}`.
  No byte of a child's output crosses the kernel protocol — the host owns the process, its capture
  and its bounds — while the lease contributes stance through the new `lease_stance` reverse RPC:
  read-only and hook leases are refused outright, because neither touches the machine. Each stream
  is captured up to the caller's bound (its own default, hard ceiling 256 MiB) with the excess
  dropped and `truncated` set, `stdin` is written without losing output, a timeout or a lease
  cancellation SIGTERMs then SIGKILLs the whole group before returning, and every tracked group is
  reaped when the host shuts down (its own hard exit included), so no child outlives its generation.
- Supervised background processes: `PluginServices.spawn_process(argv, cwd, env, stdin, stdout_path,
  stderr_path, process_id)` starts a process that outlives the invocation that started it, and
  returns `ProcessHandle{process_id, pid, start_time, stdout_path, stderr_path, running, exit_code}`
  — the identity the caller persists, where `start_time` is the kernel's own start stamp for that
  pid, so a recycled pid can never impersonate a live process. `write_process` (only for a process
  started with `stdin=True` in this host), `read_process_output(stream, offset, limit)` (bounded
  pages with liveness at read time), `cancel_process` (SIGTERM group, then SIGKILL) and
  `list_processes` complete the surface; logs are real files at paths the caller names (created and
  appended, never truncated) or host spool files whose paths the handle carries. Supervised
  processes are partitioned by the session that started them, so no session can read or stop
  another's. Starting or stopping one requires a writable lease; reading does not. A start id that
  is still running is refused rather than duplicated, and reusing it after exit appends to the same
  log. A host swap keeps supervised processes running (the caller's persisted identity is what
  re-attaches them), while the kernel's own shutdown stops every tracked process by group.

### P4 kernel/SDK seams (S1-S3)
- Provider selection is opaque: `KernelProviderEntry` carries only `api_key`/`base_url`, config
  `providers.<name>` must be a provider name declared by an admitted plugin, and credentials are
  injected per plugin for exactly the names it declares (no wire vocabulary or vendor branch in the
  kernel). A model must name a configured provider. The provider turn request now carries
  `metadata={"session_id","run_id"}` for session-scoped adapter headers, and tool declarations can
  express `path_argument_keys`/`supports_parallel` (carried into the admitted definition).
- `PluginServices.get_state_schema_version(scope)` reports the stored owner state's schema version
  (`0` when nothing is stored); `InvocationFinishedFact` now carries the tool `owner` and the
  declared `effects` the kernel governed under (from the invocation's own intent record).
- Per-session run locks replace the workspace-wide lock: independent sessions (delegated children,
  wakeups, a user turn beside a background run) execute concurrently in one workspace, and `close()`
  drains them after cancelling.
- Kernel-owned delegation: `spawn_child/join_child/cancel_child` services create durable child
  sessions with lineage, depth ≤ 3, a durable spawn budget of 4 per root chain, mandatory narrowing
  of the parent's tool scope, join without polling, and cancel for both running and approval-parked
  children. `kernel.child_spawned`/`kernel.child_finished` are control facts on the parent.
- Per-session tool visibility: a session's durable `tool_allowlist` bounds both the advertised tool
  list and dispatch (out-of-scope calls are denied, never executed).
- Typed messages: `ContextContribution.role` and `enqueue_continuation(..., role=...)` let an owner
  declare a `user`-role message that the kernel renders verbatim (no system re-wrapping); role is
  persisted with the continuation and accepted as a control fact.
- Bounded reads and governed queries: `PluginServices.read_events(after_sequence, limit<=200,
  include_payload)` returns session-scoped event views with a 64 KiB payload budget;
  `PluginServices.model_query(prompt, model=None, max_output_tokens=None)` performs one tool-free,
  history-free provider turn and records `kernel.model_query` with its truthful usage.
- Durable wakeups: `schedule_wakeup(payload, due_at_ms, idempotency_key, role)` /
  `cancel_wakeup` own a workspace-scoped table; `Kernel.tick()` claims ONE due wakeup and creates its
  authentic running session in one transaction (no TTL or reaper), renders the payload with its
  declared role, drives it under the session lock and settles the row terminally. A kernel ticker
  runs alongside the activation driver when `auto_drain` is enabled.
- Direct-user command authority: a command declaration can set `direct_user` and a declarative
  `client_syntax`; both reach the catalog. The kernel itself attributes an authentic client
  invocation of such a command to `actor="user"` (agents and plugins can never supply an actor),
  and a read-only session does not block the user's own direct command, while every other command
  keeps the ordinary policy. Clients route the declared syntax; the kernel never parses it.
- Delegation bounds: the client prompt ingress guard stays 64 KiB, while a kernel-driven child
  run uses the delegated-prompt guard (`CHILD_PROMPT_MAX_BYTES`, 256 KiB) — an owner still enforces
  its own tighter product cap, and a payload in the 64-256 KiB band (a review diff) now runs
  instead of failing after the child session was created.
- Read-only stance and delegation: SPAWN is no longer treated as an external mutation for the
  restrictive gate, so a restricted owner can delegate — but the kernel enforces that a restricted
  owner may only spawn a child that stays restricted (`spawn_child(..., readonly=True)`), recorded
  as a kernel-owned overlay on the child session that no plugin can clear. Holding a read-only
  session (`plan`) can therefore run read-only reviews while still refusing every write.
- Measured request window (C8): every `kernel.provider_intent` now carries `request_window` — the
  exact per-segment character counts and digests of the literal messages handed to the provider,
  the measured size of the kernel-side neutral tool encoding, and the advertised tool count
  (`voidcode.sdk.hooks.RequestWindow` / `WindowSegment`, decodable from the event payload a client
  already reads). Token facts remain provider-reported usage only: there is no characters-per-token
  estimate anywhere in the kernel, so panels derive from measurement.
- Verified window projection (declared, kernel-checked, used by assembly): a plugin calls
  `services.project_window(covered_from_sequence=..., covered_to_sequence=..., replacement=...)`.
  The kernel verifies the coverage is a committed prefix of that session covering whole native
  batches, measures the accounting itself (covered bytes = encoded size of the replaced events,
  dropped bytes = the remainder), refuses a projection that would not shrink the window, and
  records it in `window_projections` (one row per session, owner-stamped). Assembly substitutes
  the owner's summary for that prefix, so a long session stays bounded instead of hitting the
  hard fail — with every historical event untouched. `services.window_projection()` /
  `clear_window_projection()` read and drop it, a lease that cannot stage internal work (a
  read-tier tool, a hook, a view) cannot project, and a deactivated owner's projection is
  removed in the same CAS switch as its overlays (schema version 4).
- Lifecycle hooks: `hooks: [{kind: "session_start"|"session_end"}]` with
  `Plugin.session_start/session_end(lifecycle: RunLifecycle, context, services) -> str | None`.
  `session_start` fires once per run immediately before that run's first provider dispatch and
  `session_end` fires with the run's terminal commit (completed or failed), each on a `hook=True`
  read-only lease; the owner's note (or its failure) is recorded in one durable
  `kernel.hook_invoked` event per owner and run, and a second run fires the pair again for its own
  run id. Firing is guarded by that same durable record, so a resume cannot duplicate it, and a
  hook failure never changes what the run decided. Context injection deliberately stays with the
  context contribution: lifecycle hooks are notifications with evidence, not context.
- Pre-tool gate: a plugin declares `hooks: [{kind: "pre_tool", match: [...], mutating: bool}]` and
  provides `Plugin.pre_tool(intent, context, services) -> str | None`. The kernel asks every
  matching gate before the invocation is admitted (and again on the approval-resume path), on a
  `hook=True` lease: a returned reason denies the call with that reason, and a gate that fails,
  times out, or vanishes also denies — fail-closed, never read as permission. Denied calls leave no
  intent and no effect. Hook leases are now read-only by construction: state/event staging,
  continuations, wakeups, spawning, budgets and overlays are refused loudly instead of silently
  dropped (a hook may only read state, events, usage and budget).
- Post-tool hooks: a plugin declares `hooks: [{kind: "post_tool", match: [...], mutating: bool}]`
  and provides `Plugin.post_tool(outcome, context, services) -> str | None`. The kernel runs them
  INSIDE the governed invocation — after a successful result, before it becomes durable — and the
  returned note is part of the result the model reads and the events record. Hooks run only for
  settled successful results, a `mutating` hook is skipped under a restrictive stance (visible
  `[hook ... skipped: read-only policy]` note), hook failure is recorded as a visible note rather
  than lost, and hook leases cannot start nested governed work (continuations/wakeups refused
  kernel-side). Declaring the hook without the callback (or vice versa) is refused at load.
- Deactivating an owner now revokes its restrictive overlays in the same CAS switch, so a disabled
  plugin can never leave a session permanently read-only; unrelated owners keep theirs.
- Restrictive overlays (plan mode): owner-owned `session_overlays` rows (single`read-only` tier)
  tighten a session durably; the effective restriction is the frozen kernel policy OR any overlay,
  and it drives admission (before intent/effects), the provider's advertised tool list, and command
  policy. An owner clears only its own row, the frozen policy is untouchable, overlays survive
  capability reloads, and `set_overlay`/`clear_overlay` require authentic user authority
  (`direct_user` commands) — model/agent/plugin paths are refused kernel-side. Host leases now carry
  the authentic actor for this check.
- Schema version 3: additive `sessions` lineage/tool-scope columns, `continuations.role`, the
  `wakeups` table and `session_overlays`; a version-2 database is refused read-only before any write.

### Capability activation (P3)
- Same-session hot update: a workspace runs one active capability generation, and a switch is a
  single SQLite transaction (`SqliteStore.commit_activation`) covering the immutable revision, the
  workspace pointer, every session ref (including the acting `running` session), migrated or
  deactivated owner state, queue/budget changes, events, projection and checkpoint. Blockers are
  re-checked inside the transaction; a refusal is recorded durably and nothing is cancelled
  implicitly.
- `Kernel` holds one immutable generation snapshot (revision, pinned SDK hash, effective config,
  admission, host, tools, commands) so configuration, catalog, schema, context and dispatch always
  describe the same generation. Each host dispatch holds a call slot until its result commits; an
  activation fence refuses new dispatches and reports foreign in-flight calls as busy instead of
  blocking them, and no lock or transaction is held across an RPC.
- Kernel-owned `kernel_reload` tool stages a durable request and returns; the reserved
  `kernel.reload` command uses the same activation service for explicit user switches. Approved
  switches activate the artifact pinned *before* the approval, so later source edits cannot change
  the approved target.
- Pure owner hooks `Plugin.migrate_state` (candidate host) and `Plugin.prepare_deactivate` (current
  host, session rows receive that row's own event tip, workspace rows `None`), validated against the
  optional `PluginManifest.state_schema`; downgrades are refused and disabled owners lose their
  queued work while keeping durable state.
- Durable automatic continuation: a model-requested switch records a resume intent in the acting
  session's checkpoint; the same run is the continuation, and a crash between commit and dispatch is
  finished once by the kernel's startup driver. A user-issued reload records no intent and bills
  nothing.
- Restart restores the persisted revision and its immutable private configuration snapshot (0600,
  `<state>/private/<revision_id>.json`) instead of re-admitting changed workspace source or config;
  trust is bound to canonical source paths captured once at startup.
- Observer facts are delivered to exactly the owners their *source* revision registered; receipts
  are one per `(event, owner)` with the misleading `generation` column removed.
- Kernel schema version 2: an existing v1 database is refused read-only before any write, and new
  `workspace_state` records the workspace's active revision. Mutating client invokes and
  confirmations now require `expected_revision` (the displayed catalog/view revision), and command
  catalogs and rendered views return the revision they were captured under.
- Pending plugin confirmations are durable facts: `kernel.confirmation_requested` commits atomically
  with the confirmation row, and every decision (allow/deny/stale) emits one
  `kernel.confirmation_decided`, so clients can restore pending requests after a reload and reduce
  them from the event stream instead of the live command result.

### Goal plugin (P2)
- New self-contained official plugin at `src/voidcode/plugins/goal/` (`__init__.py` +
  `goal_plugin.py`) speaking only the public SDK: tools (`create_goal`, `update_goal`,
  `get_goal`), commands (`goal`, `goal.set`, `goal.edit`, `goal.pause`, `goal.resume`,
  `goal.clear`, `goal.load_objective`), declarative views (`goal`, `goal.details`,
  `goal.form`), a context contributor, and a fact observer.
- Strongly typed JSON state with strict parsing: schema version, identity, status,
  objective, token budget, per-metric nullable token counters, measured elapsed time,
  cost, allowance windows, and blocked-audit counters. Unknown extra fields, missing
  required fields, and coerced booleans/numbers are rejected instead of silently
  repaired.
- Window-scoped budget enforcement: each work window opens with an explicit kernel
  constraint (`since_sequence` + allowance) computed from the truthful spend of closed
  windows; unknown spend is never treated as zero, so a finite budget raise or a budgeted
  resume is refused with a visible error until the user uncaps or creates a new goal.
  Unreported usage makes budgeted metrics unknown and fails closed (`usage-limited`),
  while the plugin cancels only its own automatic queue and keeps the constraint in place.
- Truthful accounting: unreported dispatches set token metrics to unknown rather than
  zero, known values cannot rewrite an unknown total into a partial number, unknown cost
  does not poison known tokens, and measured work time comes from provider dispatch
  facts instead of fabricated zeros.
- 3-turn genuine-block discipline: `update_goal(status="blocked")` stages an audit count
  once per distinct logical turn, fails intentionally for the first two turns, and only
  permits `blocked` after three consecutive distinct turns.
- Prompt parity restored: the model-facing text is the legacy goal prompt (`runtime/goal.py`)
  migrated verbatim — active continuation, budget limit and objective-edited variants keep
  their original wording, section order and `<objective>`/`<untrusted_objective>` markers,
  adapted only for nullable counters (`unknown`, never `0`) and the new `usage-limited`
  statuses. Tool descriptions are the legacy `tools/goal.py` text.
- State-bound presentation: `goal.form` is a create form (`goal.set`) with no goal and an
  edit form (`goal.edit`) with an existing goal, so a consumer edit preserves goal
  identity, creation time, charged usage and budget. `goal.pause` also escapes
  blocked/limited states, and `user_stop` applies the full pause policy (close window,
  release constraint, cancel queue).
- Long user objectives spill to unique, exclusively created workspace attachments
  (`.voidcode/attachments/<uuid>/goal-objective.md`) with a required SHA256 digest and
  workspace containment checks; attachment files are immutable history and are never
  overwritten or deleted. `goal.load_objective` reads the full text back.

### Kernel durability corrections (P2)
- Checkpoint references (`batch_sequence`, `intent_sequence`, `approval_sequence`) are
  now bound to the actual appended event sequence inside the same transaction that
  appends the source event (`SqliteStore.commit_staged(..., checkpoint_bindings=...)`),
  replacing the previous two-commit fix-up. A SIGKILL between the source append and the
  caller is covered by executable crash tests for both a native tool batch and a native
  approval ask: the fresh process resumes the authentic prefix, re-bills nothing, and
  cannot bypass the ask.
- Observer fact delivery is restricted at the host boundary to exactly the subscribed
  owners that still miss a receipt for that source event, so two-owner partial receipt
  state no longer re-fires the already-delivered owner.
- Keyed continuation identity is deterministic (`sha256(session, owner, key)`), so two
  concurrent leases that stage before either commits return one id that names exactly one
  durable row.
- Explicit user/owner cancellation settles recoverable parked work, not only approval
  waits; one shared fresh-work admission predicate (idle/finished/failed or terminally
  interrupted) now also gates command-queued auto-drive and confirmation re-invocation,
  and `confirm_action` routes owner cancellations like an ordinary invocation.
- Policy admission classifies external mutation (`WRITE`/`EXECUTE`/`SPAWN`) through the shared
  `has_external_mutations` predicate instead of requiring exactly `{read}`: internal `SESSION`
  state operations (native goal `create_goal`/`update_goal`, `goal.pause`/`goal.resume`/`goal.clear`)
  are no longer denied under `read_only` and no longer wait for approval under `ask`, restoring the
  legacy goal vertical; `WRITE` still waits under `ask` and is still refused under `read_only`
  (checked before approval, so `read_only` remains stronger than `yolo`), and external mutation
  combinations stay denied even alongside `READ`/`SESSION`.
- Unsettled dispatches settle before context assembly, every capped metric (tokens, cost,
  elapsed) fails closed on unknown, run-finished elapsed is measured at origin and
  persisted with the event instead of re-measured on replay, a missing stop cause on an
  interrupted run is an error rather than an invented `user_stop`, and a pre-existing
  same-version database missing the root action-chain columns (`continuations.root_id`,
  `root_budgets`) is rejected byte-unchanged by the read-only preflight.
- Verification pins, kept distinct on purpose: the Goal 27-case suite and its paid
  `deepseek-flash` **yolo** smoke (goal complete, 7331 tokens, 6.356 s, `cost_usd` `None`,
  restart with no extra spend) are recorded above at engine `95e72163…`, which already included
  the native-cancellation owner scope; the later engine revision `8f1bdb6045ff0efb…` adds the
  read-only/ask effect classification and was verified only by generic non-paid loopback runs
  (182 passed: governance/vertical/unit kernel+plugins+SDK, Goal 27 re-confirmed by the goal
  owner on that pin, kernel transport, kernel TUI) — no paid call is repeated for an engine hash
  change, and the read-only/ask branches are proven by
  `test_internal_session_effects_need_no_read_only_waiver_or_approval` plus the native-scope
  cases in `test_native_tool_cancellation_is_scoped_to_its_owner`.

### Documentation
- `docs/contracts/goal-plugin.md`: author/user contract for the goal plugin (state model,
  windowed accounting, command/view surface, spill and containment rules).

### Verification

**Full P2 is verified** (not P3). Run these from the repository root; each command is the
check itself.

```
uv run pytest tests/unit/plugins/test_goal_plugin.py tests/integration/test_goal_plugin.py -q
uv run pytest tests/integration/test_kernel_governance.py tests/integration/test_kernel_vertical.py \
  tests/unit/kernel tests/unit/plugins tests/unit/sdk \
  tests/integration/test_goal_plugin.py tests/unit/plugins/test_goal_plugin.py \
  tests/integration/test_kernel_transport.py tests/unit/test_kernel_tui.py -q
uv run ruff check src/voidcode/plugins/goal tests/unit/plugins/test_goal_plugin.py tests/integration/test_goal_plugin.py
uv run ty check src/voidcode/plugins/goal
uv run python scripts/generate_plugin_identities.py --check
```

- Exercised on the approved frozen stack (`kernel/engine.py 8f1bdb60…` — the paid-evidence
  `95e72163…` plus the SESSION read-only/ask classification only — `kernel/host.py 4b4c8453…`,
  `kernel/storage/store.py 8947131f…`, SDK directory input `b15172ef…`, plugin
  `goal_plugin.py 4ee8a848…` / manifest `5556ecdf…`): **182 passed** inclusive — governance +
  vertical + unit kernel/plugins/SDK (150), Goal 27 (19 unit + 8 integration), kernel transport,
  kernel TUI. Goal coverage: strict state parsing, windowed accounting, unknown metrics, budget
  refusal/uncap, the three-turn blocked audit including "one native batch = one turn", long
  objective single-frame transport with tamper detection, second-plugin isolation and
  process-restart persistence.
- Live smoke on the paid-evidence stack: `deepseek/deepseek-flash` with an **idle `goal.set`**
  (no user prompt, no manual drain) — the goal context alone drove read `17 25` → write `42` →
  read back → explicit complete; goal `complete`, tokens 7331, measured elapsed 6.356 s,
  `cost_usd` preserved as `None`, and a fresh kernel showed the same state without extra work.
  Paid evidence stays at `engine 95e72163…`; the later hash adds only the SESSION
  read-only/ask classification and is covered by generic non-paid loopback tests, so no paid
  call is repeated for it.
- Startup acceptance (throwaway script, deleted): a genuine goal persisted through production
  commands, ordinary base chat working with the plugin config-disabled and no goal context in
  the request, then the exact config restored with the original session intact, no unintended
  provider work and no leftover continuation. Same-session live activation belongs to P3.
- Client-side proof (CLI/HTTP/Web/TUI, GenericSurfaces, accepted): `KernelApp 8ebf0d06…` and
  `Forma fe0d460…` frozen; the real full-bundle browser run with `KERNEL_SMOKE_GOAL=1` passed in
  1.3 min (cold start yields exactly one auto session; same-SID trailing-slash client flip;
  parked native approval page reload keeps the same id; native allow/deny; stored confirms;
  real first `goal.edit` POST with budget 5000 returning status ok; null POST status ok; same
  goal id / created_at / exact 4503-char text), the full-goal PTY run is green (31 commits /
  4618 paints, no settled replay or duplicates), frontend 169 cases + typecheck/eslint,
  transport+TUI 24, and API/identity checks all pass.

### Kernel and SDK (earlier P1/P2 milestones)
- Isolated plugin host with per-call leases, strict capability admission, and
  persist-before-activate revisions.
- Scoped plugin services: optimistic state replacement, namespaced events, durable
  continuations with idempotency keys, windowed usage reports, owner budgets, and typed
  context/observer callbacks.
- Generic CLI and HTTP clients over one kernel transport surface, including command
  catalogs, view rendering, confirmations, and continuation draining.
