# Testing Policy

VoidCode uses automated tests to defend core runtime contracts, execution lifecycles, and user-facing CLI behavior. Tests are run using `pytest` and orchestrated via `mise`.

## Test Suite Structure

Tests are organized into two primary tiers under `tests/`:

- **Unit Tests (`tests/unit/`)**: Fast, focused tests asserting on specific subsystem logic:
  - `tests/unit/core/`: Turn engine progression, batch planning, transcript assembly.
  - `tests/unit/runtime/`: Session lifecycle, SQLite persistence, permissions, hook executions.
  - `tests/unit/provider/`: Provider wire adapters, catalog parsing, pricing, reasoning effort.
  - `tests/unit/tools/`: Built-in tool behaviors, argument parsing, output bounds.
  - `tests/unit/tui/`: Pure composer state machines, formatting, terminal rendering.

- **Integration Tests (`tests/integration/`)**: Multi-component workflows exercising live SQLite persistence, process crash recovery, HTTP/SSE transports, and CLI interactions.

## Core Testing Principles

1. **Assert Observable Outcomes**:
   Test persisted SQLite state, emitted runtime events, or wire API payloads rather than internal private variables.
2. **Avoid Mock Echoes**:
   Do not add assertions that merely verify mock collaborator invocations (`assert_called_once_with`). A test should fail when system behavior regresses.
3. **Offline Determinism**:
   `tests/conftest.py` installs offline guards to prevent accidental external network access during tests:
   - `_deny_live_sockets`: Blocks non-loopback network connections (loopback HTTP servers remain permitted).
   - `_deny_live_model_discovery`: Stubs live provider model discovery queries.
   - `_stub_external_tool_probes`: Stubs doctor tool version probes.
   Tests that legitimately require real external network access opt in with the `allow_live_network` fixture.

## Running Tests

Run the full suite using `mise`:

```bash
# Run all unit and integration tests
mise run test

# Run tests with terminal coverage summary
mise run test:coverage
```

Or run targeted tests with `uv run pytest`:

```bash
# Run a specific test directory
uv run pytest tests/unit/runtime/

# Run a single test file
uv run pytest tests/unit/tools/test_edit_patch.py

# Run a specific test suite file
uv run pytest tests/unit/runtime/test_fact_store.py
```

## Manual & Acceptance Verification

- **CLI Acceptance**: Follow the [Quickstart](../README.md#quickstart) to initialize a workspace with a configured model, create a sample file, and execute:
  ```bash
  uv run voidcode run "read sample.txt" --workspace /path/to/configured-workspace
  ```
- **TUI Acceptance**: Exercise interactive terminal controls using a real PTY with isolated XDG directories.
