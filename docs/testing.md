# Testing Policy

VoidCode uses Python and frontend tests to defend core runtime contracts, execution lifecycles, and user-facing behavior. Python tests run with `pytest`; frontend component tests use Vitest and browser acceptance tests use Playwright. `mise` orchestrates project checks.

## Test Suite Structure

Automated coverage is organized into Python and frontend tiers:

- **Python (`tests/`)**:
  - **Unit Tests (`tests/unit/`)**: Focused subsystem behavior:
    - `tests/unit/core/`: Turn engine progression, batch planning, transcript assembly.
    - `tests/unit/runtime/`: Session lifecycle, SQLite persistence, permissions, hook executions.
    - `tests/unit/provider/`: Provider wire adapters, catalog parsing, pricing, reasoning effort.
    - `tests/unit/tools/`: Built-in tool behaviors, argument parsing, output bounds.
    - `tests/unit/tui/`: Composer behavior, terminal rendering, and real-PTY input, cursor, and resize behavior.
  - **Integration Tests (`tests/integration/`)**: Multi-component workflows exercising live SQLite persistence, process crash recovery, HTTP/SSE transports, and CLI interactions.
- **Frontend (`frontend/`)**: Vitest unit/component tests and Playwright browser acceptance tests, including local-loopback provider workflows.

## Core Testing Principles

1. **Assert Observable Behavior**:
   Prefer persisted state, user-visible results, state transitions, bounds, and error behavior over snapshots of implementation details.
2. **Keep Fixtures Behavioral**:
   A collaborator fixture should drive a meaningful consumer path; its call count or forwarded arguments alone do not establish coverage.
3. **Run Deterministically**:
   Automated provider scenarios use local loopback transports with real request and response shapes. Standard test runs do not require external provider credentials.
4. **Measure Test Cost**:
   Use `--durations` to identify slow cases. Reduce repeated setup only while preserving the exercised boundary and its assertions.
5. **Offline Determinism**:
   `tests/conftest.py` installs offline guards to prevent accidental external network access during tests:
   - `_deny_live_sockets`: Blocks non-loopback network connections (loopback HTTP servers remain permitted).
   - `_deny_live_model_discovery`: Stubs live provider model discovery queries.
   - `_stub_external_tool_probes`: Stubs doctor tool version probes.
   Tests that legitimately require real external network access opt in with the `allow_live_network` fixture.

## Running Tests

Run the full repository checks with `mise run check`. Run Python and frontend test tiers independently as needed:

```bash
# Run Python unit and integration tests
mise run test

# Run frontend component tests
mise run frontend:test

# Build the frontend, then run Playwright browser acceptance tests
mise run frontend:e2e

# Run Python tests with terminal coverage summary
mise run test:coverage
```

`mise run frontend:e2e` builds before launching the browser tests. Its provider workflows use local loopback and do not require external provider credentials. Browser E2E is not included in `mise run check`.

Or run Python tests directly with `uv run pytest`:

```bash
# Run the full Python suite and report slow cases
uv run python -X utf8 -m pytest -n auto --dist worksteal --durations=20

# Run a specific test directory
uv run python -X utf8 -m pytest tests/unit/runtime/

# Run a single test file
uv run python -X utf8 -m pytest tests/unit/tools/test_edit_patch.py

# Run a specific test suite file
uv run python -X utf8 -m pytest tests/unit/runtime/test_fact_store.py
```

## Manual & Acceptance Verification

- **CLI Acceptance**: Follow the [Quickstart](../README.md#quickstart) to initialize a workspace with a configured model, create a sample file, and execute:
  ```bash
  uv run voidcode run "read sample.txt" --workspace /path/to/configured-workspace
  ```
- **TUI Acceptance**: Native TUI tests exercise interactive behavior through a real PTY/terminal with isolated XDG directories, including terminal resize, caret shape, input and cancellation/re-entry, and composer behavior.
