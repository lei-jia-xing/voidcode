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
| `cron` | Typed wakeup facts, lease owner/stale wakeup identity, loop parsing and deactivation read. User selected croniter five-field syntax: removed the custom numeric validator and obsolete CLI advice. 164 schedule/plugin/integration tests pass, including named weekdays/months and actual fire dates. | Final state/tool ingress and configuration pass. |
| `delegation` | Scope inheritance, Markdown agent discovery/validation, shared frontmatter and removal of inert profile machinery; unit/integration tests. | Task operations, cancellation, child diagnostics and optional queries. |
| `effectiveness` | Public-fact projection, counters, paging, report fields and command read. Removed constant session count, fabricated unattributed-event count and static privacy claims; integration checks actual absence of arguments/result text. Stateless: no deactivation transform needed. | Factory configuration validation. |
| `exec` | Adapter, shell execution/capture/artifacts/progress and background-process argument ingress read. Strict Pydantic arguments reject unknown/coercible values; shell timeout now matches declared 1–600 seconds. Removed redundant validation exception wrapping, use typed progress field, corrected character-preview naming. Background lifecycle, readiness, log scan and controls read. Persisted identities now use strict existing Pydantic models: corrupt records fail instead of disappearing or inventing identity fields. 95 shell/background/built-in/dispatch/real-process tests pass. | Shell interception and byte-vs-line log paging await user choices; artifact/result fields, UTF-8 read boundaries and factory configuration. |
| `files` | Adapter delegates to shared capabilities. | Shared tools, configuration and failures. |
| `goal` | State parser, objective attachments/context, all tools/commands/views, accounting observer and deactivation read. Shared tool payload, dataclass-owned defaults and a single resume transition replace duplicate code. Failed budget edits no longer write objective attachments. Usage accounting now has one lifetime accumulation path and distinct open/closed-window budget decisions. 30 unit and 10 real-core integration tests pass, including absent/partial metrics and paused late usage. | State ingress invariants/configuration and repeated edit-context notice. |
| `hooks` | Configuration/executor read; custom argv protocol duplicates process execution and has several one-field wrappers. | Plugin callbacks, policy boundary, command-hook compatibility and process lifecycle. |
| `lsp` | Registration, configuration/contracts, server/preset resolution, root discovery and supervised-service entry point read. Configuration now uses strict Pydantic ingress; removed silent filtering and enabled tri-state helpers. Inventory now consumes a uniform typed per-workspace configuration contract; removed unused configured/available state fields. Request/lifecycle/framing paths read: cleanup retains original failure, cancellation differs from timeout, unconfirmed stops remain explicit, and EOF stops the failed connection. Document synchronization, initialization and registry source read. Malformed initialize capabilities and non-ASCII headers now fail visibly; missing-binary advice uses the current voidcode.json path. Server requests receive a Method-not-found response instead of being skipped or confused with a same-ID client response; notifications receive no response. 41 configuration/framing/managed/core integration tests pass, including real numeric/string server request IDs, bad initialization and server exit. Tool ingress/rendering read: strict Pydantic arguments replace silent numeric conversion/clamping and correctly materialize nested diagnostic arrays; call-hierarchy execution shares the final request path. Symbol names derive from the installed protocol enum. Tool results now validate through the installed lsprotocol response registry/converter; malformed data stays distinct from valid empty results, unchanged diagnostic reports keep an unknown item count, and references reuse location rendering instead of the generic one-item fallback. Registry concurrency/stale-lease/configuration-conflict cases pass. The combined LSP run passed 66 tests; 28 tool/registry tests then passed after the three reference regressions. | Empty prepareCallHierarchy handling and final output-field review. |
| `mcp` | Strict Pydantic configuration, complete Draft 2020-12 validation with local-only references, and real SDK stdio/core integration tests. Removed unused disk cache, diagnostic collector, initialization metadata and forwarding/retry methods. Startup failures remain inspectable; unknown and known-empty inventories remain distinct; shutdown reaps the child. | Remote HTTP failure/teardown, complete content/media rendering, remaining state fields and concurrency. Call-error state fixed: connection cleanup retains failure; recoverable RPC errors retain the running connection. Crash/reconnect and recoverable-error integration tests pass. |
| `plan` | Owner-scoped overlays, state, context and deactivation read; removed unused owner constant, corrected exit instruction to `/plan.off`. Restart/read-only integration tests. | Finish state/configuration validation. |
| `providers` | Adapter, typed dispatch panel and dedicated usage surface. | Shared adapters/configuration, errors and teardown. |
| `review` | Inherit parent scope with core-owned read-only stance; real custom-tool and restricted-parent tests. Removed unused Git tree/branch/history APIs, result wrappers/fields and legacy aliases. Git reads have timeouts and disable text conversion; preserve inspection errors. Child states and incremental-review refusals tested. Complete UTF-8 prompt uses the shared SDK child limit; diff result carries its actual merge base, removing repeated Git reads. 42 unit, 12 integration/SDK and 2 core-bound tests pass. | Final command/configuration ingress pass. |
| `session_info` | All panel dispatch, header/transcript paging, usage, export/copy and bounds read. Uses existing SDK JSON conversion for nested immutable arguments/bodies. Existing markdown-it-py (via Rich) is now direct: replaces the private fence parser and preserves code whitespace. Truncated history cannot be presented as the latest context. 16 panel unit tests pass. | Factory configuration; final repository gate after this change. |
| `skills` | Shared frontmatter, discovery/enable rules, registry, on-demand tool, catalog, touched-path observer/context and rulebook source read. Removed unused full-body context renderer and unconnected skill selection parameters. Escape catalog XML metadata; malformed/missing rule state fails instead of silently resetting. 52 unit and 6 real-core integration tests pass. | Rulebook custom frontmatter, content/read bounds and final config/discovery validation. |
| `todo` | Reminder resets use typed fresh-request facts. State/reminder ingress and configuration read; fixed two same-name parsers where command parsing silently replaced strict reminder cycle-ID validation. Empty/non-string IDs in all three reminder sections are rejected. Operations, rendering, reminder policy/observer and deactivation read. Mutations preserve cycle budgets/cursors; failed or stopped runs no longer consume unsent reminders. Removed unused batch mutation counting and source constant, reused table rendering and parsed state. Final state pass rejects coercible summary/version values; unknown declared effects fail visibly instead of being discarded as read-only. 60 unit and 7 real-core integration tests pass on the resulting source. | No specific finding remains; repository-wide final gate pending. |
| `user_shell` | Replaced private process runner, temporary captures, kill helpers and result wrapper with SDK `run_process`. Honest timeout/cancel/unknown exit status; removed unused help exports. Real command/output and process-lease tests. | Command cancellation wiring is a core concern: ordinary command contexts currently have no abort signal. |
| `web` | All provider adapters, selection, normalization and HTTP errors read. Strict Pydantic arguments now match the declared 1–20 limit and reject coercion/unknown fields; removed unused provider labels, credential names, unreachable fallback and HTTP-error parameter. Corrected Exa-only documentation. 28 mocked HTTP/argument tests pass. | Factory configuration, malformed-item semantics, response/output bounds and external API verification. |

