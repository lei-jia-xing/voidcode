# Current Implementation State

This document provides a truthful snapshot of the VoidCode repository as of April 2026. VoidCode is currently in the **pre-MVP foundation stage with one real deterministic backend slice**.

For the concrete delivery checklist that connects the current repo state to the intended MVP, see [`docs/mvp-todo-plan.md`](./mvp-todo-plan.md). For normative client-facing contracts, see [`docs/contracts/README.md`](./contracts/README.md).

## Overview
The repository contains two primary, independent components:
1.  **Python Backend Slice**: A typed contract layer plus one deterministic local read-only execution path.
2.  **Bun Frontend Shell**: A React-based web interface for the future agent runtime.

**Current integration status**: 🔴 **None**. The frontend and backend are not yet connected.

---

## Backend (Python)

### Implemented Today
- [x] **Project Structure**: Hatch/UV-ready layout with `src/voidcode/runtime`, `src/voidcode/graph`, and `src/voidcode/tools`.
- [x] **CLI Entrypoints**: `voidcode --help` and `voidcode run "read <path>" --workspace <dir>` both work.
- [x] **Dependency Management**: Fully configured `pyproject.toml` and `mise.toml` for local development.
- [x] **Development Tooling**: Ruff (lint/format), basedpyright (types), and pytest (tests) are integrated and functional.
- [x] **Contract Layer**: Typed session, event, runtime, graph, and tool contracts exist in code.
- [x] **Deterministic Read-Only Slice**: The CLI can execute a governed local read-only file request through runtime, graph, and tool boundaries and emit observable events.

### Planned / In-Progress
- [ ] **LangGraph Orchestration**: Full graph compilation, richer node routing, and interrupt/resume behavior.
- [ ] **Runtime Services**: Session lifecycle, richer permission checkpoints, hooks, and persistence.
- [ ] **Tool Registry**: Dynamic tool discovery and registration beyond the current in-memory read-only default. Today the only real built-in tool path is the read-only file read flow.
- [ ] **API Layer**: FastAPI/Starlette-based server to expose the runtime to clients.

---

## Frontend (React + Bun)

### Implemented Today
- [x] **UI Framework**: React 18, Tailwind CSS, and Lucide React shell.
- [x] **Component Library**: Layout, navigation, and message-thread UI components.
- [x] **Mock State**: Zustand stores populated with mock session and agent event data.
- [x] **Frontend Tooling**: Vite-based dev server with Bun support, ESLint, and Prettier.

### Planned / In-Progress
- [ ] **Live API Integration**: Connection to the Python backend services.
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
