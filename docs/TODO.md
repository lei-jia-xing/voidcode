# TODO

Work preserved from retired audit/plan reports. Unchecked items remain
unfinished; open items cite the file/symbol they concern. No owners or dates.

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
  Decide: split the tool or add per-mode admission.
- [ ] Exec log paging: byte-vs-line paging was never decided
  (`src/voidcode/plugins/exec/`, background process controls). (Shell command-name
  interception is superseded — explicitly removed, not pending.)
- [ ] LSP concurrency policy: `_ensure_running`
  (`src/voidcode/plugins/lsp/lsp_manager.py:484-542`) does unlocked
  check-then-act on `_running_servers`/`_leased_servers` around the locked
  registry `acquire`; overlapping calls for the same session/server may
  double-acquire and the loser may overwrite the local lease. Needs a repro
  to confirm, then decide: manager lock or documented single-flight.
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
- [ ] cron: state/tool ingress + configuration final pass.
- [ ] delegation: task operations, cancellation, child diagnostics, optional queries.
- [ ] effectiveness: factory configuration validation.
- [ ] exec: artifact/result fields, UTF-8 read boundaries, factory configuration.
- [ ] files: plugin adapter + search error/completeness handling + factory
  configuration. (Shared `capabilities/files/` paths — glob/grep/read/write/
  edit/patch/replace — were fully reviewed by the newer retired capability
  audit; not re-opened.)
- [ ] goal: state ingress invariants, configuration, repeated edit-context notice.
- [ ] hooks: plugin callbacks, policy boundary, command-hook compatibility,
  process lifecycle.
- [ ] plan: state + configuration validation.
- [ ] providers: shared adapters, configuration, errors, teardown not covered
  by the retired capability audit's `capabilities/providers/` review.
- [ ] review: command + configuration ingress final pass.
- [ ] session_info: factory configuration final pass.
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
