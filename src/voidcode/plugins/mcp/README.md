# mcp

Call tools exposed by configured MCP servers through one stable tool.

## Surface

- Tool `mcp` with `server`, `tool`, and optional `arguments` object (validated against the discovered input schema). Effects `WRITE` + `NETWORK`, never replayed: MCP calls are treated as mutating and network-capable, so they may require approval.
- Command `/mcp`: selects a configured server without connecting. `/mcp <server>` opens cached status, startup errors, and paged tool details, including enabled/disabled state and reasons. Structured arguments accept `server` and a non-negative tool `offset`; pages contain at most ten tools and preview long metadata. Unknown discovery stays distinct from a known empty tool list. Configuration changes require `/core reload`.
- Context contribution naming the configured servers and their known tools (bounded: 32 servers, 50 tools each, 4000 bytes).

## Configuration

Plugin-only keys (strict; `providers` accepted and ignored):

- `enabled` (bool, default `true`).
- `request_timeout_seconds` (optional positive float).
- `servers`: map of non-empty server name to `{transport, command, env, url}`. `stdio` (default) requires a non-empty argv `command` and rejects `url`; `remote-http` requires an absolute HTTP(S) `url` and rejects `command`/`env`.

See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- The plugin owns the MCP manager: connections open lazily, the tool surface is cached passively, and `close` shuts the manager down.
- Full result JSON and embedded media spill to the artifact store; the page carries bounded rendered text. Server names, tool names, content, and structured content are untrusted server-provided results; remote safety annotations stay display-only.
