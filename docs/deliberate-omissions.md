# Deliberate Omissions

What voidcode intentionally does not build. For each feature decision, classify as core primitive, extension point, or deliberate omission.

See also: [runtime/AGENTS.md](../src/voidcode/runtime/AGENTS.md), [coding-standards.md](./coding-standards.md), [agent-architecture.md](./agent-architecture.md), [memory-strategy.md](./memory-strategy.md).

---

## 🔵 Core Primitives

What voidcode MUST do. Keep this surface minimal.

| Area | Primitive | Notes |
|------|-----------|-------|
| Execution | Single-agent loop with provider-backed and deterministic engines | `runtime/service.py` |
| Persistence | Append-only event log + SQLite session store | `runtime/storage.py` |
| Tools | Builtin registry: read, write, edit, glob, grep, web_fetch, web_search, apply_patch, multi_edit, todo_write, lsp | `tools/` |
| Background processes | Spawn, poll logs, send stdin, and stop long-running workspace processes (dev servers, watchers) | `tools/background_process_*.py`; `shell_exec` remains the one-shot escape hatch |
| Approval | Permission policy driven by `ToolDefinition.read_only` | `runtime/permission.py` |
| Delegation | Runtime-owned background tasks with fixed child presets (advisor, explore, researcher, worker) | `docs/contracts/background-task-delegation.md` |
| Resume | Session replay, approval continuation, context compaction | `runtime/service.py` |
| Config | Merged precedence: env → user → repo-local → request → session metadata | `runtime/config.py` |

## 🟡 Extension Points

What voidcode can do but defers to skills, hooks, or external tooling.

| Area | Extension | Mechanism |
|------|-----------|-----------|
| Long-term memory | Cross-session knowledge, user preferences, project facts | Workspace-scoped keyword memory (`runtime/memory.py`: SQLite `memories` + CLI `voidcode memory *` + config `memory` section) alongside workspace files (`AGENTS.md`, `CONTEXT.md`); broader long-term pipeline deferred |
| Plan mode | Structured planning before execution | Write plans to files; no dedicated runtime mode |
| MCP servers | External tool providers | Runtime/session-scoped, config-gated (`runtime/mcp.py`) |
| Custom agents | New agent roles beyond the preset set | Agent manifest declarations in `agent/`; runtime executes, not defines |
| LSP | Language intelligence | Runtime-managed lifecycle (`runtime/lsp.py`), not a builtin tool |
| Hooks | Pre/post tool, lifecycle phases | `hook/` layer; intervention and notification, not execution |
| Skills | Domain-specific instruction sets | Catalog-visible via `skill_refs`; injected per-request or per-delegation |

## 🔴 Deliberate Omissions

What voidcode will NEVER implement in the runtime core.

### Tools

| Omission | Rationale |
|----------|-----------|
| **Long-term memory pipeline** (hindsight / mnemopi 类跨 session 记忆管线) | Workspace memory (save/recall/search) is now a runtime capability: SQLite `memories` table + `voidcode memory add/list/search/show/delete/status` + config `memory` section + `runtime.memory_*` events. The broader long-term memory pipeline stays outside runtime primitives. See [memory-strategy.md](./memory-strategy.md). |
| **Per-file permission dialogs** | Trust model or containerization. Interactive per-file approval at the tool-call level does not scale; the current read-only/write policy split is sufficient. |
| **Interactive shell / REPL tool** | `bash` is the escape hatch. An `interactive_shell` (tmux control) implementation exists in `tools/interactive_shell.py` but is not registered in `BuiltinToolProvider` by default; a full REPL-class interactive tool remains omitted. |
| **`todo_list` as a model-facing tool** | `todo_write` exists for structured task tracking. A separate `todo_list` model-facing tool is redundant surface area. |

### Agent Architecture

| Omission | Rationale |
|----------|-----------|
| **Arbitrary sub-agent spawning** | Only supported child presets (advisor, explore, researcher, worker, product) can be delegated to; `leader` is the sole top-level executable preset (`_EXECUTABLE_AGENT_PRESETS`), not a delegation target, and `product` is a delegated read-only plan subagent (`_EXECUTABLE_SUBAGENT_PRESETS`), not a top-level preset. Open-ended agent creation is not a runtime primitive. See [agent-architecture.md](./agent-architecture.md). |
| **Agent-to-agent bus** | No direct agent-to-agent communication channel. All coordination flows through runtime-owned parent/child session linkage and background task contracts. See [agent-boundary.md](./agent-boundary.md). |
| **Plan mode as a runtime concept** | Plans are files the agent writes. No dedicated planning execution engine or plan-state machine in the runtime. |
| **Multi-agent topology beyond leader + child presets** | The runtime owns delegated child execution, not arbitrary orchestration graphs. LangGraph is not the multi-agent backbone. |

### Context & Compaction

| Omission | Rationale |
|----------|-----------|
| **Model-assisted distillation** | A `summary_strategy` knob (deterministic / model_assisted) exists with fallback machinery (`runtime/context_projection.py`), but no model projector is wired into the compaction path, so it always falls back to deterministic summaries. |
| **Multiple overlapping compaction mechanisms** | Single unified compaction path. No parallel summarizers, no competing truncation strategies. |
| **tiktoken in the hot path** | `chars / 4` estimation is good enough for context window management. Exact token counting adds a dependency and CPU cost for marginal accuracy. |

### Storage

| Omission | Rationale |
|----------|-----------|
| **Compaction during persist** | Events are append-only truth. Storage writes raw events; compaction is a read-time projection concern, not a write-time mutation. |
| **Session storage = context projection** | Session store holds complete history. Context window is a separate projection with its own truncation rules. Conflating the two breaks replay and resume. |
| **sqlite-vec** | Optional semantic-retrieval backend, not enabled by default: `detect_sqlite_vec_capability()` + config `sqlite_vec: auto/off/required` (`runtime/memory.py`). Vector search remains outside the core storage engine. |

### Configuration

| Omission | Rationale |
|----------|-----------|
| **Per-tool token budgets** | Single default context window policy is the default. Optional per-tool result caps exist (`ContextWindowPolicy.per_tool_result_tokens`, empty by default). `ContextWindowPolicy` reduced from 18 to 8 fields intentionally. |
| **Continuation distillation config knobs** | One user-facing knob exists: `summary_strategy` (deterministic / model_assisted) in the context window config. Deeper distillation knobs remain omitted. |
| **Workspace-scoped MCP lifecycle** | MCP is runtime/session-scoped. Workspace-scoped MCP servers, marketplace, or dynamic agent discovery are not implemented. |

---

## Guiding Principle

> The bar to add a new tool, a new agent role, or a new config knob is high.
> Every addition to the core primitive surface is a liability.
> When in doubt, omit it or push it to an extension point.
