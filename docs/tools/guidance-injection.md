# Tool Guidance Injection

Agent-facing tool guidance has two surfaces:

- sidecar `.txt` files next to tool implementations under `src/voidcode/tools/`
- human-readable grouping pages under `docs/tools/`

The sidecar files are the source of truth for text that can be injected into agent-visible tool descriptions. The Markdown pages are reading and navigation entry points for humans.

## Source of truth

Use `ToolDefinition.description` for the short capability summary.

Use sidecar guidance for:

- when to choose the tool,
- neighboring-tool boundaries,
- common misuse,
- approval and read-only expectations,
- result fields that should guide the next step.

Use `docs/tools/*.md` for:

- longer explanation,
- examples,
- grouped reading order,
- maintenance context.

## Mapping strategy

Static built-in tools map to sidecar files by tool name.

Tool families can share a sidecar when separate files would duplicate policy:

- `read_file`, `list`, `glob`, `grep` share `read_search.txt`
- `ast_grep_search`, `ast_grep_preview`, `ast_grep_replace` share `ast_grep.txt`
- `lsp` and `format_file` share `lsp.txt`

Dynamic MCP tools share `mcp.txt` through the `mcp/*` name prefix.

## Runtime behavior

The runtime keeps tool implementations and execution unchanged. It decorates agent-visible `ToolDefinition` values when definitions are requested.

This means:

- `ToolRegistry.resolve()` still returns the original tool object,
- `ToolRegistry.definitions()` returns descriptions with sidecar guidance appended when available,
- missing sidecar files are ignored rather than breaking runtime startup,
- dynamic MCP tools receive the shared MCP policy without duplicating long descriptions per server tool.

## Maintenance rule

When adding or changing a built-in tool, update:

- the tool implementation and tests,
- the sidecar guidance near the implementation,
- the relevant `docs/tools` page or README entry,
- contract docs only when the runtime/tool-calling contract changes.
