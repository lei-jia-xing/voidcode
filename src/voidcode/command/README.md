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
