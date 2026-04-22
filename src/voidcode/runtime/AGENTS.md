# RUNTIME KNOWLEDGE BASE

**Generated:** 2026-04-22
**Commit:** 269eaf8
**Branch:** master

## OVERVIEW
Runtime control plane for execution, persistence, approvals, hooks, capability managers, and session truth. This directory is the highest-risk backend surface because most product behavior converges in `VoidCodeRuntime`.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Public runtime exports | `__init__.py` | lazy-loads `VoidCodeRuntime` and `ToolRegistry` via `__getattr__` |
| Main control plane | `service.py` | runtime graph loop, tool execution, approvals, resume, background tasks |
| Runtime config loading | `config.py` | merges env, user, repo-local, and request overrides |
| Session persistence | `storage.py` | SQLite schema, notifications, pending approval, background task state |
| Permission defaults | `permission.py` | read-only tools auto-allow; write tools create pending approvals |
| Runtime boundary contracts | `contracts.py` | request/response/session validation, metadata rules |
| Event envelope surface | `events.py` | runtime event names emitted to clients |
| HTTP transport integration | `http.py` | runtime-backed transport app |
| LSP/MCP capability managers | `lsp.py`, `mcp.py` | runtime-managed lifecycle, not pure capability schema |
| Skill runtime bridge | `skills.py` | converts pure skill metadata into runtime contexts |
| Session state types | `session.py`, `task.py` | session refs/status plus background task types |

## STRUCTURE
```text
runtime/
├── service.py        # VoidCodeRuntime + ToolRegistry
├── config.py         # effective runtime config resolution
├── storage.py        # SQLite-backed session/task store
├── permission.py     # approval policy and PendingApproval
├── http.py           # runtime transport app
├── lsp.py / mcp.py   # managed capability lifecycle
└── skills.py         # runtime-facing skill context bridge
```

## CONVENTIONS
- Preserve the control-plane split: runtime owns governance, graph owns step progression, tools own tool logic.
- Keep `runtime/__init__.py` lazy-import behavior for `VoidCodeRuntime`, `ToolRegistry`, and HTTP exports to avoid import cycles.
- Treat `load_runtime_config()` precedence as load-bearing: environment, user config, repo-local config, request metadata, and persisted session metadata each have distinct roles.
- `ToolDefinition.read_only` drives default permission policy through `permission.py`; changing tool mutability changes approval behavior.
- Background task IDs and session IDs are validated as runtime boundary inputs; do not bypass validators in `contracts.py` / `task.py`.

## HOTSPOTS
- `service.py` is the central monolith. Read the surrounding methods before changing `_build_graph_for_engine_from_config`, `_tool_registry_for_effective_config`, `_execute_graph_loop`, `start_background_task`, or resume helpers.
- `config.py` is dense because it resolves many nested config sections. Prefer extending existing parse/serialize helpers over inventing a parallel path.
- `storage.py` owns schema evolution and terminal-state bookkeeping. Migration changes must preserve old sessions, pending approvals, and background tasks.

## ANTI-PATTERNS
- Do not move product governance into `graph/`; runtime chooses and configures graphs.
- Do not let clients or tools bypass runtime state for approvals, persistence, notifications, or capability lifecycle.
- Do not add eager imports to `runtime/__init__.py` for service/http symbols.
- Do not change `_EXECUTABLE_AGENT_PRESETS`, tool allowlist scoping, or provider fallback metadata casually; they affect active execution semantics.
- Do not duplicate pure capability logic from `skills/`, `lsp/`, `mcp/`, or provider modules when runtime only needs an integration layer.

## KEY FLOWS
- **Run path:** `VoidCodeRuntime.run_stream()` → `_stream_chunks()` → `_execute_graph_loop()`.
- **Graph selection:** `_runtime_config_for_request()` / `_effective_runtime_config_from_metadata()` → `_build_graph_for_engine_from_config()`.
- **Tool scoping:** `_tool_registry_for_effective_config()` applies builtin registry, agent manifest allowlist, and per-request tool config.
- **Approval path:** `_resolve_permission()` emits pending approval state; `resume()` / `resume_stream()` re-enter via `_resume_pending_approval_*` helpers.
- **Background tasks:** `start_background_task()` persists queued state, spawns worker thread, then `_run_background_task_worker()` finalizes lifecycle hooks and notifications.
- **Provider fallback:** `_execute_graph_loop()` increments `provider_attempt`, swaps active target, and rebuilds the graph when retryable provider failures occur.

## NOTES
- Only the `leader` preset is executable today; runtime still parses other presets for shape/config validation.
- LSP and MCP tooling are constructed by runtime and refreshed from managed capability state rather than treated as static builtins.
- Session metadata carries runtime config truth for replay/resume, including provider fallback and applied skills.
