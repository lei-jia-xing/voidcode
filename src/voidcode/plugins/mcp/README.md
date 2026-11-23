# mcp

Call tools exposed by configured MCP servers through one stable tool.

## Surface

- Tool `mcp` with `server`, `tool`, and optional `arguments` object. Arguments are validated against the discovered input schema; a tool that was not discovered in the complete paginated `tools/list` surface is refused rather than sent unvalidated. Effects `WRITE` + `NETWORK`, never replayed: MCP calls are treated as mutating and network-capable, so they may require approval.
- Command `/mcp`: selects a configured server without connecting. `/mcp <server>` opens cached status, startup errors, and paged tool details, including enabled/disabled state and reasons. Structured arguments accept `server` and a non-negative tool `offset`; pages contain at most ten tools and preview long metadata. Unknown discovery stays distinct from a known empty tool list. When `config.enabled` is `false`, the command and context report that MCP support is disabled and do not advertise configured servers. Configuration changes require `/core reload`.
- Context contribution naming the configured servers and their known tools (bounded: 32 servers, 50 tools each, 4000 bytes). It is names-only: untrusted descriptions are not copied into model context.

## Configuration

Plugin-only keys (strict; `providers` accepted and ignored):

- `enabled` (bool, default `true`): `false` keeps the plugin admitted and its tool/command registered, but all runtime requests fail and `/mcp` reports “MCP support is disabled by configuration.” Admit the plugin with `plugins.mcp.enabled: false` to remove the surface entirely.
- `request_timeout_seconds` (optional positive float).
- `servers`: map of non-empty server name to `{transport, command, env, url}`. `stdio` (default) requires a non-empty argv `command` and rejects `url`; `remote-http` requires an absolute HTTP(S) `url` and rejects `command`/`env`.

`env` is additive: stdio servers inherit the core process environment and the configured entries override it. Remote HTTP has no header or authentication configuration; only the URL is supported. There is no resources/prompts surface.

See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- The plugin owns the MCP manager: connections open lazily, discovery reads every `tools/list` page up to fixed page/tool caps, and the complete surface is cached memory-only for the current plugin runtime. Server-side tool changes require a restart or `/core reload`; there is no notification refresh. `close` stops the manager.
- Discovery is tools-only. MCP resources, prompts, sampling, elicitation, and roots are not implemented. A server that exposes only those capabilities has no usable surface here.
- stdio servers are host-side and unconfined, as documented in [configuration](../../../../docs/configuration.md). Normal plugin close/reload closes the session and transport and stops the server process group. A core SIGKILL bypasses that cleanup: a server that ignores stdin EOF can survive until manually stopped; there is no parent-death mechanism or durable PID record.
- Server stderr is discarded. Startup failures are retained as an inspectable state error, but server tracebacks are not captured.
- A call observes `ToolContext.abort_signal`: cancellation cancels the in-flight SDK future and returns promptly instead of waiting for the request deadline. A failure, timeout, or cancellation after the request may have reached the server returns `UnconfirmedStop`; a clear pre-send validation/startup failure returns `ConfirmedStop`. The core still settles the run's own cancellation.
- Full result JSON and embedded media spill to the artifact store; the page carries bounded rendered text. Display names, descriptions, disabled reasons, and errors are sanitized to single-line bounded text. Raw remote tool names are preserved for calls. Server names, tool names, content, and structured content are untrusted server-provided results; remote safety annotations stay display-only.
