# Runtime Configuration Contract

Source issue: #16

## Purpose

Define the minimal configuration surface required to make the MVP runtime genuinely configurable without making the system uncontrolled or overly broad.

## Status

The current runtime loads repo-local configuration from `.voidcode.json` for these implemented domains:

- `approval_mode`
- `model`
- `hooks`
- `tools`
- `skills`
- `lsp`
- `acp`

Only `approval_mode` currently has multi-source precedence logic. The extension domains in this slice are config-schema support only.

## MVP configuration domains

The MVP config surface should cover only these areas:

- workspace root
- model/provider selection
- approval mode
- hook enablement/defaults
- tool discovery/provider defaults
- skill discovery defaults
- extension infrastructure toggles for LSP and ACP
- client-visible session settings needed for resume

## Planned minimal config shape

The MVP contract should be able to represent a runtime configuration object with at least:

```json
{
  "workspace": "/workspace/project",
  "model": "opencode/gpt-5.4",
  "approval_mode": "ask",
  "hooks": {
    "enabled": true
  },
  "tools": {
    "builtin": {
      "enabled": true
    },
    "paths": [".voidcode/tools"]
  },
  "skills": {
    "enabled": true,
    "paths": [".voidcode/skills"]
  },
  "lsp": {
    "enabled": false,
    "servers": {}
  },
  "acp": {
    "enabled": false
  }
}
```

Field intent:

- `workspace`: bootstrap field used to determine the runtime workspace root before repo-local config discovery, and then reused for tool execution and persistence
- `model`: provider/model identifier in OpenCode `provider/model` format
- `approval_mode`: minimum execution policy mode used by runtime-governed tools
- `hooks`: minimal switch/config object for runtime hook behavior
- `tools`: minimal built-in tool enablement plus additional tool search paths
- `skills`: minimal skill discovery enablement plus additional skill search paths
- `lsp`: minimal infrastructure config container for future language-server integration
- `acp`: minimal infrastructure enablement switch for future ACP integration

## Current implemented repo-local shape

The current `.voidcode.json` parser accepts this repo-local shape:

- `approval_mode`: one of `allow`, `deny`, `ask`
- `model`: string
- `hooks.enabled`: boolean
- `tools.builtin.enabled`: boolean
- `tools.paths`: array of strings
- `skills.enabled`: boolean
- `skills.paths`: array of strings
- `lsp.enabled`: boolean
- `lsp.servers`: object
- `acp.enabled`: boolean

All extension-domain fields are optional. When omitted, they resolve to `None` at the domain level, and array fields default to empty tuples inside a provided domain object.

## Infrastructure-only note for LSP and ACP

`lsp` and `acp` are configuration-only domains in the current slice.

- They exist so later runtime startup work can consume stable typed config.
- They do **not** mean LSP-backed tools or ACP transport are active today.
- `lsp.servers` is currently a shallow object container only; no server validation or startup behavior is implemented here.

## Bootstrap rule for workspace

`workspace` is not resolved by the same precedence ladder as normal runtime config fields.

It must be determined first so the runtime can discover any repo-local config that lives under that workspace. In MVP terms:

1. explicit runtime/bootstrap input chooses the workspace root
2. repo-local config may then be discovered inside that workspace
3. normal runtime config precedence applies to non-bootstrap fields such as `model`, `approval_mode`, and `hooks`

For the currently implemented loader, repo-local values for `model`, `hooks`, `tools`, `skills`, `lsp`, and `acp` are loaded directly from `.voidcode.json`, while `approval_mode` keeps its explicit > repo-local > environment > default precedence behavior.

## Current code anchors

- `VoidCodeRuntime(workspace=...)`
- `RuntimeRequest(prompt, session_id, metadata)`
- `SessionState.metadata`
- persisted session metadata in the SQLite-backed session store

## Recommended precedence

For MVP, non-bootstrap config fields should resolve in this order:

1. explicit session override
2. explicit client or CLI flag
3. repo-local config file
4. environment variables
5. built-in defaults

For resumed sessions, fresh explicit client or CLI input should be allowed to override persisted session settings where the runtime chooses to support overrideable fields. Persisted session settings are the baseline for resume, not an absolute override over fresh explicit input.

## Planned session override shape

Session-scoped overrides should be representable separately from repo defaults. The minimum override shape should support:

```json
{
  "session_id": "session-123",
  "overrides": {
    "model": "opencode/gpt-5.4-pro",
    "approval_mode": "ask"
  }
}
```

This is intentionally narrow: only settings that materially affect runtime behavior or resume semantics should be session-overridable in MVP.

## Session-persisted settings

Resume-critical settings should persist with the session, including at minimum:

- workspace
- approval mode
- selected model/provider when relevant to deterministic resume behavior
- any runtime mode that changes how the client should interpret the session

## Current code mapping

Current concrete storage/mapping points in the codebase are:

- `VoidCodeRuntime(workspace=...)` supplies the active workspace root
- `RuntimeRequest.metadata` is the current flexible request-scoped container
- `SessionState.metadata` stores runtime/session metadata in memory
- the SQLite session store persists `SessionState.metadata` as part of the stored session payload
- the SQLite session store also persists `workspace` as a first-class column in `sessions.workspace`, and uses it for session listing and lookup

Today, the repo-local schema above is implemented, while broader session-override and resume-specific config behavior remains intentionally narrow and only partially implemented.

## Invariants

- users can change runtime behavior without editing code
- precedence must be deterministic
- persisted sessions must carry enough config to replay or resume meaningfully
- the MVP config surface must stay single-agent focused

## Current limitations

- repo-local config is intentionally shallow for extension domains and does not yet wire runtime behavior
- only `approval_mode` currently has documented environment-variable support (`VOIDCODE_APPROVAL_MODE`)
- current request metadata is flexible but not a stable public schema

## Non-goals

- advanced multi-agent configuration
- provider-specific secret management details
- full policy DSLs

## Acceptance checks

- a config doc exists that later implementation can follow directly
- the persisted-session contract explicitly calls out which settings survive resume
- config precedence is documented once and reused by TUI/web implementation work
- the config doc includes a minimal concrete shape for repo/runtime defaults and session-level overrides
