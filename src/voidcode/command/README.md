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

## Builtin prompt commands

The minimal builtin set provides exactly six product commands. These commands package common
workflow intent into prompts; they do not directly call tools or bypass runtime approval/session
governance.

| Command | Arguments | Execution mode | Default behavior | Verification guidance |
|---------|-----------|----------------|------------------|-----------------------|
| `/review [target]` | File, directory, PR/diff target, or empty for current changes | Default runtime prompt | Read-only | Report missing/unreadable targets instead of generic reviews |
| `/fix [problem]` | Concrete failing test, lint/type error, review comment, or bug | Default runtime prompt | May edit | Locate root cause, minimally edit, and run targeted checks |
| `/explain [target]` | File, module, stack trace, error, or behavior | Default runtime prompt | Read-only | State clearly when the target cannot be found or read |
| `/plan [goal]` | Implementation goal, acceptance criteria request, or issue shape | `product` agent | Read-only | Produce plan, risks, acceptance criteria, and verification strategy |
| `/test [target]` | Code or behavior to test, or failing test output | Default runtime prompt | May edit tests | Prefer targeted tests before broad suites; never delete/weaken tests |
| `/commit [context]` | Optional commit intent/context | Default runtime prompt | Read-only | Inspect status/diff; report clean tree instead of inventing a message |

Commands render templates into runtime prompts through `CommandRegistry` → `resolve_prompt_command()` → `render_command_template()`. The rendered prompt replaces the slash command line before graph or provider execution. Builtins are defined in `loader.py` as `_BUILTIN_COMMANDS` and can be overridden by project-local `commands/**/*.md` files.
