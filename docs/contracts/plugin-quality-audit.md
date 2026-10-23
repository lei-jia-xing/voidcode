# Plugin quality audit

This records source review and behavioral evidence for the 20 bundled plugins.
A reviewed area is not evidence that the rest of the package has been audited.
The aim is clear semantics, fewer implementation paths, and useful fields; it is
not a requirement to change every plugin or to add a dependency.

| Plugin | Reviewed areas and changes | Remaining review |
| --- | --- | --- |
| `ast_grep` | Shared file guards/diagnostics replace two identical private modules. Strict argument model; lease-owned process execution; reject incomplete/malformed native results before rewrite. Native-result/guard and bundled-dispatch tests. | Whole-tool WRITE declaration also blocks search in read-only sessions; result metadata and rewrite bounds need a final pass. |
| `btw` | Command ingress, provider failures, current conversation window, no transcript pollution, pending tool approval. Uses contextual `model_query`; unit and real-provider integration tests. | No specific finding remains in this small package. |
| `compaction` | Certified context boundaries, resource references, tool-free model summary, paging and truncation; focused unit/integration tests. | Recheck lifecycle/configuration as part of final package pass. |
| `cron` | Typed wakeup facts, lease owner and stale wakeup identity; focused tests. | Schedule parser, loop command, configuration and deactivation. |
| `delegation` | Scope inheritance, Markdown agent discovery/validation, shared frontmatter and removal of inert profile machinery; unit/integration tests. | Task operations, cancellation, child diagnostics and optional queries. |
| `effectiveness` | Typed public facts replace classification by tool/event names; focused tests. | State projection, remaining fields, commands and deactivation. |
| `exec` | Adapter delegates to shared capabilities. | Shared execution implementation and configuration. |
| `files` | Adapter delegates to shared capabilities. | Shared tools, configuration and failures. |
| `goal` | Import/coupling scan only. | State/accounting, lifecycle and user commands. |
| `hooks` | Configuration/executor read; custom argv protocol duplicates process execution and has several one-field wrappers. | Plugin callbacks, policy boundary, command-hook compatibility and process lifecycle. |
| `lsp` | Registration and supervised-service entry point read. | Manager, protocol failures, inventory and teardown. |
| `mcp` | Import/coupling scan only. | Manager, cache, protocol failures and teardown. |
| `plan` | Owner-scoped overlays, state, context and deactivation read; removed unused owner constant, corrected exit instruction to `/plan.off`. Restart/read-only integration tests. | Finish state/configuration validation. |
| `providers` | Adapter, typed dispatch panel and dedicated usage surface. | Shared adapters/configuration, errors and teardown. |
| `review` | Inherit parent scope with core-owned read-only stance; real custom-tool and restricted-parent tests. Removed unused Git tree/branch/history APIs, result wrappers/fields and legacy aliases. Git reads have timeouts and disable text conversion; preserve inspection errors. Child states and incremental-review refusals tested. | Final prompt-bound calculation and duplicated base-branch Git reads. |
| `session_info` | Typed context/export/copy, explicit truncation, diagnostic headers; focused tests. | Remaining panel inputs, subprocesses and output semantics. |
| `skills` | One strict shared frontmatter parser, discovery and duplicate validation; focused tests. | Activation/rules, context contribution and unused fields. |
| `todo` | Reminder resets use typed fresh-request facts; focused tests. | Remaining state/commands, mutations and deactivation. |
| `user_shell` | Replaced private process runner, temporary captures, kill helpers and result wrapper with SDK `run_process`. Honest timeout/cancel/unknown exit status; removed unused help exports. Real command/output and process-lease tests. | Command cancellation wiring is a core concern: ordinary command contexts currently have no abort signal. |
| `web` | Entry point read; Exa-only description disagrees with multi-provider implementation. | Providers/configuration, result normalization, bounds and errors. |

## User shell output

The command uses the process service's default 256 KiB capture per stream. Excess
bytes are dropped with an explicit marker and `truncated=true`; the event stores
the bounded captures. The displayed output keeps a UTF-8 head and tail with a
10,000-byte content budget plus its truncation marker. A timeout/cancellation has
an unknown exit code and a separate status, never a fabricated child exit code.

See also [the coupling audit](./plugin-coupling-audit.md). Final completion requires
reviewing the remaining areas above and running the repository's checks on the
resulting source.
