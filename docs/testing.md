# Testing Policy

VoidCode uses Python and frontend tests to defend kernel contracts, plugin boundaries, execution lifecycles, and user-facing behavior. Python tests run with `pytest`; frontend component tests use Vitest. `mise` orchestrates project checks.

## Test Suite Structure

Python coverage is organized around the kernel and its plugin seam:

- **Unit tests (`tests/unit/`)**:
  - `tests/unit/kernel/`: kernel behavior — admission and pinned identities, storage schema and append semantics, host lifecycle, process leases, pre/post-tool gates, lifecycle hooks, read coverage, overlays, request windows, delegation bounds, nested tools, and bundled-source boot.
  - `tests/unit/plugins/`: bundled plugin behavior asserted through each plugin's declared tools and commands — files, exec, web, providers (catalog, dispatch, reasoning/auth/streaming contracts, panels), session_info, todo, skills, agents, review, delegation, btw, cron, compaction, hooks, formatter, user_shell, effectiveness, and goal. `ast_grep`, `mcp`, and `lsp` have no dedicated new-world behavior suite yet; they are exercised only by the SDK contract and bundled-source boot tests.
  - `tests/unit/sdk/`: the shared contracts — configuration parsing, manifest identities, command/tool/view models, and the import-direction guard that keeps the SDK free of implementation dependencies.
  - `tests/unit/plugin_host/`: the host process protocol: strict codec behavior and stdio server semantics.
  - `tests/unit/test_kernel_tui.py`: the inline TUI client (catalog dispatch, ledger commit-once behavior, overlays).
- **Integration tests (`tests/integration/`)**: multi-component workflows over a real kernel and real storage:
  - `tests/integration/test_kernel_activation.py`, `test_kernel_governance.py`, `test_kernel_transport.py`, `test_kernel_vertical.py`.
  - `tests/integration/test_*_plugin.py`: one end-to-end suite per product plugin (agents, btw/effectiveness, compaction, cron, delegation, formatter, goal, hooks, plan, review).
- **Frontend (`frontend/`)**: Vitest component tests for the kernel app and its eight view renderers (`frontend/src/kernel/views.test.tsx`). See `frontend/README.md` for the frontend workflow.

## Core Testing Principles

1. **Assert Observable Behavior**:
   Prefer durable events and state, tool results, command results, rendered views, bounds, and error behavior over snapshots of implementation details. Do not port assertions on legacy wire payload shapes, legacy database rows, or paint sequences.

2. **Keep Fixtures Behavioral**:
   A collaborator fixture should drive a meaningful consumer path; its call count or forwarded arguments alone do not establish coverage.

3. **Drive Providers Through a Loopback**:
   `tests/provider_loopback.py` runs a real OpenAI-compatible HTTP server on `127.0.0.1` with canned replies and records every request body, header set, and response. Kernel and plugin suites point the real `providers` plugin (provider name `endpoint`) at that endpoint: no provider credentials are needed, and the exercised path is the genuine wire adapter rather than a stub. Tests using the loopback are the default for provider behavior.

4. **Exercise Plugins Through Their Public Surface**:
   Plugin tests admit the plugin and invoke its declared tools/commands, so the same manifest validation, argument schema, effect classification, and service surface a user gets is what the test covers.

5. **Run Deterministically**:
   No test may reach the outside world by accident. Standard runs require no network and no external binaries.

## Offline Guards (`tests/conftest.py`)

`tests/conftest.py` installs autouse fixtures that keep the suite deterministic:

- **`_deny_live_sockets`**: fails immediately on any real non-loopback socket connect (including `ssl.SSLSocket`). Loopback stays open, because integration tests run real HTTP servers there; AF_UNIX sockets stay open too. Plugin code runs in the out-of-process host, so this guard covers the test and kernel processes; a plugin's own network use is the declared `network` capability, exercised against loopback endpoints.
- **`_isolated_xdg_runtime_dirs`**: gives each test its own `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, and `LOCALAPPDATA`, so kernel state never leaks between tests or into the developer's directories.
- **`allow_live_network`**: the explicit opt-out. Only a test whose subject *is* the live network may request it.

Two former guards — live model discovery and the doctor's external `<tool> --version` probes — were deleted together with those subjects; provider and formatter coverage now runs offline through the loopback and declared configuration instead.

## Fixtures

- `tests/fixtures/reload_plugin.py` and `tests/fixtures/s5_plugin.py` are small external plugins used to prove admission, activation, state migration, deactivation, and service surfaces without shipping product code.

## Running Tests

```bash
# Full gate: lint, typecheck, plugin identities, Python tests, frontend checks
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
uv run python -X utf8 -m pytest tests/unit/kernel/
uv run python -X utf8 -m pytest tests/integration/test_kernel_transport.py

# One file
uv run python -X utf8 -m pytest tests/unit/plugins/test_files.py
```

## Manual & Acceptance Verification

- **CLI acceptance**: with a workspace that has a valid `voidcode.json` and a reachable provider, run `uv run python -m voidcode.kernel run "read README.md"`; then inspect with `... sessions` and `... show <session_id> --events`.
- **TUI acceptance**: `uv run python -m voidcode.kernel tui` in a real terminal exercises the inline composer, overlays, and commit-once scrollback; the same binary with `--server http://127.0.0.1:<port> --token <token>` exercises the remote client path.
- **Transport acceptance**: `uv run python -m voidcode.kernel serve --port 8000`, then `curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/api/commands` and open `http://127.0.0.1:8000/` for the served web client (when a frontend bundle is built).
