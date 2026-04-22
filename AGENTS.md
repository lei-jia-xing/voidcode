# PROJECT KNOWLEDGE BASE

**Generated:** 2026-04-22
**Commit:** 269eaf8
**Branch:** master

## OVERVIEW
Local-first coding agent runtime. Python backend (`src/voidcode`) plus Bun/React frontend shell (`frontend/`), both still pre-MVP.

## STRUCTURE
```text
voidcode/
├── src/voidcode/         # Python package in src-layout
│   ├── runtime/          # Session, storage, events, runtime boundary
│   ├── graph/            # Deterministic orchestration slice
│   └── tools/            # Built-in tool contracts + implementations
├── tests/                # pytest unit + integration coverage
├── frontend/             # Bun/Vite/React shell; see frontend/AGENTS.md
├── docs/                 # Architecture, roadmap, development, standards
├── .github/workflows/    # CI + release automation
└── mise.toml             # Repo task entrypoint
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| CLI behavior | `src/voidcode/cli.py` | `voidcode run`, `sessions list`, `sessions resume` |
| Python entrypoint | `src/voidcode/__main__.py` | `python -m voidcode` delegates to CLI |
| Runtime orchestration boundary | `src/voidcode/runtime/service.py` | CLI calls runtime, not graph directly |
| Runtime implementation work | `src/voidcode/runtime/AGENTS.md` | read before touching runtime control-plane code |
| Session persistence | `src/voidcode/runtime/storage.py` | SQLite-backed local session store |
| Runtime contracts | `src/voidcode/runtime/contracts.py` | request/response boundary types |
| Graph planning/finalization | `src/voidcode/graph/read_only_slice.py` | current deterministic slice |
| Tool behavior | `src/voidcode/tools/` | builtin tools include read/write/edit/glob/grep/list/web_fetch/web_search/apply_patch/code_search/multi_edit/todo_write/lsp |
| Unit tests | `tests/unit/` | contracts, metadata, import, CLI smoke |
| Integration tests | `tests/integration/test_read_only_slice.py` | full deterministic slice + session persistence |
| Dev workflow | `mise.toml` | canonical task runner |
| Repo standards | `docs/coding-standards.md` | coding + commit rules |
| Frontend work | `frontend/AGENTS.md` | read for any `frontend/` change |

## CODE MAP
| Symbol | Type | Location | Role |
|--------|------|----------|------|
| `main` | function | `src/voidcode/cli.py` | CLI process entry |
| `build_parser` | function | `src/voidcode/cli.py` | command surface definition |
| `VoidCodeRuntime` | class | `src/voidcode/runtime/service.py` | runtime boundary for requests/sessions |
| `ToolRegistry` | class | `src/voidcode/runtime/service.py` | current built-in tool registry |

## CONVENTIONS
- Python is pinned to 3.13 only.
- Use `uv` for Python env/deps; `mise` only orchestrates tasks and `.venv` loading.
- Repo-level verification is `mise run check`; it chains Python and frontend checks.
- Pre-commit runs hygiene + Ruff + basedpyright through `uv run`.
- Commit messages follow Conventional Commits as documented in `docs/coding-standards.md`.
- Tests import from `src/` layout directly; integration coverage lives in `tests/integration/`.

## ANTI-PATTERNS (THIS PROJECT)
- Do not have UI clients call tools directly.
- Do not have LangGraph talk directly to UI clients; flow goes CLI/client → runtime → graph/tools.
- Do not claim full frontend/runtime parity; the web client now has a minimal live runtime path, but it is not yet a fully productized runtime-driven app.
- Do not expand pre-MVP scope into multi-agent/cloud/IDE-plugin work unless the task explicitly targets roadmap changes.
- Do not commit generated frontend artifacts.
- Do not open public issues for security-sensitive reports.

## UNIQUE STYLES
- Backend architecture is intentionally split into `runtime/`, `graph/`, and `tools/` with contract files marking boundaries.
- Session recovery is local and SQLite-backed under `.voidcode/`.
- The backend now exposes a broader tool surface including read/write/edit/search/web and patch workflows under `src/voidcode/tools/`.
- Frontend source is intentionally small and flatter than the aspirational structure described in `frontend/README.md`.
- Runtime-specific invariants and hotspot entry points live in `src/voidcode/runtime/AGENTS.md`.

## COMMANDS
```bash
mise install
uv sync --extra dev
uv run voidcode --help
mise run lint
mise run typecheck
mise run test
mise run check
mise run pre-commit
```

## NOTES
- `src/voidcode/` uses src-layout; do not look for a top-level `voidcode/` package directory.
- CI has two jobs: Python and frontend. Release workflow only publishes Python packages.
- Read `src/voidcode/runtime/AGENTS.md` before changing runtime session/config/tool orchestration.
- Read `frontend/AGENTS.md` before touching anything under `frontend/`.
