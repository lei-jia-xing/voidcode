# Command system

`voidcode.command` owns command definitions, discovery, resolution, and command-adjacent events.

## Boundaries

- **Prompt commands / slash commands** render into runtime prompts before graph execution.
- **Tool instructions** (`read`, `grep`, `run`, `write`) are parsed here so graph and provider paths share one implementation.
- **TUI commands** are local UI actions identified by stable IDs and are intentionally separate from prompt commands.

## Sources

The MVP loader merges commands in this order, with later sources overriding earlier ones:

1. builtin commands
2. optional user command directory
3. project-local `commands/**/*.md`
4. project-local `.voidcode/commands/**/*.md`

Markdown command files may include simple YAML-like frontmatter:

```md
---
description: Review a target
agent: reviewer
enabled: true
---

Review $1 with full context: $ARGUMENTS
```

Templates currently support `$ARGUMENTS` and `$1` through `$9`. Argument splitting uses `shlex` so quoted arguments are preserved.

Command frontmatter may declare a runtime `mode` (`normal` or `plan`). Unknown modes are rejected.

## Builtin prompt commands

VoidCode ships two builtin prompt commands. They package common workflow intent into prompts; they do not directly call tools or bypass runtime approval/session governance.

| Command            | Arguments                                        | Execution mode | Default behavior | Verification guidance |
| ------------------ | ------------------------------------------------ | -------------- | ---------------- | --------------------- |
| `/init [focus]`    | Optional focus notes for the project knowledge base | Default runtime prompt | May write `AGENTS.md` | Generate or refresh structured project knowledge, then read back the final file |
| `/plan [goal]`     | Implementation goal, acceptance criteria request, or issue shape | `plan` mode | Read-only; may update runtime todo state | Produce a concrete goal with acceptance criteria, risks, and a verification strategy |

`/init` is intentionally a prompt command, not a separate CLI bootstrap flag: the active agent inspects the actual repository and writes a structured `AGENTS.md` with stable project knowledge. It should preserve useful existing guidance, avoid secrets and transient task state, and verify by reading the final file.

Commands render templates into runtime prompts through `CommandRegistry` → `resolve_prompt_command()` → `render_command_template()`. The rendered prompt replaces the slash command line before graph or provider execution. Builtins are defined in `loader.py` as `_BUILTIN_COMMANDS` and can be overridden by project-local `commands/**/*.md` files.

A command-declared `mode` is written into the request metadata `mode` field, where the runtime aggregation point (`resolve_mode`) turns it into the effective read-only stance and context transform refs.
