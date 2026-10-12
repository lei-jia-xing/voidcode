# Command system

`voidcode.command` owns command definitions, discovery, resolution, and command-adjacent events.

## Boundaries

- **Prompt commands / slash commands** render into runtime prompts before core turn execution.
- **Tool instructions** (`read`, `grep`, `run`, `write`) are parsed here so prompt paths share one implementation.
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

Command frontmatter does not support a runtime `mode`; declaring that field is rejected.

## Builtin prompt commands

VoidCode ships one builtin prompt command. It packages common workflow intent into a prompt; it does not directly call tools or bypass runtime approval/session governance.

| Command         | Arguments                                           | Default behavior                                  | Verification guidance |
| --------------- | --------------------------------------------------- | ------------------------------------------------- | --------------------- |
| `/init [focus]` | Optional focus notes for the project knowledge base | May write `AGENTS.md`                             | Generate or refresh structured project knowledge, then read back the final file |

`/init` is intentionally a prompt command, not a separate CLI bootstrap flag: the active agent inspects the actual repository and writes a structured `AGENTS.md` with stable project knowledge. It should preserve useful existing guidance, avoid secrets and transient task state, and verify by reading the final file.

Commands render templates into runtime prompts through `CommandRegistry` → `resolve_prompt_command()` → `render_command_template()`. The rendered prompt replaces the slash command line before provider turn execution through the runtime host. The builtin is defined in `loader.py`; project-local `commands/**/*.md` files add user commands.

The builtin `/plan` command and command-declared runtime modes are not supported. An unmatched `/plan` is rejected by the CLI as an unknown command; a user command may define `/plan`.
