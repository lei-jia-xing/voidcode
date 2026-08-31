# Deliberate Omissions

What voidcode intentionally does not build. For each feature decision, classify as core primitive, extension point, or deliberate omission.


---

## 🔵 Core Primitives

What voidcode MUST do. Keep this surface minimal.

| Area | Primitive | Notes |
|------|-----------|-------|
| Execution | Single-agent loop with provider-backed and deterministic engines | `runtime/service.py` |
| Persistence | Append-only event log + SQLite session store | `runtime/storage.py` |
| Tools | Builtin registry（非穷尽示例）：read, write, edit, glob, grep, web_fetch, web_search, apply_patch, multi_edit, todo_write, lsp | `tools/`；runtime-managed LSP/MCP 以及 task/process 等能力会按配置和运行时路径注入 |
| Background processes | Spawn, poll logs, send stdin, and stop long-running workspace processes (dev servers, watchers) | `tools/background_process_*.py`; `shell_exec` remains the one-shot escape hatch |
| Approval | Permission policy driven by `ToolDefinition.read_only` | `runtime/permission.py` |
| Delegation | Runtime-owned background tasks with fixed child presets (advisor, explore, researcher, worker, product), single/batch dispatch (`task`/`task_batch`), bounded output, status/list/group, explicit retry/cancel, and keep-alive steer | `docs/contracts/background-task-delegation.md` |
| Resume | Session replay, approval continuation, context compaction | `runtime/service.py` |
| Config | Runtime config fields resolve as explicit/request → repo-local → environment → built-in defaults; user config is a separate merge surface（主要是 provider configs 与 TUI/Web settings），而 persisted session metadata 是单独的 resume/session override，不与上述层级混为一谈 | `runtime/config.py` |

## 🟡 Extension Points

What voidcode can do but defers to skills, hooks, or external tooling.

| Area | Extension | Mechanism |
|------|-----------|-----------|
| Plan mode | Structured planning before execution | Runtime-enforced `RuntimeMode.plan` read-only execution stance; no dedicated planning engine or plan-state machine. Plans remain files/text produced by the agent |
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
| **Per-file permission dialogs** | Trust model or containerization. Interactive per-file approval at the tool-call level does not scale; the current read-only/write policy split is sufficient. |
| **`todo_list` as a model-facing tool** | `todo_write` exists for structured task tracking. A separate `todo_list` model-facing tool is redundant surface area. |

### Agent Architecture

| Omission | Rationale |
|----------|-----------|
| **Arbitrary sub-agent spawning** | Runtime supports only fixed child presets (advisor, explore, researcher, worker, product) through `task` / `task_batch`; `leader` is the sole top-level executable preset (`_EXECUTABLE_AGENT_PRESETS`), not a delegation target, and `product` is a delegated read-only plan subagent (`_EXECUTABLE_SUBAGENT_PRESETS`), not a top-level preset. Keep-alive worker re-entry, explicit retry/cancel/steer, and bounded output/list/group surfaces are implemented within this fixed boundary. Open-ended agent creation is not a runtime primitive. See [agent-architecture.md](./agent-architecture.md). |
| **Agent-to-agent bus** | No direct agent-to-agent communication channel. All coordination flows through runtime-owned parent/child session linkage and background task contracts. See [agent-boundary.md](./agent-boundary.md). |
| **Plan mode as a runtime concept** | `RuntimeMode.plan` exists as a runtime-enforced read-only execution stance. There is no dedicated planning engine or plan-state machine; planning artifacts remain files or plan text produced by the agent. |
| **Multi-agent topology beyond leader + child presets** | The runtime owns delegated child execution, not arbitrary orchestration graphs. The plain-Python graph layer is not a multi-agent backbone. |

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
