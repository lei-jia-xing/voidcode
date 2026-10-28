# Markdown agent definitions

Agent files follow the [Claude Code subagent format](https://code.claude.com/docs/en/sub-agents):
YAML metadata followed by Markdown instructions. The required fields are `name`
and `description`; identity comes from `name`, not the filename. Definitions load
recursively from `.claude/agents/` in the workspace and `~/.claude/agents/`
(`CLAUDE_CONFIG_DIR/agents/` when configured). Project definitions override user
and built-in definitions of the same name. Duplicate names within one scope are
errors that identify both files.

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

## Supported execution subset

| Field | VoidCode behavior |
| --- | --- |
| `name` | Exact, case-sensitive identity, up to 256 characters; `:` is reserved. |
| `description` | Description shown by the agent catalog. |
| `tools` | Optional comma-separated string or YAML list. Omission inherits the parent scope. A supplied list restricts that scope. |
| `model` | Omission or `inherit` uses the workspace model. Other values are explicitly unsupported. |
| `maxTurns` | Positive cap on the initial child run, bounded further by the caller's `max_steps`. |

The Claude tool identifiers `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, and
`Agent` map at file ingress to `read`, `write`, `edit`, `glob`, `grep`, `shell_exec`,
and `task`. Native catalog names can also be listed. This is a format adapter,
not a role-specific tool table. Unknown or unavailable tool names fail through
normal core validation; no scope is silently broadened. A caller's explicit
`tool_allowlist` intersects an agent's declared tools. An empty intersection is an
error. Built-in definitions have no fixed tool lists.

This is not full Claude Code runtime compatibility. Unsupported standard fields,
such as `disallowedTools`, `permissionMode`, `skills`, `hooks`, `memory` and
`isolation`, are rejected rather than accepted without an implementation. The
current core has one user-prompt seam for child creation, so the agent instructions
prefix the child task; they are not sent as a separate system message. Built-in
read-only agents retain the existing core stance. A custom file cannot weaken an
inherited read-only stance or widen its parent's authority.

There is no alternate manifest syntax or conversion path. The former `id`,
`mode`, `prompt_append`, `tool_allowlist`, `fallback_models` and `skill_refs` file
fields are rejected. `/agents` lists definitions and `agents.show` takes `name`.

## Implementation

An `AgentDefinition` stores only effective prompt text, execution restrictions
and source metadata. It has no prompt-materialization object, display-only model
preferences, derived selectable flags, or unused serialization protocol. YAML
metadata is validated at file ingress; internal code uses the typed definition.
Agents and skills share the same bounded PyYAML parser in
`capabilities/frontmatter.py`, including duplicate-key and non-string-key checks.
The shared definition and its query contract live in
`capabilities/agent_queries.py`: an exact eight-key answer whose missing or
unknown key is malformed, never a silent scope widening.
