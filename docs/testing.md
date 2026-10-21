# Testing Policy

VoidCode uses Python and frontend tests to defend core contracts, plugin boundaries, execution lifecycles, and user-facing behavior. Python tests run with `pytest`; frontend component tests use Vitest. `mise` orchestrates project checks.

## Test Suite Structure

Python coverage is organized around the core and its plugin seam:

- **Unit tests (`tests/unit/`)**:
  - `tests/unit/core/`: core behavior — admission, storage schema and append semantics, runtime lifecycle, process leases, pre/post-tool gates, lifecycle hooks, read coverage, overlays, request windows, delegation bounds, nested tools, bundled-source boot, and the upgrade seams (`test_digests.py`, `test_upgrade_images.py`, `test_upgrade_preflight.py`, `test_upgrade_adoption.py`, `test_upgrade_drain.py`).
  - `tests/unit/plugins/`: bundled plugin behavior asserted through each plugin's declared tools and commands — files, exec, web, providers (catalog, dispatch, reasoning/auth/streaming contracts, panels), session_info, todo, skills, agents, review, delegation, btw, cron, compaction, hooks, user_shell, effectiveness, and goal. `ast_grep` and `mcp` have no dedicated behavior suite yet; they are exercised only by the SDK contract and bundled-source boot tests. `lsp` is covered by `tests/unit/plugins/test_lsp_managed.py` and `tests/integration/test_lsp_managed.py`.
  - `tests/unit/sdk/`: the shared contracts — configuration parsing, command/tool/view models, strict codec behavior, and the import-direction guard that keeps the SDK free of implementation dependencies.
  - `tests/unit/test_core_tui.py`: the TUI's view widgets plus the app loop over stub clients — on-demand views, command results, decision gates, and reconnect. `tests/unit/core/test_tui_stream_cancel.py` proves quit/switch cancels a real idle HTTP follow stream with generation isolation. Real-terminal PTY acceptance (composer, overlays, scrollback, remote path) remains the manual run below.
- **Integration tests (`tests/integration/`)**: multi-component workflows over a real core and real storage:
  - `tests/integration/test_core_activation.py`, `test_core_governance.py`, `test_core_transport.py`, `test_core_vertical.py`, `test_core_upgrade.py`, `test_core_upgrade_transport.py`.
  - `tests/integration/test_*_plugin.py`: one end-to-end suite per product plugin (agents, btw/effectiveness, compaction, cron, delegation, goal, hooks, plan, review, skills, todo, user_shell).
- **Frontend (`frontend/`)**: Vitest component tests for the core app and its eight view renderers (`frontend/src/core/views.test.tsx`). See `frontend/README.md` for the frontend workflow.

## Core Testing Principles

1. **Assert Observable Behavior**:
   Prefer durable events and state, tool results, command results, rendered views, bounds, and error behavior over snapshots of implementation details. Do not port assertions on legacy wire payload shapes, legacy database rows, or paint sequences.

2. **Keep Fixtures Behavioral**:
   A collaborator fixture should drive a meaningful consumer path; its call count or forwarded arguments alone do not establish coverage.

3. **Drive Providers Through a Loopback**:
   `tests/provider_loopback.py` runs a real OpenAI-compatible HTTP server on `127.0.0.1` with canned replies and records every request body, header set, and response. Core and plugin suites point the real `providers` plugin (provider name `endpoint`) at that endpoint: no provider credentials are needed, and the exercised path is the genuine wire adapter rather than a stub. Tests using the loopback are the default for provider behavior.

4. **Exercise Plugins Through Their Public Surface**:
   Plugin tests admit the plugin and invoke its declared tools/commands, so the same registration, argument schema, effect classification, and service surface a user gets is what the test covers.

5. **Run Deterministically**:
   No test may reach the outside world by accident. Standard runs require no network and no external binaries.

## Offline Guards (`tests/conftest.py`)

`tests/conftest.py` installs autouse fixtures that keep the suite deterministic:

- **`_deny_live_sockets`**: fails immediately on any real non-loopback socket connect (including `ssl.SSLSocket`). Loopback stays open, because integration tests run real HTTP servers there; AF_UNIX sockets stay open too. Plugin code runs in the core process, so this guard covers the test and core processes alike; a plugin's own network use is the declared `network` effect, exercised against loopback endpoints.
- **`_isolated_xdg_runtime_dirs`**: gives each test its own `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, and `LOCALAPPDATA`, so core state never leaks between tests or into the developer's directories.
- **`allow_live_network`**: the explicit opt-out. Only a test whose subject *is* the live network may request it.

Two former guards — live model discovery and the doctor's external `<tool> --version` probes — were deleted together with those subjects; provider coverage now runs offline through the loopback and declared configuration instead.

## Fixtures

- `tests/fixtures/reload_plugin.py` and `tests/fixtures/s5_plugin.py` are small external plugins used to prove admission, activation, state migration, deactivation, and service surfaces without shipping product code.

## Running Tests

```bash
# Full gate: lint, typecheck, Python tests, frontend checks
mise run check

# Same hooks the commit gate runs
uv run pre-commit run --all-files

# Python suite
mise run test
mise run test:coverage

# Frontend component tests
mise run frontend:test
```

Or run `pytest` directly:

```bash
# The whole suite with slow cases reported
uv run python -X utf8 -m pytest -n auto --dist worksteal --durations=20

# One tier
uv run python -X utf8 -m pytest tests/unit/core/
uv run python -X utf8 -m pytest tests/integration/test_core_transport.py

# One file
uv run python -X utf8 -m pytest tests/unit/plugins/test_files.py
```

## Manual & Acceptance Verification

- **CLI acceptance**: with a workspace that has a valid `voidcode.json` and a reachable provider, run `uv run python -m voidcode.core run "read README.md"`; then inspect with `... sessions` and `... show <session_id> --events`.
- **TUI acceptance**: `uv run python -m voidcode.core` (bare) in a real terminal exercises the inline composer, overlays, and commit-once scrollback; the same binary with `--server http://127.0.0.1:<port> --token <token>` exercises the remote client path.
- **Transport acceptance**: `uv run python -m voidcode.core serve --port 8000`, then `curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/api/commands` and open `http://127.0.0.1:8000/` for the served web client (when a frontend bundle is built).
