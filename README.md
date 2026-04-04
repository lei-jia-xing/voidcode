[![Python](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![CI](https://github.com/lei-jia-xing/voidcode/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/lei-jia-xing/voidcode/actions/workflows/ci.yml)

# VoidCode

VoidCode is a local-first coding agent runtime inspired by OpenCode and Claude Code.

> **Status:** VoidCode is pre-MVP software in early development. The repository currently focuses on establishing the runtime foundations, architecture boundaries, and developer workflow needed for the first end-to-end agent loop.

## What VoidCode is aiming to build

VoidCode is designed to provide a local development-agent experience centered on:

- conversational task execution
- code reading and search
- governed tool calls and file edits
- permission checkpoints for risky actions
- hooks and event streams
- session persistence and resume
- a headless runtime separated from CLI or future UI clients

The current direction is intentionally narrow: ship a stable single-agent MVP loop before expanding into a larger platform.

## Quickstart

Preferred local setup uses uv-managed Python environments and Bun. Python 3.14 is the supported version.

> **Note:** The current implementation includes a real deterministic CLI → runtime → read-only tool slice for local file reads, plus a Bun frontend shell. The frontend is still mock-backed; there is no live backend API integration yet.

```bash
# Setup tools and Python environment
mise install
uv sync --extra dev

# Setup frontend environment
mise run frontend:install

# Start the CLI
uv run voidcode --help

# Prove the deterministic read-only slice
uv run voidcode run "read README.md" --workspace .

# List persisted sessions
uv run voidcode sessions list --workspace .

# Resume a persisted session
uv run voidcode sessions resume local-cli-session --workspace .

# Start the web frontend (mock-backed)
mise run frontend:dev
```

## Architecture summary

VoidCode follows a layered architecture where **LangGraph handles agent orchestration** and a **custom runtime handles product-level concerns**.

- The runtime is the system boundary for sessions, permissions, hooks, storage, streaming, and tool governance.
- LangGraph is used as the orchestration engine for graph state, routing, checkpoints, and interrupt/resume flow.
- Clients such as the CLI, a future web frontend, or future IDE integrations talk to the runtime rather than calling tools directly.
- The codebase is organized around three core areas:
  - `src/voidcode/runtime/` for runtime services and execution boundaries
  - `src/voidcode/graph/` for LangGraph orchestration and state transitions
  - `src/voidcode/tools/` for built-in tools and tool metadata

Key design principles carried forward from the architecture plan:

- keep runtime, graph, and UI responsibilities clearly separated
- govern tools before execution through registry, permission, and hooks
- make sessions and execution state recoverable
- allow concurrent reads while keeping writes controlled
- prioritize observability for turns, tools, approvals, hooks, and errors
- keep the MVP tight around one stable single-agent task loop

For the English-facing architecture and roadmap summaries, see:

- [`docs/architecture.md`](./docs/architecture.md)
- [`docs/roadmap.md`](./docs/roadmap.md)
- [`docs/mvp-todo-plan.md`](./docs/mvp-todo-plan.md)
- [`docs/development.md`](./docs/development.md)

The original planning sources remain in Chinese:

- `voidcode-architecture-v1.md`
- `voidcode-backlog-v1.md`

## Development workflow

Install dependencies once:

```bash
mise install
uv sync --extra dev
```

Common tasks are defined in `mise.toml`:

```bash
# Python tasks
mise run lint
mise run format
mise run typecheck
mise run test

# Frontend tasks (Bun)
mise run frontend:install
mise run frontend:dev
mise run frontend:lint
mise run frontend:typecheck

# Combined check (Python + Frontend)
mise run check

# Pre-commit
mise run pre-commit
```

Set up pre-commit hooks locally:

```bash
uv run pre-commit install
```

The current pre-commit configuration runs repository hygiene checks plus Ruff and mypy. `mise` loads the existing `.venv` for task execution; uv remains the source of truth for Python environments and dependencies.

## Contributing and community

- Contribution guide: [`CONTRIBUTING.md`](./CONTRIBUTING.md)
- Code of conduct: [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md)
- Security policy: [`SECURITY.md`](./SECURITY.md)
- Changelog: [`CHANGELOG.md`](./CHANGELOG.md)

## License

VoidCode is released under the [MIT License](./LICENSE).
