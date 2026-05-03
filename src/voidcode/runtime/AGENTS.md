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
| Background task contract | `../../docs/contracts/background-task-delegation.md` | parent/child linkage, result output, retry/cancel semantics |

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
- Delegated child execution must enter through runtime-owned routing and background task/session contracts. CLI, HTTP, and ACP are adapters, not alternate subagent execution paths.
- Manifest `skill_refs` are catalog/default selection metadata. `force_load_skills` and delegated `load_skills` force full skill-body injection for that request or child session without leaking parent-only skill bodies.
- MCP servers are managed at runtime or session scope through `runtime/mcp.py`; do not document or implement workspace-scoped MCP lifecycle without a separate explicit task.

## HOTSPOTS
- `service.py` is the central monolith. Read the surrounding methods before changing `_build_graph_for_engine_from_config`, `_tool_registry_for_effective_config`, `_execute_graph_loop`, `start_background_task`, or resume helpers.
- `config.py` is dense because it resolves many nested config sections. Prefer extending existing parse/serialize helpers over inventing a parallel path.
- `storage.py` owns schema evolution and terminal-state bookkeeping. Runtime SQLite persistence is still pre-MVP and does not guarantee backward compatibility across schema changes; prefer explicit schema updates and fail-fast behavior unless a task explicitly requires migration support.

## ANTI-PATTERNS
- Do not move product governance into `graph/`; runtime chooses and configures graphs.
- Do not let clients or tools bypass runtime state for approvals, persistence, notifications, or capability lifecycle.
- Do not add eager imports to `runtime/__init__.py` for service/http symbols.
- Do not change `_EXECUTABLE_AGENT_PRESETS`, tool allowlist scoping, or provider fallback metadata casually; they affect active execution semantics.
- Do not duplicate pure capability logic from `skills/`, `lsp/`, `mcp/`, or provider modules when runtime only needs an integration layer.
- Do not treat permission denials as terminal session failures; denied tool calls should surface as tool-level feedback so the model can adapt.

## KEY FLOWS
- **Run path:** `VoidCodeRuntime.run_stream()` → `_stream_chunks()` → `_execute_graph_loop()`.
- **Graph selection:** `_runtime_config_for_request()` / `_effective_runtime_config_from_metadata()` → `_build_graph_for_engine_from_config()`.
- **Tool scoping:** `_tool_registry_for_effective_config()` applies builtin registry, agent manifest allowlist, and per-request tool config.
- **Delegated routing:** `task` tool routing validates supported child presets/categories before `start_background_task()` creates a child session lineage.
- **Approval path:** `_resolve_permission()` emits pending approval state; `resume()` / `resume_stream()` re-enter via `_resume_pending_approval_*` helpers.
- **Background tasks:** `start_background_task()` persists queued state, spawns worker thread, then `_run_background_task_worker()` finalizes lifecycle hooks and notifications. `background_output` reads bounded results/full-session slices; `background_cancel` returns deterministic status payloads for unknown, running, and terminal tasks.
- **Provider fallback:** `_execute_graph_loop()` increments `provider_attempt`, swaps active target, and rebuilds the graph when retryable provider failures occur.

## PERSISTENCE / RESUME / REPLAY INVARIANTS

Session truth is the core product asset. All persistence and resume paths must satisfy these invariants:

- **SQLite is the single source of truth.** In-memory state must never be the authoritative record for sessions, approvals, questions, or background tasks. If the process restarts, the database must contain enough information to reconstruct runtime behavior.
- **Resume checkpoints are validated on load.** `load_resume_checkpoint()` rejects malformed JSON, invalid kinds, and version mismatches. Do not bypass `_decode_resume_checkpoint_payload()` or `validated_resume_checkpoint_envelope()`.
- **Schema mismatches fail fast with actionable diagnostics.** `_assert_canonical_schema()` raises `RuntimeError` with a reset command. Do not silently ignore schema drift or attempt implicit migrations unless a task explicitly requires it.
- **Notification deduplication is storage-enforced.** `session_notifications` uses `UNIQUE(workspace, dedupe_key)` and `INSERT OR IGNORE`. Do not rely on in-memory dedupe sets for restart survival.
- **Background task state transitions are guarded.** `is_background_task_transition_allowed()` enforces the terminal/running/queued state machine. `mark_background_task_terminal()` and `request_background_task_cancel()` check transitions before writing.
- **Cross-process resume must restore pending state.** A session waiting on approval or question must have `pending_approval_json` or `pending_question_json` persisted. `save_pending_approval()` and `save_pending_question()` write both the session snapshot and the resume checkpoint atomically.
- **Event sequences are monotonic per session.** `append_session_event()` uses `RETURNING last_event_sequence` with `+1` allocation. Deduped events roll back the sequence counter to preserve monotonicity.

### Error handling expectations

| Scenario | Behavior | Location |
|----------|----------|----------|
| Unknown session ID | `UnknownSessionError` with session ID | `storage.py` load methods |
| Corrupt resume checkpoint JSON | `ValueError` with "persisted resume checkpoint JSON is malformed" | `storage.py:_decode_resume_checkpoint_payload` |
| Invalid checkpoint kind | `ValueError` with kind value | `storage.py:_decode_resume_checkpoint_payload` |
| Schema mismatch | `RuntimeError` with missing columns/tables and reset command | `storage.py:_raise_schema_mismatch` |
| Invalid background task transition | Returns current state without writing | `storage.py:mark_background_task_terminal` |
| Unknown notification ID | `ValueError` with notification ID | `storage.py:acknowledge_notification` |
| Missing sequence scope | `RuntimeError` with scope name | `storage.py:_next_sequence_value` |

### Testing guidance

- Unit tests in `tests/unit/runtime/test_session_storage.py` cover schema bootstrap, revert markers, deduplication, pruning, and pending state persistence.
- Integration tests in `tests/integration/test_read_only_slice.py` cover live→persisted→replay parity for provider contexts and tool results.
- When adding new persistence paths, include at least one test that simulates a process boundary: write state, create a fresh store instance pointing to the same database, and verify the read surface matches.

## NOTES
- Top-level execution is limited to `leader` and explicit `product`; supported delegated child presets are `advisor`, `explore`, `product`, `researcher`, and `worker`.
- LSP and MCP tooling are constructed by runtime and refreshed from managed capability state rather than treated as static builtins.
- Session metadata carries runtime config truth for replay/resume, including provider fallback and applied skills.
