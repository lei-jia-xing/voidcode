# ast_grep

Structural code search and rewrite through the `ast-grep` CLI.

## Surface

- Tool `ast_grep` (`READ`) with `mode` `search` | `preview`. Required arguments: `mode`, `pattern`, `path` (workspace-relative or absolute file or directory). Optional: `rewrite` (required for `preview`), `lang`, `include`, `exclude`, `include_hidden` (default `true`), `respect_gitignore` (default `false`), `limit` (default 200, max 5000).
- Tool `ast_grep_replace` (`WRITE`, never replayed): one workspace-relative or absolute **regular file**, `pattern`, `rewrite`, `apply: true`, and a required lowercase 64-character `expectedHash`. Directory, multi-file, external, and literal-symlink targets are refused. `lang`, `include_hidden`, and `respect_gitignore` are accepted; language is inferred from the file when omitted.
- Search may read external paths, consistent with `read`, `grep`, and `glob`; it never writes. Replace is workspace-only.

## Configuration

Takes no plugin config; `create_plugin` ignores it. The executable is discovered on `PATH` as `ast-grep`; there is no executable-path or version configuration. `ast-grep` 0.50+ is required (tested with 0.51.1), and invocation fails with a typed `tool_environment_error` carrying install guidance when the binary is missing.

## Behavior and limits

- `lang` is required when `path` is a directory: ast-grep cannot disambiguate one structural pattern across languages. For a file target it is inferred with `ast-grep outline --json=stream`. A malformed pattern is a typed `tool_input_mismatch` (`invalid_arguments`), never a zero-match success. Other exit-0 stderr warnings stay visible in the result body/output.
- Search defaults match `grep`/`glob`: hidden entries are included, and the ignore stack is not consulted unless `respect_gitignore=true`. `ast-grep` applies its own VCS ignore stack inside a repository (nested ignore files, `.git/info/exclude`, and global excludes), which is wider than `grep`'s workspace-root stack; `grep`/`glob` read nested `.gitignore` files and only the search root's `.git/info/exclude`. `include`/`exclude` are post-filtered with the shared `pathlib` glob semantics; default dependency/build directories are excluded below the requested root. Because filters are applied after ast-grep returns, they never silently re-include ignored files and they do not reduce scan cost. `limit` is enforced after filtering; the full filtered native JSONL remains in the artifact.
- Every path argument is passed after `--`. Search resolves paths like `grep` and reports typed `invalid_path`/`target_not_found`/`not_a_regular_file` failures. Replace routes through `resolve_workspace_path(..., allow_outside_workspace=False)`, refuses a literal symlink leaf (`symlink_target`), and rejects anything that is not one regular file (`not_a_regular_file`).
- Replace checks `expectedHash` before the preview and re-hashes after the preview and immediately before commit; every race is a typed `stale_edit`/`content_hash_mismatch`. The apply command's reported count is compared with actual bytes; an unchanged file is a `no_op` failure, and every success carries `Change.content_hash`. A timeout/cancel after apply starts re-hashes the target and reports a possible partial prefix as a `Change` on the failure.
- Preflight and diff generation retain at most 64 KiB per target; larger files are hashed/UTF-8-validated in a streaming pass and report stats without a diff.
- The preview/search process captures up to 8 MiB of stdout (1 MiB for language inference and apply). On capture overflow, timeout, or cancellation the result is a typed partial failure with the captured bytes stored as an artifact reference instead of being discarded. Zero-match results leave no empty artifact, and a failed replace never leaves an orphan preview artifact.
- Match excerpts and diff hunks claim no read observations; replacement is guarded by the caller's `expectedHash` plus its own re-hashes, not by prior-read history.
