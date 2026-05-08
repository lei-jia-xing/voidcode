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

Command frontmatter may also declare `workflow_mode` and legacy `workflow_preset`. `workflow_mode` is the public selector, while `workflow_preset` stays readable for compatibility with older commands and session metadata. When both are present, they must resolve to the same mode.

## Builtin prompt commands

VoidCode ships eleven builtin prompt commands. They package common workflow intent into prompts; they do not directly call tools or bypass runtime approval/session governance.

The set is split into two groups: **product** commands that reflect everyday developer intent, and **runtime/operational** commands that drive runtime-owned continuation loops.

### Product commands

| Command              | Arguments                                                              | Execution mode                      | Default behavior                                   | Verification guidance                                                                   |
| -------------------- | ---------------------------------------------------------------------- | ----------------------------------- | -------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `/init [focus]`      | Optional focus notes for the project knowledge base                    | Default runtime prompt              | May write `AGENTS.md`                              | Inspect repo structure, generate/update structured project instructions, then read back |
| `/review [target]`   | File, directory, PR/diff target, or empty for current changes          | Default runtime prompt              | Read-only                                          | Report missing/unreadable targets instead of generic reviews                            |
| `/fix [problem]`     | Concrete failing test, lint/type error, review comment, or bug         | Default runtime prompt              | May edit                                           | Locate root cause, minimally edit, and run targeted checks                              |
| `/explain [target]`  | File, module, stack trace, error, or behavior                          | Default runtime prompt              | Read-only                                          | State clearly when the target cannot be found or read                                   |
| `/plan [goal]`       | Implementation goal, acceptance criteria request, or issue shape       | `product` mode + `review` workflow  | Read-only workspace; may update runtime todo state | Produce plan, risks, acceptance criteria, verification strategy, and start-work handoff |
| `/start-work [plan]` | Accepted plan, handoff, issue, session summary, or implementation goal | `sustain` mode                      | May edit                                           | Restate target, execute smallest safe change, track progress, and verify                |
| `/test [target]`     | Code or behavior to test, or failing test output                       | Default runtime prompt              | May edit tests                                     | Prefer targeted tests before broad suites; never delete/weaken tests                    |
| `/commit [context]`  | Optional commit intent/context                                         | Default runtime prompt              | Read-only                                          | Inspect status/diff; report clean tree instead of inventing a message                   |
| `/compact [focus]`   | Optional focus notes for what must be preserved                        | Default runtime prompt              | Read-only                                          | Refresh runtime-owned continuity summary; do not edit workspace files                   |

### Runtime / operational commands

These drive runtime-owned continuation loops. They are intentionally operational (not editorial) and are surfaced here so the discovery surface matches what actually ships.

| Command                  | Purpose                                                              |
| ------------------------ | -------------------------------------------------------------------- |
| `/continuation-loop`     | Start or continue a runtime-owned continuation loop on the session   |
| `/intensive-loop`        | Start a higher-intensity continuation loop with verification state   |
| `/cancel-continuation`   | Cancel the active continuation loop on the session                   |

`/init` is intentionally a prompt command, not a separate CLI bootstrap flag: the active agent inspects the actual repository and writes a structured `AGENTS.md` with stable project knowledge. It should preserve useful existing guidance, avoid secrets and transient task state, and verify by reading the final file.

Commands render templates into runtime prompts through `CommandRegistry` → `resolve_prompt_command()` → `render_command_template()`. The rendered prompt replaces the slash command line before graph or provider execution. Builtins are defined in `loader.py` as `_BUILTIN_COMMANDS` and can be overridden by project-local `commands/**/*.md` files.

Workflow mode is assembled through the dedicated `workflow_mode_prompt_context` slot, not by folding it into the agent prompt text. That keeps command intent, agent prompt materialization, and workflow guidance separate during runtime prompt assembly.
