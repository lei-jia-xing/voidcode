# TODO

Work preserved from retired audit/plan reports. Unchecked items remain
unfinished; open items cite the file/symbol they concern. No owners or dates.

## Interactive usability gaps (Pi comparison)

Initial source-based collection, not a completed interactive audit. P1 items
cover reported friction and confirmed implementation limits; P2 items need
hands-on comparison before being treated as missing features. Pi references:
[terminal usage](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/usage.md),
[autocomplete contracts and implementation](https://github.com/earendil-works/pi/blob/main/packages/tui/src/autocomplete.ts),
[extension UI](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md).
Pi offers command descriptions, argument hints/completion, interactive selectors,
and visible usage/context information. Use those interaction patterns as a
reference while preserving VoidCode's catalog, revision, and append-only rules.

Implementation progress:

- [x] TUI and web completion now show one namespace level at a time and include
  catalog command descriptions; TUI Tab enters child commands while Enter keeps
  executable parent commands available. Argument hints/completion and web keyboard
  navigation are implemented below; full acceptance remains tracked separately.
- [x] TUI bare commands with required scalar fields open a revision-bound form
  instead of immediately submitting empty arguments. Explicit arguments retain
  their existing syntax. Both clients now support these forms. Rich plugin-specific
  list/detail/actions remain open below.
- [x] Command results may return bounded, validated declarative presentations.
  `/agents` opens a profile selector and `/goal` opens its create/edit form;
  both clients dispatch actions under the receipt revision. History replay never
  reopens an interactive presentation. Form validation errors retain the draft.
  See `docs/command-ux.md` for the remaining per-command acceptance inventory.
- [x] Footer context now uses terminal default foreground instead of grey dim.
  Real PTY dark/light and narrow-terminal acceptance is recorded below.
- [x] Both clients show required/optional argument hints and a namespace marker.
  Web completion now supports Up/Down, Tab, Enter, and Esc. Full argument
  suggestions and cursor/argument-preservation acceptance remain open.

- [x] Plugin-owned input preparation supplies dynamic agent/workflow/run options
  in both clients; goal edit loads existing values. Natural string parameters are
  catalog metadata (`text_argument`), with explicit JSON retained. Preparation is
  strictly read-only and cannot execute the command to discover candidates.
- [x] Plan status now offers the explicit next mode action; bare `/loop` opens a
  multiline interval/prompt editor. TUI and web use human presentation titles.
  Late replies cannot close or reopen a newer editor.

- [x] Markdown details now expose revision-bound actions and can open prepared
  forms. TUI details scroll within terminal height; history replay stays passive.
  Skills have selectable discovery and paged details. Todo has paged tasks,
  explicit add/edit/update commands, hidden state tokens and stale-edit rejection;
  model tools and user commands share the same mutation implementation.

- [x] `/compact` now returns structured accounting for success/no-op/empty sessions;
  unavailable measurements remain unknown, and CLI/history retain the receipt.

- [x] MCP/LSP inventories now offer service selection and status/error details.
  MCP tools are paged from cached discovery; browsing never starts a service.
  Disabled/empty states explain configuration recovery. Physical client acceptance
  remains in the command inventory.

- [x] Existing goals now show status/usage and applicable Edit/Pause/Resume/Clear
  actions. Empty/cleared goals open creation; edit uses prepared prefilled input.
  Long objectives use a labelled preview; read-only details do not mutate state.

- [x] `/review` prepares target/focus input with explicit branch/SHA requirements,
  without starting child work. Natural focus text remains supported; focus is
  preserved when an explicit review target is selected.

- [x] `/loop` receipts offer Browse/Cancel; `/loop list` pages schedules and
  `/loop delete` shares the model tool cancellation path. Empty lists offer creation.
  Cancellation disarms pending wakeups without claiming to stop already started runs.

- [x] Workflow status now offers applicable lifecycle actions, paged node
  navigation and UTF-8-safe result pages. Run summaries are labelled previews;
  full structured status and existing cancellation confirmation remain intact.

- [x] `/effectiveness` now presents paged readable diagnostics and explicit unknown
  usage/cost values, with refresh/navigation actions and unchanged structured data.

- [x] `/context` now has paged segment details that separate character counts
  from cumulative measured tokens and unknown capacity. `/usage` shows measured
  values, unknown coverage and scoped budget ceilings in structured fields.

- [x] Real Chromium acceptance now covers review draft retention on refusal,
  schedule creation/list/cancel, diagnostic empty states, goal creation preparation
  and passive replay. It also caught and verifies a hot-reload fix: candidate
  runtimes retain the budget reader, so `/usage` keeps working after adoption.

### P1 — confirmed friction

- [ ] Slash commands must provide useful interactions, not merely print a string.
  Implemented foundation: validated `CommandResult.presentation`, direct-receipt
  mounting in both clients, `/agents` choices → profile details, `/goal` prefilled
  create/edit form, and schema-derived required-argument forms in both clients.
  Passive presentations render in TUI history; interactive ones never reopen
  on replay. Remaining plugin commands still require the per-command audit.
  Audit status/list commands for selectable items and relevant next actions,
  editing commands for forms and validation, and long detail output for a
  readable detail view. Reuse declarative choices/forms/status views in both
  clients; keep committed command receipts as history. A successful string
  response alone does not satisfy the interactive UX acceptance criteria.

- [x] Hierarchical slash discovery: `_completion_candidates` in
  `src/voidcode/tui/terminal_app.py` and `slashSuggestions` in
  `frontend/src/core/CoreApp.tsx` flatten all public paths with prefix matching.
  Typing `/` or `/ag` lists `/agents` and `/agents show` as peers. Show one
  root entry initially; after `/agents `, offer its subcommands. Preserve bare
  `/agents` as an executable list command and allow direct `/agents show` input.
  Apply the same rule to goal/workflow/core and nested namespaces; derive the
  hierarchy from the admitted catalog, never a client command table.
- [x] Completion descriptions and argument guidance: TUI
  `Composer.completion_rows` (`src/voidcode/tui/composer.py`) and web suggestion
  buttons show command paths only, discarding catalog descriptions. Display
  a concise description, required argument hints, and a clear distinction
  between entering a command group and executing a command. Keep long text
  readable at narrow widths without obscuring the selected item.
- [ ] Plugin slash argument UX: `_dispatch_catalog_command` in
  `src/voidcode/tui/terminal_app.py` maps a single required string naturally,
  but multi-field commands otherwise require a JSON object. For example,
  `/goal set` exposes objective + optional token budget this way. Audit every
  bundled plugin's commands (start with goal, workflows, delegation, cron,
  plan, todo, review, skills); specify natural input or a guided form, usage,
  examples, defaults, and actionable validation errors for each. Preserve
  strict structured CLI/RPC contracts and one authoritative argument mapping.
  `/agents show <name>` already works and must keep working.
