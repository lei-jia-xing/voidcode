# Dynamic MCP Tools

MCP tools are exposed as dynamic runtime tools named:

```text
mcp/<server>/<tool>
```

They are discovered from configured MCP servers. The available set can differ by workspace, runtime config, and server health.

## How agents should understand them

Use an MCP tool only when:

- the runtime exposes it in the current tool registry,
- its `ToolDefinition.input_schema` matches the arguments you plan to send,
- built-in tools cannot express the needed capability,
- you are prepared for approval or failure.

Do not assume:

- the same MCP tool is always available,
- a dynamic tool is read-only,
- a tool is idempotent or safe from its name alone,
- all MCP responses have the same data shape.

## Risk profile

The current runtime wraps MCP tools as `read_only=false`, so they may trigger approval.

This conservative policy is intentional. MCP servers can represent external systems, local side effects, or account-scoped actions that the runtime cannot classify reliably from the schema alone.

## Return value

Important fields include:

- `data.server`
- `data.tool`
- `data.content`
- `content`, when the MCP response includes text blocks

Treat these as server-provided external results.

## Injection policy

MCP tools share one dynamic-tool policy instead of one guidance page per tool. Runtime-visible tool schemas still come from each MCP tool descriptor, but agent usage guidance is shared through the `mcp/*` policy.
