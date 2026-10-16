# Native Commands

`voidcode.command` defines the authoritative table of native slash commands (`/`), argument rules, and busy gates (Codex parity).

## Boundaries
- Slash commands (`/`) form a closed set of built-in actions, pickers, and modes defined in `native.py` (`NATIVE_COMMANDS`).
- Upstream Codex has no custom slash commands. Custom markdown macro commands and `$ARGUMENTS` prompt expansion are removed.
- Custom capability extensibility is provided via Skills (`$skill_name`). See `src/voidcode/skills/` and `SkillRegistry`.
- Tool execution is separate: models request tools through runtime governance and permissions.
- Local shell execution is invoked via `!<command>` (escape).

## Native Table
All client surfaces (HTTP `GET /api/commands`, TUI `_submit`, and web client `App.tsx`) derive directly from `native.py:NATIVE_COMMANDS`.