- [x] Dynamic argument pickers: optional SDK `CommandInput.prepare_input`,
  catalog `has_input`, and revision-bound `command.input` render input without
  invoking the command. Agent names, configured workflows, and applicable run
  IDs come from plugin-owned discovery/state. Form options are bounded by the
  view byte limit and validated at submission. Empty choices produce notices;
  unavailable capability, handler errors, malformed data and stale revisions
  remain visible failures. Covered by `test_command_input.py`, agent/workflow
  input tests, TUI/Web selector tests and the real RPC integration test.
- [ ] Completion keyboard behavior and client parity: TUI
  `Composer._accept_completion` replaces the entire draft; web suggestions
  currently apply via button clicks and append a space. Define and verify
  Up/Down, Tab, Enter, Esc, exact matches, trailing spaces, deletion, cursor
  movement, and parent commands with children. Tab should allow continuing
  into subcommands/arguments; selection must preserve existing arguments and
  must not accidentally dispatch. Cover catalog refresh/plugin disable and
  stale revisions in `tests/unit/tui/test_tab_completion.py` and
  `frontend/src/core/slash.test.tsx` when implementing.
- [x] Footer contrast: context uses terminal default foreground, leaving dim for
  secondary notices. Real PTY acceptance covers 16-color ANSI with dark/light
  palette hints at 80 and 40 columns; footer retains normal foreground, and
  model information survives narrow widths. See
  `tests/integration/test_tui_command_browsers.py` and
  `tests/unit/tui/test_statusline_segments.py`; the client contract is updated.
