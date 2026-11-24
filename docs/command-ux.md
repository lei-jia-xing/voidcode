# Slash command interaction acceptance

Command IDs, schemas, effects, and revision checks remain authoritative. TUI
and web name commands by their public path: the primary command, then exactly
one space per subcommand level (`/goal set`, `/core reload`, `/workflow run`).
CLI/RPC accept the same structured arguments as before. Interaction is a client
presentation of those contracts.

## Public command paths

A public path is a pure projection of the admitted descriptor: its slash
`client_syntax` when declared, otherwise the machine ID with dots as level
separators. Clients never keep their own command table.

- The dotted machine ID (`goal.set`) stays the catalog/RPC identity used by
  `CommandDescriptor.name`, `command.invoke`, confirmations, and revision
  checks. Typed schemas and revision binding are unchanged.
- A dotted slash spelling (`/goal.set`) is not an alias and is refused.
- Help and completion show each primary root once and group its subcommands
  beneath it. A root that has subcommands but no executable descriptor
  (`/core`, `/workflow`) gets one derived heading instead of disappearing.
  Completion names those children, and a bare namespace that cannot execute
  reports its subcommands instead of a bare unknown-command error.
- CLI `voidcode invoke` accepts the canonical ID or the same space-separated
  public path (quoted, e.g. `voidcode invoke SESSION "goal set"`). RPC/JSON keep
  the canonical ID only.
- `client_syntax` prefixes without a leading slash (`!`) and
  `slash_visible=False` are unchanged: `!` remains the shell escape, and hidden
  helpers stay callable without being advertised on the slash surface.

`CommandResult.presentation` provides a validated, bounded declarative view.
Only a direct successful reply may open choices or an editor. Historical replay
must render the receipt without reopening an editor or dispatching its action.
Both clients submit actions under the revision that produced the presentation.

## Bundled command inventory

The following source-based inventory tracks the interaction work. An existing
command or a readable receipt is not evidence that the acceptance scenario has
passed. Each scenario needs client behavior tests or a recorded manual check.

| Commands | Current behavior / evidence | Interaction acceptance still required |
| --- | --- | --- |
| `/skills`, `/skills show` | List returns choices; detail returns Markdown; bare show prepares dynamic name options without invocation. SDK, plugin, TUI/Web and real RPC tests cover selection and revision binding | Long skill details |
| `/goal` | `goal/goal_plugin.py`: empty state opens creation; existing goal shows bounded status/objective/usage details and applicable Edit/Pause/Resume/Clear actions. Edit prepares the prefilled form without mutation | Physical client acceptance for lifecycle actions and large-objective loading |
| `/goal set`, `/goal edit` | Prepared multiline creation and prefilled editing; natural objective text via catalog `text_argument`; explicit JSON retained | Physical client acceptance for large objective loading and validation/refusal recovery |
| `/goal pause`, `/goal resume`, `/goal clear`, `/goal load_objective` | Typed lifecycle commands / exact-read form helper | Actions offered from goal details; physical client acceptance remains; replacement confirmation and exact-read helper remain governed |
| `/todo`, `/todo clear`, `/todo add`, `/todo edit` | Paged Markdown with Add/Edit actions, multiline creation and state-filtered task operations; the displayed list revision rejects another todo writer and bookkeeping-only churn is retried. Mutating ops require an explicit task/phase target; task text is escaped and delimited as untrusted data in context/reminders. Real Chromium and 80/40-column PTY cover creation, selection, passive replay and readable footer | Physical acceptance of every task operation, target-omission errors, and concurrent-edit recovery |
| `/plan`, `/plan off`, `/plan status` | The core overlay is the only mode truth. Status reports `Plan mode: active`/`inactive` and notes a different read-only reason; reading status does not toggle. Unit interaction test covers enter/status/leave | Physical client acceptance incl. the empty-form Ctrl+S path; overlay, restart, concurrency, malformed-legacy-state, and frozen-policy integration tests remain authoritative |
| `/skills`, `/skills show` | Choices and paged details show source/status/instructions, disabled/empty recovery, and broken entries with bounded diagnostics and recovery actions; Unicode bounds tested. Real Chromium covers page navigation and plugin disable after reload | Physical terminal acceptance of long skill details; actual enable/disable remains configuration admission |
| `/workflow run` | Bare command prepares a configured workflow select and task editor; no preparation side effect. Natural name input and full structured JSON task remain available | Physical client acceptance of selecting, editing task, and starting only on submit |
| `/workflow status`, `/workflow pause`, `/workflow resume`, `/workflow cancel` | Bare commands prepare state-filtered run selectors. Status shows paged nodes, readable UTF-8 result pages, and applicable Pause/Resume/Cancel actions; cancellation retains governed confirmation | Physical client acceptance of result paging, lifecycle actions and cancellation confirmation |
| `/loop` | Bare command prepares a multiline editor with interval example/default; creation offers Browse/Cancel and reports expiry. `/loop list` pages schedules with actual next-fire status, `/loop delete` shares tool deletion/disarming and states that an in-flight run is not interrupted. Natural input remains available; prompts fire as ordinary user text, never slash dispatch; preparation does not schedule | Physical client acceptance for list/cancel and claimed-run semantics |
| `/review` | Bare slash input prepares a target/focus form with default workspace changes and branch/SHA requirements; explicit natural focus and structured invocation remain available. Preparation performs no Git/provider/child work; focus survives all target types. The child is labelled `review-git`, receives only the visible `read`/`grep`/`glob` subset, and the diff is delimited as untrusted data. Plan/read-only startup is covered by integration | Physical client acceptance of target selection and missing-argument recovery |
| `/compact` | One-step explicit action returns structured accounting: event range, covered/dropped bytes, summary source and measured tokens; absent measurements remain unknown. Unit tests cover success, no-op and empty session | Physical client acceptance for accounting and refusal readability |
| `/mcp`, `/lsp` | Passive service selectors open status/error details; MCP tools page through the full cached surface and distinguish undiscovered from known-empty. Unit tests verify selection, failures, disabled-configuration reporting and 61-tool Unicode pagination | Physical client acceptance of server navigation and recovery; configuration changes still require admission |
| `/btw`, `!<command>` (hidden command ID `shell`) | Separate question / supervised shell commands | Preserve natural raw text and cancellation semantics; `!` is client-invoked and has no approval/hook gate, and an idle command is cancellable from the TUI |

