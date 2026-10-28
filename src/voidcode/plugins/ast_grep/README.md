# ast_grep

Structural code search and rewrite through the `ast-grep` CLI.

## Surface

- Tool `ast_grep` with `mode` `search` | `preview` | `replace`. Required arguments: `mode`, `pattern`, `path` (workspace-relative or absolute file or directory). Optional: `rewrite` (required for `preview`/`replace`), `lang`, `apply` (required `true` for `replace`), `expectedHash` (optional caller-supplied SHA-256, single-file replace only).
- Declares the `WRITE` effect with `replay_policy="never"`, so every mode — including read-only search — routes through the mutating-tool governance path.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Spawns `ast-grep run --json=stream` via the process service with a 30s timeout; a missing binary surfaces as a tool error. Targets must exist and stay inside the resolved scope (escape attempts are rejected).
- Matches spill full native JSONL to an artifact; inline output renders at most 50 matches.
- `replace` preflights every target: non-UTF-8 targets are rejected, a caller-supplied `expectedHash` is compared against current bytes, and targets whose bytes changed between the preview run and the commit check fail with a `stale_edit` diagnostic. `expectedHash` is an explicit content hash, not authorization or read provenance.
- Match excerpts and diff hunks claim no read observations; the replace guards above compare hashes only, with no prior-read requirement.