- [ ] Footer information: `_footer_context` / `_footer_row` in
  `src/voidcode/tui/terminal_app.py` show workspace/model/effort/session, but no
  usage or context pressure. Compare Pi's visible context and accumulated usage;
  decide a compact display backed by committed usage facts and available
  provider metadata. Unknown values stay unknown; show cost only when reliable
  pricing exists. Keep essential information readable before adding more fields.
- [ ] In-run input: `_dispatch_user_prompt` in
  `src/voidcode/tui/terminal_app.py` restores the draft and asks for Esc while
  a run is active; web disables its prompt input during a run. Pi supports
  steering and follow-up queues. Design explicit delivery semantics, pending
  message visibility, cancellation/recovery, and draft preservation using public
  core contracts; never imply a message was delivered before it was committed.

### P2 — comparison and interaction audit still needed

- [ ] Composer workflow audit (`src/voidcode/tui/composer.py`,
  `src/voidcode/tui/terminal_app.py`, web composer): compare Pi's `@` file search,
  Tab path completion, external editor, multiline/large paste, and image input.
  Record what already works, concrete failed tasks, and small acceptance cases
  before proposing implementation; artifact placeholders alone do not establish
  an input capability gap.
- [ ] Discoverability and everyday operations audit (TUI `/tui` controls, web
  `CoreApp.tsx`, plugin command definitions): compare model/thinking selectors,
  settings, shortcut help, session selection, copy/export, and plugin status.
  Walk through first launch, no model, invalid arguments, empty lists, and
  unavailable plugins. Prioritize guided recovery and readable command output
  over raw JSON. Retain existing model/thinking controls; do not duplicate them
  under a hardcoded alias table or import Pi's fork/undo model.
- [ ] Transcript interaction audit (`src/voidcode/tui/terminal_app.py`,
  `src/voidcode/tui/views.py`, frontend views): compare long tool output,
  expand/collapse, thinking visibility, errors, and result copying. Capture
  terminal repros and web keyboard behavior; separate rendering problems from
  missing data and do not change committed history to improve presentation.

## Recently closed

- [x] Command invocations and confirmed commands preserve bounded supervisor file
  observations on their completion events, including commands that fail or cancel.
- [x] Explicit user/owner cancellation immediately settles abandoned tool batches
  with unknown effects for the active call and not-executed outcomes for the tail.
- [x] Commands own independent abort signals; session cancellation reaches both the
  active run and concurrent commands without replacing either signal.

- [x] Remote business communication uses WebSocket + JSON-RPC 2.0, with a
  shared method/notification schema, both remote clients migrated, acknowledged
  bounded replay, concurrent cancellation, and no automatic mutation resend.
  HTTP serves health/schema/static assets only; local `SessionClient` stays
  direct. See `docs/contracts/client-transport.md` and
  `tests/integration/test_core_rpc.py`.

## Product decisions (explicit, conditional)
- [ ] `ast_grep` search refused in read-only sessions: whole tool declares
  `effects={WRITE}` (`src/voidcode/plugins/ast_grep/ast_grep_tool.py:280`).
  Splitting declarations alone is insufficient: CLI search uses `run_process`,
  which `core/services.py` rejects on read-only leases. A read-only execution
  contract or an in-process search implementation is still needed.
- [ ] Exec log paging: byte-vs-line paging was never decided
  (`src/voidcode/plugins/exec/`, background process controls). (Shell command-name
  interception is superseded — explicitly removed, not pending.)
- [x] LSP concurrency policy: manager lifecycle locking serializes local lease
  acquisition/handoff, shutdown and failed-process retirement. Concurrent first-use
  regression verifies exactly one registry lease and complete release on shutdown;
  bypassing the lifecycle lock reproduces the duplicate acquisition. Process-id
  checks prevent stale failure cleanup from removing a newer local entry.
