# Current Implementation State

This document provides a truthful snapshot of the VoidCode repository as of April 2026. VoidCode is currently in the **pre-MVP foundation stage with a stable single-agent loop**.

For the concrete delivery checklist that connects the current repo state to the intended MVP, see [`docs/mvp-todo-plan.md`](./mvp-todo-plan.md). For normative client-facing contracts, see [`docs/contracts/README.md`](./contracts/README.md).

## Overview
The repository contains two primary, independent components:
1.  **Python Backend**: A typed contract layer plus a stable single-agent loop implementation.
2.  **Bun Frontend Shell**: A React-based web interface for the future agent runtime.

**Current integration status**: 🟡 **Initial web integration landed**. The backend exposes a local HTTP/SSE transport, and the frontend now consumes it for session listing, session replay, and streamed runs in the main shell.

---

## Backend (Python)

### Implemented Today
- [x] **Project Structure**: Hatch/UV-ready layout with `src/voidcode/runtime`, `src/voidcode/graph`, and `src/voidcode/tools`.
- [x] **CLI Entrypoints**: `voidcode --help` and `voidcode run "read <path>" --workspace <dir>` both work.
- [x] **Dependency Management**: Fully configured `pyproject.toml` and `mise.toml` for local development.
- [x] **Development Tooling**: Ruff (lint/format), basedpyright (types), and pytest (tests) are integrated and functional.
- [x] **Contract Layer**: Typed session, event, runtime, graph, and tool contracts exist in code.
- [x] **Stable Single-Agent Loop**: The CLI can execute a governed local deterministic multi-step request through runtime, graph, and tool boundaries and emit observable events.
- [x] **Extension Infrastructure Foundations**: The runtime now includes typed configuration and discovery infrastructure for tools, skills, LSP, and ACP.
- [x] **Built-in Tool Provider**: A dedicated `BuiltinToolProvider` handles registration for `grep`, `read_file`, `shell_exec`, and `write_file` through the runtime boundary.
- [x] **Skill Discovery Infrastructure**: Minimal discovery exists for `.voidcode/skills/<name>/SKILL.md` files; the runtime emits `runtime.skills_loaded` events for every run.
- [x] **LSP and ACP Configuration Seams**: Typed configuration carriers and disabled manager/adapter stubs exist for future language-server and transport integration.
- [x] **Minimal HTTP Transport**: A thin backend HTTP layer now exposes `GET /api/sessions`, `GET /api/sessions/{session_id}`, and `POST /api/runtime/run/stream` with SSE chunks serialized directly from the runtime boundary, and it can now be served locally through `voidcode serve`.

### Planned / In-Progress
- [x] **LangGraph Orchestration**: Stable single-agent deterministic loop implementation with support for sequential turn execution, tool resolution, and interrupt/resume.
- [x] **Runtime Services**: Session lifecycle management, sqlite-backed persistence, and approval-resume continuity.
- [x] **Permission Engine**: Governed execution supporting `allow`, `deny`, and `ask` modes with TTY-only inline approval in the CLI.
- [x] **Contract-First Events**: Canonical event schema implemented for turns, tools, and approvals, with automated renumbering for consistency across resumes.
- [x] **HTTP Transport Parity**: The backend HTTP layer now fully exposes session list/resume and run/stream operations on parity with the CLI, including approval-resolution endpoints.
- [x] **Dynamic Tool Registration**: The runtime now includes typed configuration and discovery infrastructure for tools, supporting the `BuiltinToolProvider`.
- [ ] **Skill Execution**: Discovery is implemented (emitting `runtime.skills_loaded`), but the runtime does not yet execute skill logic or provide skill-specific tool contexts.
- [ ] **Real LSP and ACP Integrations**: Configuration seams exist; real process management and transport support are pending.
- [ ] **TUI Client**: A terminal-native user interface is planned to replace the current basic CLI `run` experience.
- [x] **Web Client Integration (initial slice)**: The frontend now consumes the backend transport for session listing, replay, and streamed runs in the current single-agent shell.

---

## Frontend (React + Bun)

### Implemented Today
- [x] **UI Framework**: React 18, Tailwind CSS, and Lucide React shell.
- [x] **Component Library**: Layout, navigation, and message-thread UI components.
- [x] **Runtime-backed MVP state path**: Zustand stores now drive the main session/task/activity UI from runtime-backed session data and streamed events.
- [x] **Frontend Tooling**: Vite-based dev server with Bun support, ESLint, and Prettier.

### Planned / In-Progress
- [x] **Live API Integration (minimal transport)**: The main session/task/activity UI now consumes runtime-backed session data and ordered streamed events through the local HTTP/SSE transport.
- [ ] **WebSocket Streaming**: Real-time agent event streaming from the backend.
- [ ] **Session Persistence**: True persistence via the backend database.
- [ ] **File System Browser**: Integration with the local workspace for code reading.

### Planning status
- [x] **Foundation / Epic 0**: Developer tooling, repository structure, CI baseline, and contributor-facing docs are substantially in place.
- [x] **Executable contract layer for the web MVP slice**: The current web shell now exercises the client contracts for session list, replay, and streamed runs against the local transport.

---

## Repository Metadata & Links
- **Canonical Repository**: [https://github.com/lei-jia-xing/voidcode](https://github.com/lei-jia-xing/voidcode)
- **Default Branch**: `master`
- **Issue Tracker**: Enabled on GitHub.
- **Project Scope**: Local-first coding agent runtime inspired by OpenCode and Claude Code.
