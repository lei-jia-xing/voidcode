<p align="center">
  <a href="./LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License" />
  </a>
  <img src="https://img.shields.io/badge/python-3.13-blue.svg" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/bun-1.3+-fbf0df.svg" alt="Bun 1.3+" />
  <img src="https://img.shields.io/badge/status-pre--MVP-orange.svg" alt="Project status" />
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs welcome" />
</p>

# VoidCode

VoidCode is a local-first coding agent runtime inspired by OpenCode and Claude Code.

> **Status:** VoidCode is still pre-MVP. The current focus is tightening runtime boundaries, improving the execution control plane, and delivering a repeatable end-to-end single-agent loop.

> **Documentation note:** contributor-facing documents at the repository root are in English. Internal design and planning documents under `docs/` are currently mostly written in Chinese.

## What VoidCode aims to provide

VoidCode is built around a local developer workflow with these core capabilities:

- conversational task execution
- code reading and search
- controlled tool calls and file editing
- approval checkpoints for risky operations
- hooks and observable runtime events
- local session persistence and resume
- a headless runtime separated from CLI and future UI clients

The roadmap remains intentionally narrow: ship a stable, demoable single-agent MVP first, then expand from a runtime-owned control plane rather than growing outward too early.

## Quick start

The recommended setup uses `uv` for Python and Bun for the frontend. Supported Python version: **3.13**.

> **Current state:** the repository already has a real deterministic CLI → runtime → single-agent loop with multi-step execution, session persistence and resume, and inline approval in TTY mode. It also exposes a minimal local HTTP/SSE transport. The TUI and web frontend both exist, but neither is yet at full CLI parity.

```bash
# Install toolchain and Python dependencies
mise install
uv sync --extra dev

# Install frontend dependencies
mise run frontend:install

# Explore the CLI
uv run voidcode --help

# Run a deterministic read task
uv run voidcode run "read README.md" --workspace .

# Run a write task that requires approval
uv run voidcode run "write hello.txt hello world" --workspace . --approval-mode ask

# List persisted sessions
uv run voidcode sessions list --workspace .
```

## Architecture overview

VoidCode uses a runtime-centric architecture: **runtime** is the system control plane, **graph** is the execution/orchestration layer, and LangGraph currently powers only one deterministic slice.

- The runtime owns session state, permissions, tools, storage, streaming, and governance.
- The graph advances execution state. Today that includes a LangGraph-backed deterministic slice and a runtime-driven provider-backed single-agent path.
- Clients such as the CLI, web frontend, and future integrations talk to the runtime rather than invoking tools or graph code directly.
- The repository already contains `src/voidcode/agent/` as a declaration layer for agent presets, but true multi-agent execution semantics are still post-MVP.

Key backend boundaries:

- `src/voidcode/runtime/` — runtime services and execution boundary
- `src/voidcode/graph/` — execution/orchestration layer
- `src/voidcode/tools/` — built-in tools and tool metadata
- `src/voidcode/hook/` — hook configuration and executor
- `src/voidcode/lsp/`, `skills/`, `provider/`, `acp/`, `mcp/` — capability-layer boundaries
- `src/voidcode/tui/` — terminal client layer

Design principles that currently shape the project:

- keep runtime, orchestration, and UI responsibilities clearly separated
- gate tool usage through registry, permission, and hook policies
- make sessions and execution state resumable
- allow concurrent reads while controlling writes
- preserve observability around turns, tools, approvals, hooks, and failures
- keep the MVP scope narrow and verifiable

## Repository layout

```text
voidcode/
├── src/voidcode/         # Python package (src-layout)
├── tests/                # pytest unit and integration coverage
├── frontend/             # Bun/Vite/React shell
├── docs/                 # internal architecture, roadmap, and contract docs
├── .github/workflows/    # CI and release automation
└── mise.toml             # canonical task runner entrypoint
```

## Development workflow

One-time setup:

```bash
mise install
uv sync --extra dev
mise run frontend:install
```

Common tasks from `mise.toml`:

```bash
# Python
mise run lint
mise run format
mise run typecheck
mise run test
mise run build

# Frontend
mise run frontend:dev
mise run frontend:lint
mise run frontend:typecheck
mise run frontend:test
mise run frontend:e2e
mise run frontend:build

# Combined verification
mise run check
mise run ci

# Pre-commit hooks
mise run pre-commit
uv run pre-commit install
```

`mise` orchestrates tasks and loads the local virtual environment. `uv` remains the source of truth for Python dependency management and execution.
Bun scripts are owned by `frontend/package.json`; the repository root intentionally has no `package.json` so root-level automation goes through `mise.toml`.

## Documentation map

For a deeper view of the current design and roadmap, see:

- [`docs/architecture.md`](./docs/architecture.md)
- [`docs/roadmap.md`](./docs/roadmap.md)
- [`docs/mvp-todo-plan.md`](./docs/mvp-todo-plan.md)
- [`docs/mvp-demo-guide.md`](./docs/mvp-demo-guide.md)
- [`docs/contracts/README.md`](./docs/contracts/README.md)
- [`docs/development.md`](./docs/development.md)

These internal docs are currently maintained in Chinese.

## Contributing and community

- Contribution guide: [`CONTRIBUTING.md`](./CONTRIBUTING.md)
- Code of conduct: [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md)
- Security policy: [`SECURITY.md`](./SECURITY.md)
- Changelog: [`CHANGELOG.md`](./CHANGELOG.md)

## License

VoidCode is released under the [MIT License](./LICENSE).
