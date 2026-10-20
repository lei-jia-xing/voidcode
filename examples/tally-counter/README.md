# Tally counter: a runnable third-party plugin

An ordinary external plugin — one model tool (`tally_add`), one user command
(`tally`), one view (`tally`) — over shared per-session state. No bundled
privileges, no kernel imports, no new dependencies.

## Layout

- `tally_plugin.py` — tool, command, view, and `create_plugin` entrypoint.
- `voidcode-plugin.json` — manifest; `files` pinned by the canonical generator,
  never hand-edited.

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

External sources need explicit startup trust by plugin id (trust follows the
canonical path the configuration resolves to, recorded once per revision):

```bash
uv run python -m voidcode.kernel --trust-plugin tally-counter catalog
```

## Run

```bash
export WS=/tmp/tally-ws STATE=/tmp/tally-state
uv run python -m voidcode.kernel --workspace "$WS" --state-dir "$STATE" \
  --trust-plugin tally-counter create-session --session-id demo
REV=$(uv run python -m voidcode.kernel --workspace "$WS" --state-dir "$STATE" \
  --trust-plugin tally-counter catalog | python -c "import json,sys; print(json.load(sys.stdin)['revision_id'])")
uv run python -m voidcode.kernel --workspace "$WS" --state-dir "$STATE" \
  --trust-plugin tally-counter invoke demo tally --arguments '{"delta": 3}' --expected-revision "$REV"
uv run python -m voidcode.kernel --workspace "$WS" --state-dir "$STATE" \
  --trust-plugin tally-counter views demo
```
Expected: `invoke` returns `status: ok` with `data: {"total": 3,
"updates": 1}`; `views` includes the `tally` view with value `3 (1
update(s))`. A second invoke with `{"delta": 2}` yields `total: 5`,
`updates: 2` — both surfaces share the same optimistic-versioned state.

Omit the provider for command/view-only runs (no model traffic): drop
`providers` from `voidcode.json` and leave `model` unset (`"model": {}`). The
model-tool smoke (`run` with the loopback provider driving `tally_add`) is
covered by `tests/integration/test_tally_example.py`, which needs no
credentials.

## After editing the plugin

Repin identities (any source edit is a new identity; admission refuses drift):

```bash
uv run python scripts/generate_plugin_identities.py --plugin-dir examples/tally-counter
uv run python scripts/generate_plugin_identities.py --plugin-dir examples/tally-counter --check
```

Then restart with `--trust-plugin tally-counter` (new external trust is a
startup decision) or, when the path is already authorized, `kernel.reload`.

## Contract notes

- Tool and command declare `SESSION`: internal state, auto-allowed in every
  approval mode, never refused by `read_only`.
- Registered definitions must equal the manifest declarations exactly; both
  are derived from the admitted manifest at `create_plugin` time, so drift
  is impossible by construction.
- The full author contract (manifest/identity, scope/approval, pinned
  revisions, errors, state migration, activation, diagnostics) lives in
  `docs/contracts/plugin-authoring.md`.