## User shell output

The command uses the process service's default 256 KiB capture per stream. Excess
bytes are dropped with an explicit marker and `truncated=true`; the event stores
the bounded captures. The displayed output keeps a UTF-8 head and tail with a
10,000-byte content budget plus its truncation marker. A timeout/cancellation has
an unknown exit code and a separate status, never a fabricated child exit code.

See also [the coupling audit](./plugin-coupling-audit.md). Final completion requires
reviewing the remaining areas above and running the repository's checks on the
resulting source.

## Verified checkpoint

On 2026-10-23, source commits `522d2d9`, `a187f6b`, and `2acf759` passed the
full Python suite: **1269 passed, 1 skipped** (369.59 seconds). Ruff lint/format,
source type checking, generated API checks and commit hooks also passed.
This verifies the changes made so far; the remaining package review areas in the
table still prevent claiming that the complete audit is finished.

### MCP checkpoint

Source commit `6c6d2a9` passed the full Python suite: **1295 passed, 1 skipped,
1 warning** (404.78 seconds). The run suppressed warning details, so that warning's
origin remains unverified. The 26 dedicated MCP tests then passed with normal
warning reporting and produced no warning. A separate diagnostic run with global
`-W error` hit the already-known Google SDK `_UnionGenericAlias` deprecation during
core bootstrap; this does not identify the full-suite warning.

The MCP change reuses installed Pydantic/jsonschema/MCP SDK packages. `referencing`
was already transitive and is now declared directly to provide a closed reference
registry; resolving the lock file added no package. The dependency floor now
reflects the MCP 2.x API used by the implementation.

### Lifecycle, LSP, and schedule checkpoint

Commits `8c67a4b` and `268a4af` passed the full Python suite: **1308 passed,
1 skipped** (363.63 seconds), with warning details enabled and no warning emitted.
The earlier unlocated warning did not reproduce. Crash/reconnect and recoverable
RPC errors are covered by real MCP integration tests; LSP strict configuration
also retains the existing real-server coverage.

After that checkpoint, the user selected croniter's five-field grammar. The Cron
change passed **164** schedule/plugin/integration tests. It removes the custom
numeric-only validator, accepts library aliases/extensions, retains the five-field
limit and leaves fire-time iteration to the same existing library. The full-suite
checkpoint above predates this scoped Cron change.

### Cross-plugin checkpoint

Commit `a307feb` passed the full Python suite: **1360 passed, 1 skipped**
(349.32 seconds), with no warning emitted. This includes the recent review,
goal, todo, exec and skills changes. The subsequent session_info fixes are
covered separately and are not part of that full-suite checkpoint.

The session_info change declares markdown-it-py directly to parse CommonMark
fences. It was already installed through Rich; the offline lock update resolves
the same 97 packages and adds no package.

### LSP bidirectional messages

The client advertises no server-request capabilities. Unsupported incoming
requests receive JSON-RPC error `-32601`, retaining the server's request ID;
notifications receive no response. Classification uses the presence of `method`
before considering a client response ID. This follows the
[JSON-RPC specification](https://www.jsonrpc.org/specification).
Real stdio tests exercise notifications followed by server requests with the
same numeric ID, a different numeric ID, and a string ID, then verify that
the original client request still completes.
