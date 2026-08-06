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
| Tools | Builtin registry: read, write, edit, glob, grep, web_fetch, web_search, apply_patch, code_search, multi_edit, todo_write, lsp | `tools/` |
| Approval | Permission policy driven by `ToolDefinition.read_only` | `runtime/permission.py` |
| Delegation | Runtime-owned background tasks with fixed child presets (advisor, explore, researcher, worker) | `docs/contracts/background-task-delegation.md` |
| Resume | Session replay, approval continuation, context compaction | `runtime/service.py` |
| Config | Merged precedence: env → user → repo-local → request → session metadata | `runtime/config.py` |

## 🟡 Extension Points

What voidcode can do but defers to skills, hooks, or external tooling.

| Area | Extension | Mechanism |
|------|-----------|-----------|
| Long-term memory | Cross-session knowledge, user preferences, project facts | Workspace files (`AGENTS.md`, `CONTEXT.md`), not runtime subsystem |
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
| **Memory tools** (save/recall/search across sessions) | Replaced by workspace files. Long-term memory is an extension point, not a runtime primitive. See [memory-strategy.md](./memory-strategy.md). |
| **Per-file permission dialogs** | Trust model or containerization. Interactive per-file approval at the tool-call level does not scale; the current read-only/write policy split is sufficient. |
| **Interactive shell / REPL tool** | `bash` is the escape hatch. A dedicated interactive shell tool duplicates what bash already provides and adds state-management complexity the runtime should not own. |
| **Background process tools** | Removed. `bash` handles backgrounding. The runtime should not track arbitrary OS processes. |
| **`todo_list` as a model-facing tool** | `todo_write` exists for structured task tracking. A separate `todo_list` model-facing tool is redundant surface area. |

### Agent Architecture

| Omission | Rationale |
|----------|-----------|
| **Arbitrary sub-agent spawning** | Only supported presets (leader, advisor, explore, researcher, worker) can be delegated to. Open-ended agent creation is not a runtime primitive. See [agent-architecture.md](./agent-architecture.md). |
| **Agent-to-agent bus** | No direct agent-to-agent communication channel. All coordination flows through runtime-owned parent/child session linkage and background task contracts. See [agent-boundary.md](./agent-boundary.md). |
| **Plan mode as a runtime concept** | Plans are files the agent writes. No dedicated planning execution engine or plan-state machine in the runtime. |
| **Multi-agent topology beyond leader + child presets** | The runtime owns delegated child execution, not arbitrary orchestration graphs. LangGraph is not the multi-agent backbone. |

### Context & Compaction

| Omission | Rationale |
|----------|-----------|
| **Model-assisted distillation** | Deterministic summaries (last-N tool result retention) are sufficient for now. Semantic compaction via model calls adds cost and latency without proven benefit. See [memory-strategy.md](./memory-strategy.md). |
| **Multiple overlapping compaction mechanisms** | Single unified compaction path. No parallel summarizers, no competing truncation strategies. |
| **tiktoken in the hot path** | `chars / 4` estimation is good enough for context window management. Exact token counting adds a dependency and CPU cost for marginal accuracy. |

### Storage

| Omission | Rationale |
|----------|-----------|
| **Compaction during persist** | Events are append-only truth. Storage writes raw events; compaction is a read-time projection concern, not a write-time mutation. |
| **Session storage = context projection** | Session store holds complete history. Context window is a separate projection with its own truncation rules. Conflating the two breaks replay and resume. |
| **sqlite-vec** | Removed. Vector search over session data is not a runtime primitive. If semantic retrieval is needed, it belongs in an extension layer, not the storage engine. |

### Configuration

| Omission | Rationale |
|----------|-----------|
| **Per-tool token budgets** | Single default context window policy. Per-tool budgets add configuration surface without clear benefit. `ContextWindowPolicy` reduced from 18 to 8 fields intentionally. |
| **Continuation distillation config knobs** | No user-facing toggles for how compaction summarizes. The runtime chooses the strategy; users configure the window size, not the distillation algorithm. |
| **Workspace-scoped MCP lifecycle** | MCP is runtime/session-scoped. Workspace-scoped MCP servers, marketplace, or dynamic agent discovery are not implemented. |

---

## Guiding Principle

> The bar to add a new tool, a new agent role, or a new config knob is high.
> Every addition to the core primitive surface is a liability.
> When in doubt, omit it or push it to an extension point.
