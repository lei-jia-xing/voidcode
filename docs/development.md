# Development Guide

This guide summarizes the local workflow for contributing to VoidCode.

For repository coding expectations, see [`docs/coding-standards.md`](./coding-standards.md).

## Tooling baseline

VoidCode uses:

- `mise` for task management and sourcing the existing `.venv`
- `uv` for dependency and package management (Python)
- `bun` for frontend development and dependency management
- Python 3.14 as the supported uv-managed local version

## Initial setup

Install toolchain dependencies and project dependencies:

```bash
mise install
uv sync --extra dev
mise run frontend:install
```

Confirm the CLI entrypoint is available:

```bash
uv run voidcode --help
uv run voidcode run "read README.md" --workspace .
uv run voidcode sessions list --workspace .
uv run voidcode sessions resume local-cli-session --workspace .
```

## mise tasks

The repository defines these `mise` tasks:

### Python tasks

- `mise run lint` → `uv run ruff check .`
- `mise run format` → `uv run ruff format .`
- `mise run typecheck` → `uv run basedpyright --warnings src`
- `mise run test` → `uv run pytest`

### Frontend tasks

- `mise run frontend:install` → `bun install`
- `mise run frontend:dev` → `bun run dev`
- `mise run frontend:lint` → `bun run lint`
- `mise run frontend:typecheck` → `bun run typecheck`
- `mise run frontend:test` → `bun run test:run`
- `mise run frontend:coverage` → `bun run test:coverage`

### Global tasks

- `mise run check` → runs all Python and frontend checks
- `mise run pre-commit` → `uv run pre-commit run --all-files`

## MVP Demo and Verification

For the canonical end-to-end demo flow and the full verification ladder (unit, integration, and smoke), see [`docs/mvp-demo-guide.md`](./mvp-demo-guide.md). Use this guide to verify that the core "read-only" runtime loop and session persistence are functional on your machine.

`mise.toml` does not manage Python installation directly; it sources the repository's existing `.venv` and delegates Python dependency/environment management to `uv`.

## Frontend Development

The frontend is a Bun-powered React application located in `frontend/`.

### Current Implementation State
- **UI Shell**: Functional navigation and layout components.
- **Runtime-backed MVP path**: The frontend now consumes the local runtime transport for session listing, session replay, and streamed runs.
- **Backend Integration**: A minimal live HTTP/SSE connection to the Python runtime now exists for the current single-agent web shell; broader client work remains future scope.

### Frontend workflow

1.  **Install dependencies**: `mise run frontend:install`
2.  **Start dev server**: `mise run frontend:dev` (runs on [http://localhost:5173](http://localhost:5173))
3.  **Lint/Typecheck**: `mise run frontend:lint` and `mise run frontend:typecheck`
4.  **Run component tests**: `mise run frontend:test`
5.  **Run coverage**: `mise run frontend:coverage` or `mise run check` for all-up validation.

## Project layout

The current source tree reserves space for three main implementation areas:

- `src/voidcode/runtime/`
- `src/voidcode/graph/`
- `src/voidcode/tools/`
- `frontend/` (React + Bun + Vite)


Tests live under `tests/`, and the original planning documents remain at the repository root in Chinese.
