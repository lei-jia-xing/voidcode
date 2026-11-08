# Slash command interaction acceptance

Command IDs, schemas, effects, and revision checks remain authoritative. TUI
and web display catalog paths with spaces. CLI/RPC accept the same structured
arguments as before. Interaction is a client presentation of those contracts.

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
| `agents`, `agents.show` | List returns choices; detail returns Markdown; bare show prepares dynamic name options without invocation. SDK, plugin, TUI/Web and real RPC tests cover selection and revision binding | Empty/error discovery and long profile details still need physical terminal/browser acceptance |
| `goal` | `goal/goal_plugin.py`: empty state opens creation; existing goal shows bounded status/objective/usage details and applicable Edit/Pause/Resume/Clear actions. Edit prepares the prefilled form without mutation | Physical client acceptance for lifecycle actions and large-objective loading |
| `goal.set`, `goal.edit` | Prepared multiline creation and prefilled editing; natural objective text via catalog `text_argument`; explicit JSON retained | Physical client acceptance for large objective loading and validation/refusal recovery |
| `goal.pause`, `goal.resume`, `goal.clear`, `goal.load_objective` | Typed lifecycle commands / exact-read form helper | Actions offered from goal details; physical client acceptance remains; replacement confirmation and exact-read helper remain governed |
| `todo`, `todo.clear`, `todo.add`, `todo.edit` | Paged Markdown with Add/Edit actions, multiline creation and state-filtered task operations; fixed revision rejects stale edits. Real Chromium and 80/40-column PTY cover creation, selection, passive replay and readable footer | Physical acceptance of every task operation and concurrent-edit recovery |
| `plan`, `plan.off`, `plan.status` | Status and mode transitions return the explicit next-mode form; reading status does not toggle. Unit interaction test covers enter/status/leave | Physical client acceptance; existing overlay and frozen-policy integration tests remain authoritative |
| `skills`, `skills.show` | Choices and paged details show source/status/instructions, disabled/empty recovery; Unicode bounds tested. Real Chromium covers page navigation and plugin disable after reload | Physical terminal acceptance of long skill details; actual enable/disable remains configuration admission |
| `workflow.run` | Bare command prepares a configured workflow select and task editor; no preparation side effect. Natural name input and full structured JSON task remain available | Physical client acceptance of selecting, editing task, and starting only on submit |
| `workflow.status`, `workflow.pause`, `workflow.resume`, `workflow.cancel` | Bare commands prepare state-filtered run selectors. Status shows paged nodes, readable UTF-8 result pages, and applicable Pause/Resume/Cancel actions; cancellation retains governed confirmation | Physical client acceptance of result paging, lifecycle actions and cancellation confirmation |
| `loop` | Bare command prepares a multiline editor with interval example/default; creation offers Browse/Cancel. `loop.list` pages session schedules, `loop.delete` shares tool deletion/disarming. Natural input remains available; preparation does not schedule | Physical client acceptance for list/cancel and claimed-run semantics |
| `review` | Bare slash input prepares a target/focus form with default workspace changes and branch/SHA requirements; explicit natural focus and structured invocation remain available. Preparation performs no Git/provider/child work; focus survives all target types | Physical client acceptance of target selection, missing-argument recovery and governed child startup |
| `compact` | One-step explicit action returns structured accounting: event range, covered/dropped bytes, summary source and measured tokens; absent measurements remain unknown. Unit tests cover success, no-op and empty session | Physical client acceptance for accounting and refusal readability |
| `mcp`, `lsp` | Passive service selectors open status/error details; MCP tools page through the full cached surface and distinguish undiscovered from known-empty. Unit tests verify selection, failures, disabled recovery and 61-tool Unicode pagination | Physical client acceptance of server navigation and recovery; configuration changes still require admission |
| `effectiveness` | Readable paged diagnostics show tool outcomes, retries/truncation, model edits and usage coverage; unknown tokens/cost stay explicit, full structured data remains available | Physical client acceptance of diagnostics navigation and empty/unknown states |
| `btw`, shell (`!`) | Separate question / supervised shell commands | Preserve natural raw text and existing cancellation/approval semantics; action receipts may remain concise |

Paths in the table are relative to `src/voidcode/plugins/`. Core help/status and
client navigation also need keyboard and empty-state acceptance; they must keep
working with zero plugins. Do not replace the catalog with a client-side list of
plugin command names.

## Completion acceptance

- `/` and `/ag` show one root `agents`, with its description and namespace marker.
- `/agents ` offers `show`; bare `/agents` remains executable with Enter.
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

The Chromium no-provider scenario additionally verifies review target preparation
and server validation with draft retention; schedule creation, list and cancellation;
usage after explicit hot reload; context/effectiveness empty states; MCP/LSP
configuration recovery notices; goal creation without submitting; and passive
replay after refresh. These checks do not establish non-empty server navigation,
all goal lifecycle actions, workflow execution UI, or terminal acceptance.
Hot reload now preserves the budget reader binding, verified by invoking `/usage`
after a revision adoption in the same browser scenario.
