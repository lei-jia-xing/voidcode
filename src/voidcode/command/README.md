# Command System

`voidcode.command` handles prompt command discovery, resolution, and template rendering.

## Boundaries
- Prompt commands (`/command`) expand into user prompt text before core turn execution.
- Tool execution is separate: models request tools through runtime governance and permissions.
- TODO: 支持 !<shell 指令> 用户入口；执行仍经 runtime 权限、审批、持久化与取消边界。
- TODO: 重新评估 `$ARGUMENTS` / `$1`–`$9` 的字面量与递归展开边界，并比较 hook 组装与纯模板。

## Sources
Commands load in priority order (later overrides earlier):
1. Builtin commands (`loader.py`)
2. Extension commands
3. User command directory (`~/.config/voidcode/commands/`)
4. Project commands (`commands/**/*.md` and `.voidcode/commands/**/*.md`)
Command files use optional YAML frontmatter (`description`, `agent`, `enabled`) followed by the template body with `$ARGUMENTS` and `$1`–`$9` positional parameters.
