# Testing policy

The Python suite focuses on runtime behavior contracts; `mise run test` runs
the complete unit + integration suite. CLI command handling and TUI rendering
are deliberately not automated-test targets. There are no marker lanes:
no `slow`/`fuzz`/`integration` selection and no fast/slow split to keep in sync.

## What "core" means

A module is core when getting it wrong breaks a user-visible contract of the
local-first runtime:

- runtime governance, execution lifecycle, approvals, storage and resume
- the core complete-turn and native tool-batch engine
- tool contracts plus permission/path safety
- provider protocol, naming and config precedence
- transport route/SSE contracts and the HTTP wire shape
- persisted event/state transitions (store transitions)

## Adding a test

A test must defend an observable runtime or transport contract, boundary,
invariant, transition, precedence rule, or real error path. Assert what a
consumer observes: persisted state, emitted events, or wire payloads.

Do not add: wiring assertions, mock echoes (`assert_called_once_with` on a
collaborator), tautologies, padding rows over the same path, bare
`not throws`/`is not None` checks, or restated constants. A test that would not
fail under a real bug is worse than no test: it costs time and hides the gap.

## Deliberately untested surfaces

CLI behavior and the TUI have no automated tests. Verify them manually when
changing their behavior:

- CLI: `uv run voidcode --help`, `uv run voidcode run --help`, then a real
  configured run such as `uv run voidcode run 'read README.md' --workspace .`.
- TUI: `uv run voidcode tui --workspace .`, enter a prompt, inspect the streamed
  response and scrollback, then exit.

Manual smoke checks are not substitutes for runtime contract tests. Keep
approval, persistence, resume, event, provider, storage, and HTTP behavior
covered at their owning runtime/transport boundaries.

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
