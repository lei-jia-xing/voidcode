# Current Implementation State

This document provides a truthful snapshot of the VoidCode repository as of April 2026. VoidCode is currently in the **pre-MVP foundation stage with a stable single-agent loop**.

For the concrete delivery checklist that connects the current repo state to the intended MVP, see [`docs/mvp-todo-plan.md`](./mvp-todo-plan.md). For normative client-facing contracts, see [`docs/contracts/README.md`](./contracts/README.md).

## Overview
The repository contains two primary, independent components:
1.  **Python Backend**: A typed contract layer plus a stable single-agent loop implementation.
2.  **Bun Frontend Shell**: A React-based web interface for the future agent runtime.

**Current integration status**: 🟡 **Minimal transport only**. The backend now exposes a local HTTP/SSE transport and the frontend includes a thin runtime client/debug path, but the main UI remains mock-driven.

---

## Backend (Python)

### Implemented Today
- [x] **Project Structure**: Hatch/UV-ready layout with `src/voidcode/runtime`, `src/voidcode/graph`, and `src/voidcode/tools`.
- [x] **CLI Entrypoints**: `voidcode --help` and `voidcode run "read <path>" --workspace <dir>` both work.
- [x] **Dependency Management**: Fully configured `pyproject.toml` and `mise.toml` for local development.
- [x] **Development Tooling**: Ruff (lint/format), basedpyright (types), and pytest (tests) are integrated and functional.
- [x] **Contract Layer**: Typed session, event, runtime, graph, and tool contracts exist in code.
- [x] **Stable Single-Agent Loop**: The CLI can execute governed local file and shell requests through runtime, graph, and tool boundaries and emit observable events.
- [x] **Extension Infrastructure Foundations**: The runtime now includes typed configuration and discovery infrastructure for tools, skills, LSP, and ACP.
- [x] **Built-in Tool Provider**: A dedicated `BuiltinToolProvider` handles registration for `grep`, `read_file`, `shell_exec`, and `write_file` through the runtime boundary.
- [x] **Skill Discovery Infrastructure**: Minimal discovery exists for `.voidcode/skills/<name>/SKILL.md` files; the runtime emits `runtime.skills_loaded` events for every run.
- [x] **LSP and ACP Configuration Seams**: Typed configuration carriers and disabled manager/adapter stubs exist for future language-server and transport integration.
- [x] **Minimal HTTP Transport**: A thin backend HTTP layer now exposes `GET /api/sessions`, `GET /api/sessions/{session_id}`, and `POST /api/runtime/run/stream` with SSE chunks serialized directly from the runtime boundary, and it can now be served locally through `voidcode serve`.

### Planned / In-Progress
- [x] **LangGraph Orchestration**: Stable single-agent loop implementation with support for model turns, tool execution, and interrupt/resume.
- [x] **Runtime Services**: Session lifecycle management, sqlite-backed persistence, and approval-resume continuity.
- [x] **Permission Engine**: Governed execution supporting `allow`, `deny`, and `ask` modes with TTY-only inline approval in the CLI.
- [x] **Contract-First Events**: Canonical event schema implemented for turns, tools, and approvals, with automated renumbering for consistency across resumes.
- [x] **HTTP Transport Parity**: The backend HTTP layer now fully exposes session list/resume and run/stream operations on parity with the CLI, including approval-resolution endpoints.
- [x] **Dynamic Tool Registration**: The runtime now includes typed configuration and discovery infrastructure for tools, supporting the `BuiltinToolProvider`.
- [ ] **Skill Execution**: Discovery is implemented (emitting `runtime.skills_loaded`), but the runtime does not yet execute skill logic or provide skill-specific tool contexts.
- [ ] **Real LSP and ACP Integrations**: Configuration seams exist; real process management and transport support are pending.
- [ ] **TUI Client**: A terminal-native user interface is planned to replace the current basic CLI `run` experience.
- [ ] **Web Client Integration**: The backend transport is ready; frontend integration is pending.

---

## Frontend (React + Bun)

### Implemented Today
- [x] **UI Framework**: React 18, Tailwind CSS, and Lucide React shell.
- [x] **Component Library**: Layout, navigation, and message-thread UI components.
- [x] **Mock State**: Zustand stores populated with mock session and agent event data.
- [x] **Frontend Tooling**: Vite-based dev server with Bun support, ESLint, and Prettier.

### Planned / In-Progress
- [ ] **Live API Integration**: A thin frontend runtime client/debug path now exists for the minimal transport, but the main session/task/activity UI still does not consume runtime-backed state.
- [ ] **WebSocket Streaming**: Real-time agent event streaming from the backend.
- [ ] **Session Persistence**: True persistence via the backend database.
- [ ] **File System Browser**: Integration with the local workspace for code reading.

### Planning status
- [x] **Foundation / Epic 0**: Developer tooling, repository structure, CI baseline, and contributor-facing docs are substantially in place.
- [ ] **Executable contract layer for clients**: The contract docs now exist under `docs/contracts/`, but implementation work against them is still pending.

---

## Repository Metadata & Links
- **Canonical Repository**: [https://github.com/lei-jia-xing/voidcode](https://github.com/lei-jia-xing/voidcode)
- **Default Branch**: `master`
- **Issue Tracker**: Enabled on GitHub.
- **Project Scope**: Local-first coding agent runtime inspired by OpenCode and Claude Code.
