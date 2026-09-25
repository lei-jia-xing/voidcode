# Testing policy

The Python suite is intentionally core-only. `mise run test` runs the entire
suite (unit + integration in parallel, well under a
minute). There are no marker lanes: no `slow`/`fuzz`/`integration` selection and
no fast/slow split to keep in sync.

## What "core" means

A module is core when getting it wrong breaks a user-visible contract of the
local-first runtime:

- runtime governance, execution lifecycle, approvals, storage and resume
- the graph step engine
- tool contracts plus permission/path safety
- provider protocol, naming and config precedence
- transport route/SSE contracts and the HTTP wire shape
- persisted event/state transitions (store transitions)

## Adding a test

A test must defend an observable contract, boundary, invariant, transition,
precedence rule, or real error path. Write what a consumer observes — exit
status, stdout/stderr shape, persisted state, emitted events, wire payloads.

Do not add: wiring assertions, mock echoes (`assert_called_once_with` on a
collaborator), tautologies, padding rows over the same path, bare
`not throws`/`is not None` checks, or restated constants. A test that would not
fail under a real bug is worse than no test: it costs time and hides the gap.

## Effectively unguarded today

The following areas have no dedicated suite and get tests only when they
change; their measured coverage is incidental (10-46%+ reached through
integration/config suites exercising them in passing, e.g.
`tools/delegation/task.py` 76.2%, `command/models.py` 80.6%,
`doctor/checker.py` 69.1%, `runtime/mcp.py` 39.9%):

- the TUI: `src/voidcode/tui/**` (app, term, region, theme, transcript,
  statusline, composer, overlay, events, keys) and the TUI CLI wiring — the pure
  layers carry unit suites; the `app` loop is covered by the pty integration test
- truly near zero: `runtime/context/projection.py`, `runtime/review.py`,
  `cli/trace.py`, `tools/local_custom.py`
- `runtime/{lsp,mcp,acp,context/rules,tool_call_preview}.py`,
  `tools/{delegation/*,process/*}.py`, `provider/{auth,trace}.py`,
  `doctor/**`, `command/**`, `formatter/executor.py`, `server.py`,
  `cli/tasks_view.py`, `tools/lsp.py`, `acp/stdio.py`,
  `runtime/background/process.py`, `command/loader.py`, `lsp/roots.py`, and the
  trivial `permission_path_helpers.py`
- the frontend component layer: only the store, runtime client, SSE parser,
  wire contracts and pure helpers are tested

Three offline guards in `tests/conftest.py` keep the suite offline on purpose
(`_deny_live_sockets`, `_deny_live_model_discovery`,
`_stub_external_tool_probes`). `tests/unit/test_offline_guards.py` pins their
teeth; the `allow_live_network` fixture opts a single test out.

## Gate

`mise run test` (whole suite) and `mise run test:coverage` are the Python gates;
`mise run check` / `mise run ci` compose them with lint, typecheck, schema and
frontend checks. CI and the release workflow run the same unfiltered suite.

## Recovering a deleted suite

`~2000` non-core tests were removed in one sweep. Any of them can be restored
from history when its area becomes load-bearing again:

```bash
git log --oneline -- tests/unit/doctor          # find the sweep commit
git checkout <sha>^ -- tests/unit/doctor       # restore that suite as it was
```

Restore the files, then trim them to the invariants you actually need rather
than re-adding the whole surface.
