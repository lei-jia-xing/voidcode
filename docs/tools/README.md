# Agent-facing Tools Guide

This directory is written for **agents that generate `ToolCall` objects**. It complements the shared contract in `docs/contracts/agent-tool-calling.md`.

These pages do not redefine runtime or tool-layer types. Instead, they answer the questions an agent actually needs help with:

- When should I choose one tool over another?
- Which tools are read-only, and which ones may trigger approval?
- How should I think about neighboring tools with overlapping use cases?
- Which tool results are safe to treat as the source of truth for the next step?

## Relationship to other docs

- [`docs/contracts/agent-tool-calling.md`](../contracts/agent-tool-calling.md): the top-level contract for tool envelopes, approval expectations, and shared result shapes.
- [`src/voidcode/tools/contracts.py`](../../src/voidcode/tools/contracts.py): the code-level source of truth for `ToolDefinition`, `ToolCall`, and `ToolResult`.
- [`src/voidcode/tools/README.md`](../../src/voidcode/tools/README.md): the capability-layer boundary for tools, not an agent usage guide.

## Recommended reading order

### 1. Start with the global rules

- Tools must be invoked through the runtime. Agents do not instantiate tools directly.
- `read_only=true` tools normally do not enter approval.
- `read_only=false` tools may pause in `ask` mode and require approval.
- File paths are resolved relative to the workspace root. Path escape attempts fail.

### 2. Then choose by task type

#### Reading / searching

- [`read-search.md`](./read-search.md)
  - `read_file`
  - `list`
  - `glob`
  - `grep`

#### Editing / writing

- [`edit.md`](./edit.md)
- [`multi-edit.md`](./multi-edit.md)
- [`apply-patch.md`](./apply-patch.md)

#### Execution / external commands

- [`shell-exec.md`](./shell-exec.md)

## Why the first wave only documents these tools

The first priority is the tools that are easiest to misuse and most likely to affect correctness:

- `edit`
- `multi_edit`
- `apply_patch`
- `shell_exec`
- the core read/search tool family

These tools directly affect:

- whether the agent reads enough context before writing,
- whether it chooses an unnecessarily destructive write path,
- whether post-edit follow-up actions still rely on the real file state,
- whether command execution becomes an overused escape hatch.

After these pages settle, the next candidates are:

- `write_file`
- `ast_grep_*`
- `lsp` / `format_file`
- `web_fetch` / `web_search` / `code_search`
- `todo_write`
- `mcp/*` (better documented as one dynamic-tool policy page than one page per MCP tool)

## Hard usage rules for agents

1. **Read before you write.** Unless the user explicitly asked to create a new file, prefer read-only tools first.
2. **Choose the narrowest tool that fits.** If you already know the file path, use `read_file` instead of `shell_exec cat ...`.
3. **Prefer structured, recoverable results.** If you need diffs, replacement counts, or file-oriented edits, prefer `edit`, `multi_edit`, or `apply_patch` over `shell_exec`.
4. **Treat post-formatter file state as truth.** For formatter-aware editing tools, future actions must rely on the final file state after the tool completes.
5. **Do not use `shell_exec` as a search tool.** Reading and search should normally go through dedicated tools.
