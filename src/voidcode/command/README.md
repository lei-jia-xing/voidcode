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

Markdown command files may include a YAML frontmatter block. The block syntax — delimiters, safe YAML loading, duplicate-key rejection, key/field whitelist boundaries, and the frontmatter size bound — is shared through `voidcode/frontmatter.py`; this loader owns only the command fields:

```md
---
description: Review a target
agent: reviewer
enabled: true
---

Review $1 with full context: $ARGUMENTS
```

Standard YAML semantics apply inside the block: values are implicitly typed (`enabled: yes` is a boolean, `enabled: sometimes` is a string and is rejected), quoted values keep commas and colons, and an unquoted `#` starts a comment. A file with no `---` opening line, or with an opening line that never closes, is treated as plain markdown with no frontmatter. Duplicate keys, non-string keys, and malformed YAML fail with the file path and position. A file that declares frontmatter must also declare a template body.

Frontmatter is plain YAML metadata, so a ` #` inside an unquoted scalar starts a comment: `description: Fix bug #42` reads as `Fix bug`. Quote the value when you mean the text literally (`description: "Fix bug #42"`). The same applies to `: ` inside a value and to values starting with a YAML indicator character (`&`, `*`, `!`, `%`, `@`, backtick).

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