Source paths in the evidence column are relative to `src/voidcode/plugins/`. Core help/status and
client navigation also need keyboard and empty-state acceptance; they must keep
working with zero plugins. Do not replace the catalog with a client-side list of
plugin command names.

## Completion acceptance

- `/` and `/sk` show one root `skills`, with its description and namespace marker.
- `/skills ` offers `show`; bare `/skills` remains executable with Enter.
- A root with subcommands but no executable command (`/core`, `/workflow`) shows
  a derived `Subcommands: …` summary instead of an empty description. Typing
  the bare root and submitting names those subcommands; no command is invoked.
- Help lists the root first and indents its subcommands under the full
  `/root subcommand` spelling; the dotted machine ID never appears.
- Tab enters child namespaces. Enter on a namespace with no command enters it;
  Enter on an exact executable selection dispatches only that command.
- Descriptions and required `<argument>` / optional `[argument]` hints come from
  catalog data. Narrow terminals retain the selected command path.
- Up/Down selects, Esc dismisses without clearing the draft, and editing reopens.
- Mid-line editing and existing arguments must never be destroyed by completion.
- Catalog refresh removes disabled capabilities; pending actions retain their
  displayed revision and never silently retry under another revision.

Real client acceptance is recorded in `tests/integration/test_tui_command_browsers.py`
(ANSI PTY, 80/40 columns, light/dark palette hints, create and replay) and
`frontend/e2e/command-browsers.spec.ts` (Chromium keyboard hierarchy, Todo editors,
Skills pages, passive replay, catalog refresh and disabled command rejection).

## Subagent monitor acceptance

The monitor is a hidden, revision-bound live view (`subagents.monitor`, Alt+A
in the TUI, action button in Chromium): a searchable roster of the bound
session's whole descendant subtree plus one scrollable Markdown detail, with
explicit subtree cancellation through the durable confirmation gate. Roster and
detail refresh only from `command.input` reads carrying `refresh_after_ms`
while the view is open; no invoke receipts, no notice consumption, no history replay mounting.

Exercised on real surfaces (not mocks for the UI path):
- `tests/integration/test_tui_monitor.py`: restored persisted roster with both
  nested labels on screen at 80x24 and 40x18; empty roster stays mounted and
  Enter-safe; remote disable drops an open roster plus the chord visibly;
  remote gated live scene (cross-plugin grandchild) browses roster/detail and
  restores draft/caret by real edit; deny leaves the subtree running while
  allow settles it `interrupted`/`owner_cancel` with the requested (not
  stopped) receipt wording, sibling and root completing afterwards.
- `frontend/e2e/monitor.spec.ts`: read-only open via `command.input` only with
  zero invokes; controlled filter with Esc clear; close restores draft/focus;
  reload never resurrects; disable removes button and panel; one gated live
  scenario browses roster/detail, denies then allows with the completion
  receipt in the transcript, verifies running/interrupted/completed states.
- `tests/unit/tui/test_shortcut_bindings.py`,
  `tests/unit/tui/test_live_command_input.py`, `tests/unit/test_core_tui.py`
  choices/viewport cases, and `frontend/src/core/{slash,views}.test.tsx`
  cover chord conflicts, filter/selection stability, refresh cadence and
  failure recovery, gate priority, and poll/navigation non-interference.
- Native approval priority was exercised once per surface with throwaway
  two-stage scripts (not permanent tests): first open the monitor with a draft
  on an idle root, then start one real bundled `write` under policy ask. The
  genuine approval arrives while the monitor is already open. TUI: Alt+A cannot
  replace the gate across refresh; deny restores the monitor and draft/caret.
  Chromium: the shortcut button stays disabled and the focused Deny button
  retains focus across refresh; after deny, closing the monitor restores the
  exact draft/caret. Both runs finish `completed`, with one approval request,
  exactly one durable deny decision, and no target file.
- A throwaway admitted conflict plugin claims `alt+a` alongside subagents.
  Real TUI input names both claimants, dispatches neither, and retains draft/
  caret. Chromium names the conflict while both command buttons still open their
  own presentations. This does not claim browser-level Alt+A interception.

Limits of the current proof: cancellation targets the child subtree in the
exercises above; deeper multi-level queued-followup races and the backend's
rare parked-registration races are backend-suite territory (see the subagents
monitor integration tests), not re-proven here.

The Chromium no-provider scenario additionally verifies review target preparation
and server validation with draft retention; schedule creation, list and cancellation;
usage after explicit hot reload; context empty states; MCP/LSP
configuration recovery notices; goal creation without submitting; and passive
replay after refresh. These checks do not establish non-empty server navigation,
all goal lifecycle actions, workflow execution UI, or terminal acceptance.
Hot reload now preserves the budget reader binding, verified by invoking `/usage`
after a revision adoption in the same browser scenario.
