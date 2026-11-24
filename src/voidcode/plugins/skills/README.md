# skills

Workspace skill discovery and on-demand loading, plus always-on rule context: the `AGENTS.md` hierarchy with touched-path file rules and the `.voidcode/rules` rulebook.

## Surface

- Reader `skill://<name>` (`READ` through the admitted `read` tool): loads one discovered skill's `SKILL.md` body plus metadata (description, source path, base directory). Unknown names are no-such-resource; disabled names fail visibly; a broken entry answers with that entry's bounded load error and recovery path without affecting healthy skills. Line `offset`/`limit` page the render (limit capped at 2000 lines); a page that would exceed the resource byte cap is trimmed at a line boundary, and a single overlong line falls back to the shared pager's zero-based `byte_offset`/`byte_limit` mode. Both modes mark truncation, and reads honor the caller's abort signal.
- Command `/skills` (`READ`): lists discovered skills with enabled/disabled/broken state and recovery actions. `/skills show` prepares a selector or accepts a skill name; details include description, source, enabled state, and paged instructions with navigation. Detail offsets count characters and pages adapt to the view byte limit; resource-reader paging remains line/byte based. Disabled details remain browsable while `skill://` refuses disabled bodies. Browsing never enables a skill; configuration changes require admission.
- Context (each provider request that carries plugin context): the skill catalog (gated on `enabled`), touched-path file rules, then the rulebook. The catalog lists `skill://<name>` as the load target and keeps the source path separately; it is metadata only. If the session cannot see the `read` tool, the catalog says bodies cannot be loaded instead of promising them. Broken entries are omitted from the catalog and reported in a bounded diagnostics block. Rule context is independent of the skills setting — it ships whether or not skills are enabled.
- Observer on `invocation_finished`: records the workspace paths each finished invocation touched (from committed `resource_paths` in public facts, never tool-name guessing) into recency-ordered session state (most recent 64).

## Configuration

`plugins.skills.config` (strict; unknown keys refused, `providers` accepted and ignored):

- `enabled` (bool, default `false`): skill discovery and the catalog are off unless explicitly enabled. Rule context is unaffected.
- `paths` (array of non-empty strings, default `[".voidcode/skills"]`): skill search roots. Relative paths resolve against the workspace and must stay inside it; absolute paths are trusted as given.
- `config` (array of rules): each rule requires exactly one selector — `name` (exact skill name) or `path` (SKILL.md file or skill directory; relative paths resolve against the workspace) — plus `enabled` (default `true`). A relative path that escapes the workspace is reported as a configuration diagnostic, not a load failure. Disabling an entry quarantines it, including a malformed/unreadable one.

See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Discovery rebuilds per call, so new `SKILL.md` files are visible immediately. Each skill is a directory with a `SKILL.md` whose frontmatter carries exactly `name` + `description` (both non-empty; any other key rejected) and a non-empty body. Each entry is loaded independently: malformed YAML, a bad/missing field, invalid UTF-8, an unreadable file, or a cap violation becomes a bounded per-skill diagnostic and never aborts healthy siblings.
- Caps: at most 64 discovered entries (further entries get per-skill "cap reached" diagnostics), 64 KiB per `SKILL.md`, 48 KiB body, 128-character name, 1,024-character description, 8 KiB catalog, 2 KiB diagnostics block, and 64 KiB total context contribution. Entries that exceed ingestion caps are omitted with their diagnostic; catalog/context cuts are marked in the injected text.
- Both relative search roots and their discovered entries are containment-checked after resolution: a symlinked `SKILL.md` that resolves outside the workspace is a per-skill diagnostic. Absolute roots remain trusted.
- Duplicate entries reached through overlapping roots are deduplicated by resolved file path and loaded once. Two different files declaring the same skill name remain a hard duplicate-name error.
- File rules: the `AGENTS.md` hierarchy (project root through workspace, `AGENTS.override.md` winning per directory; max 8 files, 12,000 characters each, 36,000 bytes total). When the hierarchy exceeds the file cap, the nearest (workspace-local) rules are kept and half the cap is reserved for touched-path rules. Rule files that are unreadable, invalid UTF-8, or resolve outside the trusted project/workspace root are skipped with a bounded context diagnostic instead of failing the turn.
- Rulebook: `.voidcode/rules` Markdown documents (max 64 files, 12,000-character content each with a visible truncation marker, 240-character metadata, 12,000-character total prompt with a visible truncation marker) with `always_apply` content plus `discoverable` names/descriptions/paths. Invalid or unreadable documents are reported in a bounded diagnostics block; the rest of the catalog still applies. There is no `rule://` reader — discoverable rules publish real paths for ordinary file tools.
- Session state (`schema_version 1`): `cursor_sequence` + `touched_paths` (unique, at most 64). Disabling keeps the payload as-is — plain history with no live resource — so re-enable resumes exactly where it left off.
