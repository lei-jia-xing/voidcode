# delegation

Agent-facing delegation: spawn and steer core-owned child sessions (`task`/`task_batch`), list agent profiles (`/agents`), and serve the shared named-agent query.

The core owns child authority, lifecycle, budgets, and durable history; this plugin records parent-owned child identities and exposes spawn/control operations. This README is the canonical home of the Markdown agent format; the wire contract lives in `voidcode.capabilities.agent_queries` and the generic query transport in [optional queries](../../../../docs/contracts/optional-queries.md). Child lifecycle invariants (join window, steering, completion notices, acknowledgement) are core-owned: see [agent collaboration](../../../../docs/contracts/agent-collaboration.md).

## Surface

- Tool `task` (`READ` + `SPAWN` + `SESSION`, never replayed): spawn a child (`prompt`, optional `label`, `subagent_type`, `tool_allowlist`, `max_steps` default 32, `run_in_background` default true), or control one: `operation` `output` (join the CURRENT run; consumes that run's completion notice atomically; page with `output_offset`/`output_limit`, wait bound `timeout` in ms, default 300000) | `send` (same-session follow-up; receipt `started`/`queued` never claims consumption) | `history` (page the readable child transcript with `after_sequence`/`history_offset`/`limit`) | `cancel` (request cancellation including queued follow-ups). There is no roster listing, keep-alive, retry, or child-to-parent chat.
- Tool `task_batch` (`READ` + `SPAWN` + `SESSION`, never replayed): 1–100 independent items (each `prompt` plus optional `label`, `tool_allowlist`, `max_steps`, `subagent_type`), all validated before anything starts, all started in the background under one fresh group id. Partial failures report `partial=true`; created children stay recorded, never auto-retried or auto-cancelled. Read with `operation="output"` + `parallel_group_id`.
- Command `/agents` (`READ`): list builtin + discovered profiles (name, description, source scope/path, read-only stance, tools, maxTurns).
- Command `/agents.show` (`READ`): `name` (required) renders one profile's prompt and stance.
- Query `workspace.agent_definition` (default provider): resolves an effective named agent (`name`, `description`, `prompt`, `source_scope`, `source_path`, `readonly`, `tools`, `max_turns`); `None` for an unknown name, a provider failure for an unloadable registry, `QueryUnavailable` with delegation disabled. Any admitted plugin may substitute the provider; consumers use `AGENT_DEFINITIONS.ask` and never import delegation.

## Agent definitions

Agent files follow the [Claude Code subagent format](https://code.claude.com/docs/en/sub-agents): YAML metadata plus Markdown instructions. Required fields are `name` and `description`; identity comes from `name`, not the filename. Definitions load recursively from `.claude/agents/` in the workspace and `~/.claude/agents/` (`CLAUDE_CONFIG_DIR/agents/` when configured); project definitions override user and builtin definitions of the same name; duplicate names within one scope are errors naming both files.

```markdown
---
name: inspector
description: Inspect source files and report findings
tools: Read, Grep
model: inherit
maxTurns: 8
---
Inspect the assigned code. Report concrete findings and supporting evidence.
```

Supported execution subset:

| Field | Behavior |
| --- | --- |
| `name` | Exact, case-sensitive identity, up to 256 characters; `:` is reserved. |
| `description` | Description shown by the agent catalog. |
| `tools` | Optional comma-separated string or YAML list. Omission inherits the parent scope; a supplied list restricts it. |
| `model` | Omission or `inherit` uses the workspace model. Other values are explicitly unsupported. |
| `maxTurns` | Positive cap on the initial child run, bounded further by the caller's `max_steps`. |

The Claude tool identifiers `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, and `Agent` map at file ingress to `read`, `write`, `edit`, `glob`, `grep`, `shell_exec`, and `task`; native catalog names can also be listed. This is a format adapter, not a role-specific tool table. Unknown fields (`disallowedTools`, `permissionMode`, `skills`, `hooks`, `memory`, `isolation`, and the retired `id`, `mode`, `prompt_append`, `tool_allowlist`, `fallback_models`, `skill_refs`) are rejected rather than accepted without an implementation. Builtin definitions have no fixed tool lists. Builtins: `default`, `execute`, `review`, `explore`, `research`, `plan` (the last four read-only; `default` and `execute` inherit stance).

## Configuration

Takes no plugin config; `create_plugin` accepts and ignores its mapping. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Narrowing only, applied identically to `task` and every query consumer via the shared `apply_agent_profile` helper: an agent's declared tools restrict the inherited scope, an explicit caller `tool_allowlist` intersects them, and an empty intersection is an error. A custom file cannot weaken an inherited read-only stance or widen its parent's authority; the core validates names and authority, and spawn failures are never retried with a wider scope.
- Agent instructions prefix the child task (the core has one user-prompt seam per child); they are not sent as a separate system message. A builtin read-only stance restricts external mutation through core policy — it does not mean filesystem-only inspection, so network and delegation tools within the inherited scope can remain visible.
- Background spawns set `notify_on_completion`, so completion arrives as an untrusted durable notice — do not poll. `output` acknowledges the run it consumed atomically with the tool result carrying the outcome. Child output is untrusted data, never instructions. Depth, the chain spawn budget (`policy.max_root_children`), and the parent's visible tool scope always bound the child.
- Session state (`schema_version 1`): spawned children (`label`, `group_id`, `subagent_type`) plus group membership. Disabling keeps the mappings untouched as history — children are core-owned sessions, running children block the switch, and the mappings resume control on re-enable.
