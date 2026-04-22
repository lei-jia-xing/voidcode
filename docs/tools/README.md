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

## Relationship to injected guidance

Runtime-injected agent guidance lives next to tool implementations as sidecar `.txt` files in [`src/voidcode/tools/`](../../src/voidcode/tools/). Those sidecars are the source of truth for text that may be appended to agent-visible `ToolDefinition.description` values.

This directory remains the human-readable guide:

- it groups related tools,
- it gives examples and longer explanations,
- it links contract docs and implementation boundaries,
- it should stay consistent with the sidecar guidance without becoming a second injected source of truth.

See [`guidance-injection.md`](./guidance-injection.md) for the current injection strategy.

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
- [`ast-grep.md`](./ast-grep.md)
  - `ast_grep_search`
  - `ast_grep_preview`
  - `ast_grep_replace`
- [`lsp-format.md`](./lsp-format.md)
  - `lsp`

#### Editing / writing

- [`edit.md`](./edit.md)
- [`multi-edit.md`](./multi-edit.md)
- [`apply-patch.md`](./apply-patch.md)
- [`write-file.md`](./write-file.md)
- [`lsp-format.md`](./lsp-format.md)
  - `format_file`

#### Execution / external commands

- [`shell-exec.md`](./shell-exec.md)

#### External research

- [`external-research.md`](./external-research.md)
  - `web_search`
  - `web_fetch`
  - `code_search`

#### Agent work state

- [`todo-write.md`](./todo-write.md)
  - `todo_write`

#### Dynamic tools

- [`mcp-dynamic.md`](./mcp-dynamic.md)
  - `mcp/<server>/<tool>`

## Coverage waves

The first wave documented the tools that are easiest to misuse and most likely to affect correctness:

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

The second wave adds guidance for the remaining built-in and runtime-managed tool surface:

- `write_file`
- `ast_grep_*`
- `lsp` / `format_file`
- `web_fetch` / `web_search` / `code_search`
- `todo_write`
- `mcp/*` as one dynamic-tool policy page rather than one page per MCP tool

## Hard usage rules for agents

1. **Read before you write.** Unless the user explicitly asked to create a new file, prefer read-only tools first.
2. **Choose the narrowest tool that fits.** If you already know the file path, use `read_file` instead of `shell_exec cat ...`.
3. **Prefer structured, recoverable results.** If you need diffs, replacement counts, or file-oriented edits, prefer `edit`, `multi_edit`, or `apply_patch` over `shell_exec`.
4. **Treat post-formatter file state as truth.** For formatter-aware editing tools, future actions must rely on the final file state after the tool completes.
5. **Do not use `shell_exec` as a search tool.** Reading and search should normally go through dedicated tools.
