# Testing policy

The Python suite focuses on stable runtime and observable boundary contracts;
`mise run test` runs the complete unit + integration suite. CLI/TUI may have
narrow tests for deterministic contracts, while interactive behavior and native
terminal rendering still require isolated manual PTY acceptance. There are no
marker lanes: no `slow`/`fuzz`/`integration` selection or fast/slow split.

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

## Manual CLI and native PTY acceptance

CLI/TUI may use focused subprocess/PTY regression tests for stable, observable
behavior when they exercise the real command and isolated state. Keep them
small; do not build a broad snapshot/mock UI suite. Native rendering, caret,
protocol, and restoration behavior still require an actual PTY/native-console
acceptance run. Never replace terminal responses with fabricated bytes.

- CLI: `uv run voidcode --help`, `uv run voidcode run --help`, then a real
  configured run such as `uv run voidcode run 'read sample.txt' --workspace <private-workspace>`,
  with `sample.txt` created inside that isolated workspace.
- TUI: use a private temporary workspace, `HOME`, `APPDATA`, `LOCALAPPDATA`, XDG config,
  state, cache, and data roots, plus an explicit private `VOIDCODE_DB_PATH` and
  an isolated configured provider (a private OpenAI-compatible loopback for
  offline tests; no no-provider runtime mode). Use a real PTY on Linux/macOS or
  Windows Console session on Windows; record OS, terminal, width, and height.
  Inspect startup, native caret and exact restoration, actions bound by the
  private test keymap (F1/F2/F3 have no defaults), minimum-size overlays,
  scrollback, resize, Unicode input, Esc cancellation, and double Ctrl+C quit
  (the first press clears/arms; the second within 500 ms exits). Do not claim acceptance
  without exercising that platform. Never run acceptance against repository/user state.

## Latest terminal and package-consumer acceptance

Current native UI evidence is Linux/tmux only. A real 80×24 PTY exercised all 7 statusline profiles × 8 composer styles twice (empty and visible-draft states): 112 captures across 56 combinations, with exactly one visible selected status row per capture. The same live application also received raw hostile bracketed paste and authorized tool output; escapes remained printable, title and terminal state remained intact. See `local://tui-current-matrix-control-smoke.md` and `/tmp/voidcode-status-matrix-42z67_tp/proof.json`. The independent default-key proof confirms F1/F2/F3 remain unbound, preserve the draft, create no session rows, exit 0, and restore termios exactly: `local://tui-default-key-cutover-proof.md`. Resize/caret, safe-hold, approval, artifact paging, immutable scrollback, and cancellation cases are indexed at `local://tui-native-evidence-index.md`.

The package-resource regression `tests/unit/agent/test_prompt_compose.py` passed (5 tests, 2.26s). The post-fix normal `mise run check` passed: 1,910 passed, 2 skipped in 150.18s; all configured Python, frontend, schema, and API tasks finished in 151.13s. Log: `/tmp/voidcode-postfreeze.dSNPl1/logs/mise-check-final-prompt-fix.log`. Normal all-files and supplemental pre-commit both exited 0; logs: `/tmp/voidcode-postfreeze.dSNPl1/logs/precommit-all-final-prompt-fix.log` and `precommit-supplemental-final-prompt-fix.log`.

A private VCS-aware wheel and sdist each contain 104 byte-verified non-web resources (100 palettes, index, full theme MIT license, root LICENSE and NOTICE) plus all 5 web-bundle files, with unique archive members. The isolated `python -I` ZIP-wheel consumer loaded all six builtin prompts and rendered 100 themes × 42 roles × four color systems (16,800 renders); all 144 loaded `voidcode` module origins were inside the wheel. Build, archive, and consumer evidence: `/tmp/voidcode-postfreeze.dSNPl1/logs/build-vcs-prompt-fix-final.log`, `package-integrity-prompt-fix-final.log`, and `wheel-import-smoke-prompt-fix-final.log`.

The extracted-wheel Web launcher consumer check passed under an isolated `python -I -u` child using the project `.venv` interpreter and the extracted final wheel, with all 270 observed `voidcode` module origins confined to the extraction (no editable fallback). The real `voidcode web` CLI printed `Local server running at: http://127.0.0.1:47023`; `/` returned HTTP 200 with `<!DOCTYPE html>`, and both GETs returned 200. The harness intentionally sent SIGTERM after checking, then reaped the child (`-15`, EOF); this is not a natural CLI exit-status claim. Logs: `/tmp/voidcode-postfreeze.dSNPl1/logs/web-launcher-smoke-final-prompt-fix2.log` and `.exit`; actual CLI output: `/tmp/voidcode-postfreeze.dSNPl1/launcher-smoke-final-d502gq2c/launcher-output.log`.
This evidence is not cross-platform parity: macOS and Windows native acceptance, a fresh Kitty application GUI run, and pixel/screenshot validation were not performed. Kitty 0.49.2 terminal-protocol lifecycle evidence is separately recorded at `local://tui-terminal-lifecycle-proof.md`.

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
