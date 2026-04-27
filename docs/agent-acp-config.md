# Agent and ACP Configuration Reference

This document describes the user-facing configuration for agent presets and ACP (Agent Communication Protocol) in `.voidcode.json`.

## Agent Configuration

### Top-Level `agent` Object

The `agent` key in `.voidcode.json` configures the agent preset for the current session.

```json
{
  "agent": {
    "preset": "leader"
  }
}
```

### Supported Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `preset` | string | Yes | Agent preset identifier. `leader` and `product` are executable top-level presets. |
| `prompt_profile` | string | No | Prompt profile name. Must be a built-in profile. |
| `model` | string | No | Model override for this agent. |
| `execution_engine` | string | No | Execution engine. Default is `provider`. |
| `tools` | object | No | Tool configuration override. |
| `skills` | array | No | List of skill names to enable. |
| `provider_fallback` | object | No | Provider fallback configuration. |

### Preset Types

| Preset | Mode | Description |
|-------|------|-------------|
| `leader` | **primary** | Default execution/coding agent. Executable as top-level active execution. |
| `worker` | subagent | Focused executor for narrow tasks. Available via runtime delegation only. |
| `advisor` | subagent | Read-only advisory for architecture and review. Available via runtime delegation only. |
| `explore` | subagent | Workspace-bound exploration for code structure. Available via runtime delegation only. |
| `researcher` | subagent | External research for docs and examples. Available via runtime delegation only. |
| `product` | **primary** | Planning agent for requirements alignment, scope shaping, acceptance criteria, and issue drafting. Executable as top-level active execution. |

### Examples

#### Leader with Custom Model

```json
{
  "agent": {
    "preset": "leader",
    "model": "claude-sonnet-4-20250514",
    "prompt_profile": "leader"
  }
}
```

#### Leader with Skills

```json
{
  "agent": {
    "preset": "leader",
    "skills": ["default", "code-review"]
  }
}
```

#### Leader with Provider Fallback

```json
{
  "agent": {
    "preset": "leader",
    "model": "claude-sonnet-4-20250514",
    "provider_fallback": {
      "providers": ["anthropic", "openai"],
      "retry_on_failure": true
    }
  }
}
```

---

## ACP Configuration

### Top-Level `acp` Object

The `acp` key in `.voidcode.json` configures the Agent Communication Protocol.

```json
{
  "acp": {
    "enabled": false
  }
}
```

### External Stdio Facade

VoidCode also exposes a minimal external-facing ACP-compatible stdio facade:

```bash
voidcode acp --workspace . --approval-mode ask
```

This command runs a newline-delimited JSON-RPC 2.0 server over stdin/stdout. Each input line must be one JSON-RPC request object, and each stdout line is one JSON-RPC response or notification. Human logs, debug output, and process errors are reserved for stderr so clients can safely parse stdout as protocol data only.

Supported JSON-RPC methods:

| Method | Description |
|--------|-------------|
| `initialize` | Returns ACP protocol version `1`, minimal `agentCapabilities`, `agentInfo`, and `authMethods: []`. |
| `session/new` | Allocates an external ACP session id and records it for later prompts. Non-empty `mcpServers` are rejected because MCP-over-ACP is not implemented. |
| `session/prompt` | Accepts ACP text prompt blocks, runs `VoidCodeRuntime.run_stream(RuntimeRequest(...))` in a worker thread, maps the ACP session id to the runtime session id emitted by the runtime, emits `session/update` notifications, and returns `stopReason: "end_turn"` on completion. |
| `session/cancel` | Supports ACP notification style and request style for tests/debugging. It records a best-effort cancel request while the stdio read loop remains active; because the runtime has no public active prompt interrupt hook yet, cancellation is observed between runtime stream chunks and reported as limited in request-style responses. |

Prompt output is emitted as ACP `agent_message_chunk` updates. Runtime tool request/completion events are summarized as `tool_call` and `tool_call_update` with stable per-turn tool call ids; other runtime events are exposed as `agent_thought_chunk` JSON so clients can observe the run without parsing VoidCode's internal stdout. Runtime failures are surfaced as generic `agent_message_chunk` failure text and JSON-RPC errors rather than silent process exits or raw exception leaks.

