# Tally counter: a runnable third-party plugin

An ordinary external plugin — one model tool (`tally_add`), one user command
(`tally`), one view (`tally`) — over shared per-session state. No bundled
privileges, no core imports, no new dependencies. Plugins are trusted code
equal to the application: there is no manifest, no pinning, and no sandbox.

## Layout

- `__init__.py` — tool, command, view, state schema, and `create_plugin`.

## Setup

Copy this directory anywhere outside the repo (or reference it in place), then
point a workspace at it:

```jsonc
// <workspace>/voidcode.json
{
  "schema_version": 1,
  "model": {"model": "endpoint/test-model"},
  "policy": {"approval_mode": "ask"},
  "providers": {
    "endpoint": {"api_key": "loopback-test-key", "base_url": "http://127.0.0.1:PORT/v1"}
  },
  "plugins": {"tally-counter": {"path": "/abs/path/to/tally-counter"}}
}
```

## Run

```bash
export WS=/tmp/tally-ws STATE=/tmp/tally-state
uv run python -m voidcode --workspace "$WS" --state-dir "$STATE" \
  create-session --session-id demo
REV=$(uv run python -m voidcode --workspace "$WS" --state-dir "$STATE" \
  catalog | python -c "import json,sys; print(json.load(sys.stdin)['revision_id'])")
uv run python -m voidcode --workspace "$WS" --state-dir "$STATE" \
  invoke demo tally --arguments '{"delta": 3}' --expected-revision "$REV"
uv run python -m voidcode --workspace "$WS" --state-dir "$STATE" \
  views demo
```
Expected: `invoke` returns `status: ok` with `data: {"total": 3,
"updates": 1}`; `views` includes the `tally` view with value `3 (1
update(s))`. A second invoke with `{"delta": 2}` yields `total: 5`,
`updates: 2` — both surfaces share the same optimistic-versioned state.

Omit the provider for command/view-only runs (no model traffic): drop
`providers` from `voidcode.json` and leave `model` unset (`"model": {}`). The
`model`-tool smoke (`run` with the loopback provider driving `tally_add`) is
covered by `tests/integration/test_tally_example.py`, which needs no
credentials.

## After editing the plugin

Restart the core, or run `core.reload` in a session: a restart imports
the current source with the recorded configuration, and `core.reload`
re-admits the live configuration as a new generation in-session.

## Contract notes

- Tool and command declare `SESSION`: internal state, auto-allowed in every
  approval mode, never refused by `read_only`.
- Definitions live in `__init__.py` next to the handlers: that module is the
  single source of truth for what this plugin registers.
- The full author contract (registration, scope/approval, revisions, errors,
  state validation/deactivation, activation, diagnostics) lives in
  [plugin authoring](../../docs/contracts/plugin-authoring.md). There is no
  `migrate_state` hook: a stored state a schema increase does not match is
  refused visibly.