- [ ] Event-subscription ownership stays split (`terminal_app` + web client) until a
  second consumer demonstrably reimplements reconnect/cursor bookkeeping.
- [ ] Derived settled signal (`agent_settled`-equivalent): defer until a client
  demonstrably reimplements it twice; `drain` + run-end is enough today.
- [ ] `show --events` stays an untyped rescue path; confirm no client depends
  on its shape before any typing work.
- [ ] Non-text output rendering: TUI shows `[mime]` placeholders for artifact
  references today; richer rendering only if a client demonstrably requires it.


## Unfinished review coverage (not confirmed bugs)

The retired plugin audit explicitly warned its per-package review was
incomplete. The suite passing does not close these; each needs a source read:

- [ ] compaction: lifecycle + configuration final pass.
- [ ] cron: state/tool ingress + configuration final pass. Progress: unknown
  config and bool/float schema versions are rejected; invalid kind types fail
  visibly; loop and tool creation now share the same capacity guard (49 unit
  cases pass). Final lifecycle/configuration audit remains open.
- [ ] delegation: task operations, cancellation, child diagnostics, optional queries.
- [x] effectiveness: factory accepts only injected `providers`; unsupported fields
  fail visibly. Covered by `test_effectiveness_factory_rejects_unsupported_config`.
- [ ] exec: artifact/result fields, UTF-8 read boundaries, factory configuration.
- [ ] files: plugin adapter + search error/completeness handling + factory
  configuration. (Shared `capabilities/files/` paths — glob/grep/read/write/
  edit/patch/replace — were fully reviewed by the newer retired capability
  audit; not re-opened.)
- [ ] goal: state ingress invariants, configuration, repeated edit-context notice.
- [ ] hooks: plugin callbacks, policy boundary, command-hook compatibility,
  process lifecycle.
- [x] plan: state and configuration final source pass completed. Unknown options
  are rejected (injected `providers` accepted); schema versions reject bool/float
  coercion. Tests cover strict fields, pure deactivation, explicit status action
  and user-frozen read-only policy preservation.
- [ ] providers: shared adapters, configuration, errors, teardown not covered
  by the retired capability audit's `capabilities/providers/` review.
- [x] review: command/configuration ingress source pass completed. Unsupported
  config is rejected; target requirements, type errors, Git failures and prompt
  bounds fail visibly. Child scope stays inherited/read-only without widening;
  preparation performs no Git/provider/child work. Unit and integration cases
  cover those boundaries.
- [x] session_info: factory accepts only injected `providers`; unknown plugin
  options fail visibly. Covered by factory ingress tests.
- [ ] skills: rulebook custom frontmatter, content/read bounds, config/discovery
  validation.
- [ ] web: malformed-item semantics, response/output bounds, factory
  configuration, external API verification.
- [ ] Per-plugin-id aliasing: plugins identifying their own tools/owner by
  constant (e.g. `todo/OWNER`, `OWN_TOOL_NAMES` in
  `src/voidcode/plugins/todo/todo_plugin.py:28-29,848`) are not peer
  dependencies, but admitting the same source under a different id is not
  comprehensively checked. (Cron already uses the lease's actual owner.)

## Deferred type hardening (from SDK investigation, no commitment)

- [ ] `SessionRow.status`, `RunResult.status`, `CoreResult.status`,
  `SessionRecord.status` are plain `str` (`src/voidcode/sdk/client.py:39,178`,
  `src/voidcode/core/engine.py:442`, `src/voidcode/core/storage/store.py:53`).
  Each has a distinct producer/consumer set (store seeds `idle`, engine commits
  run terminals, clients project gates); any literal typing must handle each
  shape separately, not one invented union.
- [ ] TUI `_gate_changes` (`src/voidcode/tui/terminal_app.py:237-268`) still
  imports private `EventType` and string-matches core vocabulary; the SDK
  `parse_sse_frame` migration is done (`tui/client.py:306`), only the gate
  mapping remains TUI-local. Extract to `sdk/gates.py` only if a second
  consumer needs it.
