# Tool Guidance Injection

Agent-facing tool guidance has two surfaces:

- sidecar `.txt` files next to tool implementations under `src/voidcode/tools/`
- human-readable grouping pages under `docs/tools/`

The sidecar files are the source of truth for agent-visible tool descriptions. The Markdown pages are reading and navigation entry points for humans.

## Source of truth

Use sidecar guidance for:

- when to choose the tool,
- exact argument names and syntax expectations when the agent needs them,
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

Prefer one sidecar per tool when behavior or syntax differs. Shared sidecars are acceptable only when the tools are intentionally documented as one family.

Current shared families:

- `ast_grep_search`, `ast_grep_preview`, `ast_grep_replace` share `ast_grep.txt`

Dynamic MCP tools share `mcp.txt` through the `mcp/*` name prefix.

## Runtime behavior

The runtime keeps tool implementations and execution unchanged. It decorates agent-visible `ToolDefinition` values when definitions are requested.

This means:

- `ToolRegistry.resolve()` still returns the original tool object,
- `ToolRegistry.definitions()` returns sidecar-owned descriptions for mapped built-in tools,
- missing sidecar files are ignored rather than breaking runtime startup,
- dynamic MCP tools receive the shared MCP policy appended to the tool-specific description.

## Maintenance rule

When adding or changing a built-in tool, update:

- the tool implementation and tests,
- the sidecar guidance near the implementation,
- the relevant `docs/tools` page or README entry,
- contract docs only when the runtime/tool-calling contract changes.