The facade enforces bounded request lines, prompt text, and in-memory session count to protect the long-running stdio process from accidental client-side floods.

Protocol errors are reported as JSON-RPC errors for malformed JSON, invalid request objects, unknown methods, invalid params, missing params, and unknown ACP session ids. Runtime failures are surfaced as protocol-visible `session/update` failure notifications and JSON-RPC errors; they should not silently terminate stdout framing.

The stdio facade intentionally remains a thin client/protocol layer over the runtime boundary. Runtime configuration is still loaded with the normal `load_runtime_config()` path, `--approval-mode` is honored the same way as `run` and `serve`, and execution still goes through `VoidCodeRuntime`.

### Supported Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | boolean | No | `false` | Enable ACP. Currently limited to managed path only. |
| `transport` | string | No | `"memory"` | Transport type. Only `memory` is supported. |
| `handshake_request_type` | string | No | `"handshake"` | Request type for handshake. |
| `handshake_payload` | object | No | `{}` | Additional handshake payload. |

### ACP Status

**ACP is not yet a full agent-to-agent bus.** The current implementation provides:

- Managed adapter state (disabled/enabled)
- Connect/disconnect lifecycle
- Basic request envelope support
- Limited runtime events

**Not yet implemented:**

- agent-to-agent messaging bus
- Multi-agent routing plane
- Recoverable delegated execution
- Supervisor/worker transport
- Subagent orchestration over stdio ACP
- MCP-over-ACP, HTTP ACP, auth, full session replay/list/load, or full OpenCode parity

### Examples

#### Enable ACP (Memory Transport)

```json
{
  "acp": {
    "enabled": true,
    "transport": "memory",
    "handshake_request_type": "handshake",
    "handshake_payload": {
      "version": "1.0"
    }
  }
}
```

#### Disable ACP (Default)

```json
{
  "acp": {
    "enabled": false
  }
}
```

---

## Complete Examples

### Leader Agent with Full Configuration

```json
{
  "runtime": {
    "max_steps": 8
  },
  "agent": {
    "preset": "leader",
    "model": "claude-sonnet-4-20250514",
    "execution_engine": "provider",
    "skills": ["default"],
    "provider_fallback": {
      "providers": ["anthropic", "openai"],
      "retry_on_failure": true
    }
  },
  "acp": {
    "enabled": false
  }
}
```

### Repository-Local Override

Place `.voidcode.json` in the repository root to override user-level settings:

```json
{
  "runtime": {
    "max_steps": 12
  },
  "agent": {
    "preset": "leader",
    "model": "claude-sonnet-4-20250514",
    "skills": ["default", "security"]
  }
}
```

---

## Configuration Precedence

Configuration loads in this order (later overrides earlier):

1. **Environment**: `VOIDCODE_*` environment variables
2. **User config**: `~/.voidcode.json`
3. **Repository-local**: `.voidcode.json` in the workspace root
4. **Request metadata**: Per-session overrides

---

## Error Handling

### Invalid Preset

If you specify a non-existent preset or try to use a subagent preset as top-level:

```
Error: Unknown agent preset 'unknown'. Valid presets: leader, worker, advisor, explore, researcher, product
```

Top-level selectable presets are `leader` and `product`. Other subagent presets (`worker`, `advisor`, `explore`, `researcher`) can only be executed via runtime-owned delegation, not as top-level active execution.

### Invalid Model

If the specified model is not available:

```
Error: Model 'invalid-model' not found. Available models: claude-sonnet-4-20250514, claude-3-5-sonnet-20241022, ...
```

Check your provider API keys are configured correctly.

### Invalid Skill

If you reference a skill that doesn't exist:

```
Error: Skill 'unknown-skill' not found. Available skills: default, code-review, security
```

Use `voidcode skills list` to see available skills.

### ACP Transport Error

Only `memory` transport is supported. Using other transports:

```
Error: ACP transport 'stdio' not supported. Only 'memory' transport is available.
```

---

## Related Documentation

- [Agent Architecture](./agent-architecture.md) - High-level agent design and roadmap
- [Runtime Config](./contracts/runtime-config.md) - Full runtime configuration contract
- [Runtime Events](./contracts/runtime-events.md) - Event emitted to clients
