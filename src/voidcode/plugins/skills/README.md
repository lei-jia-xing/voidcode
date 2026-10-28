# skills

Workspace skill discovery and on-demand loading, plus always-on rule context: the `AGENTS.md` hierarchy with touched-path file rules and the `.voidcode/rules` rulebook.

## Surface

- Tool `skill` (`READ`, safe): `name` (required) loads one discovered skill's `SKILL.md` body plus metadata (description, source path, base directory); optional `user_message` carries command arguments or extra context. Unknown or disabled skills fail instead of guessing. The tool description stays static — the skill catalog travels in system-prompt metadata, not on every provider request.
- Command `/skills` (`READ`): lists discovered skills with enabled state and paths; reports "Skills are disabled." when the catalog is off.
- Context (every dispatch, three parts): the skill catalog (gated on `enabled`), touched-path file rules, then the rulebook. Rule context is independent of the skills setting — it ships whether or not skills are enabled.
- Observer on `invocation_finished`: records the workspace paths each finished invocation touched (from committed `resource_paths` in public facts, never tool-name guessing) into recency-ordered session state (most recent 64).

## Configuration

`plugins.skills.config` (strict; unknown keys refused, `providers` accepted and ignored):

- `enabled` (bool, default `false`): skill discovery and the catalog are off unless explicitly enabled. Rule context is unaffected.
- `paths` (array of non-empty strings, default `[".voidcode/skills"]`): skill search roots. Relative paths must stay inside the workspace; absolute paths are used as given.
- `config` (array of `{name?, path?, enabled?}` rules): enable/disable one skill by exact name or by file/directory path (`enabled` defaults `true` per rule).

See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Discovery rebuilds per call, so new `SKILL.md` files are visible immediately. Each skill is a directory with a `SKILL.md` whose frontmatter carries exactly `name` + `description` (both non-empty; any other key rejected) and a non-empty body. Unreadable or malformed skills fail loudly at load.
- File rules: the `AGENTS.md` hierarchy (project root upward, `AGENTS.override.md` winning per directory; max 8 files, 12 KiB each, 36 KiB total) is filtered to the session's touched paths plus baseline rules, and injected framed as runtime file rules for those paths.
- Rulebook: `.voidcode/rules` Markdown documents (max 64 files, 12 KiB content each, 240-char metadata, 12 KiB total prompt) with `always_apply` content plus `discoverable` names/descriptions/paths. Rules publish real paths; consumers read them with ordinary file tools.
- Session state (`schema_version 1`): `cursor_sequence` + `touched_paths`. Disabling keeps the payload as-is — plain history with no live resource — so re-enable resumes exactly where it left off.
