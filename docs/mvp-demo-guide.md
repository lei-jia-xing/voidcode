# MVP Demo Guide

This document defines the canonical MVP demo scenario and end-to-end verification checklist for VoidCode. It is grounded in the current implemented behavior of the deterministic read-only runtime, session persistence, and HTTP transport.

## Preconditions

- Python 3.14 environment managed by `uv`.
- Repository initialized with `mise install && uv sync --extra dev`.
- A sample file exists in the workspace (e.g., `README.md`).
- No active `.voidcode/sessions.sqlite3` is required, but persistence should be verifiable.

## Canonical Demo Flow

This flow proves the core loop: execution, observation, persistence, and recovery.

1. **CLI Execution**: Run a governed read-only task.
   ```bash
   uv run voidcode run "read README.md" --workspace . --session-id demo-session
   ```
   *Expected Result*: The CLI prints `EVENT` logs for each stage (request received, tool lookup, etc.) and finishes with a `RESULT` block showing the file contents.

2. **Session Persistence**: Verify the session was recorded.
   ```bash
   uv run voidcode sessions list --workspace .
   ```
   *Expected Result*: A table showing `demo-session` with status `completed`.

3. **Session Resume**: Replay the session without re-executing the tool.
   ```bash
   uv run voidcode sessions resume demo-session --workspace .
   ```
   *Expected Result*: The CLI re-renders the identical `RESULT` block from the persisted state.

4. **HTTP Transport Observation**: Serve the session list via API.
   ```bash
   # Terminal A
   uv run voidcode serve --workspace . --port 8000

   # Terminal B
   curl http://127.0.0.1:8000/api/sessions
   ```
   *Expected Result*: Terminal B receives a JSON array containing `demo-session` metadata.

## Verification Ladder

A task is considered "MVP-demoable" only if it passes all steps below.

### 1. Unit Layer
- **Contracts**: Ensure `voidcode.runtime.contracts` types are respected.
- **Diagnostics**: `mise run typecheck` must return zero errors.
- **Command**: `uv run pytest tests/unit/`

### 2. Integration Layer
- **Runtime Loop**: Verify the full `CLI -> Runtime -> Graph -> Tool` path.
- **Persistence**: Ensure SQLite state survives process restarts.
- **Command**: `uv run pytest tests/integration/test_read_only_slice.py`

### 3. Client Smoke
- **CLI Hygiene**: `voidcode --help` and version checks pass.
- **HTTP/SSE**: Verify streaming serialization and session replay via HTTP.
- **Command**: `uv run pytest tests/integration/test_http_transport.py`

### 4. Manual QA
- **Visuals**: Confirm `EVENT` and `RESULT` logs in CLI are readable and not garbled.
- **Serve**: Confirm `voidcode serve` handles concurrent `GET /api/sessions` requests.

## Evidence Bar

To call the MVP demoable, contributors must provide:
- Output of `mise run check` (all green).
- Screenshot or log of the **Canonical Demo Flow** (Steps 1-4) succeeding on a fresh workspace.
- Verification that `.voidcode/sessions.sqlite3` contains the expected rows after Step 1.

## Boundaries and Known Gaps

### What Works Today
- Deterministic read-only execution (no LLM required).
- Local SQLite session persistence.
- Session listing and resumption.
- Minimal HTTP/SSE transport (sessions, streaming).

### Planned (Not Demoable Yet)
- **TUI Client**: The TUI is currently spec-only (`docs/tui-mvp-spec.md`).
- **Web UI Integration**: The React shell is mock-backed and does not yet consume the real API.
- **Write Approvals**: The contract for `ask/allow/deny` exists, but there is no real write tool to trigger it in the default CLI loop yet.
- **LLM Orchestration**: The LangGraph turn-loop is currently a deterministic placeholder.
