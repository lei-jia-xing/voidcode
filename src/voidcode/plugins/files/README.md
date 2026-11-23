# files

Workspace file read, write, edit, and search tools.

This package is a thin adapter: the shared `voidcode.capabilities.files` handlers decorate their public `invoke` boundary so expected diagnostics and argument-validation errors surface as typed model-visible failures. `create_plugin` registers those decorated tools directly; no plugin-owned wrapper is involved.

## Surface

- Tool `read` (`READ`): read a file or resource URI. An untruncated page that starts at `offset=1` and reaches EOF returns a SHA-256 content hash, one whole-file read observation, and the line count. A page starting at `offset>1` never returns a whole-file hash or coverage, even when it reaches EOF. A partial page (more lines remain) returns no hash and no coverage and keeps `next_offset`. Line count is reported whenever the read reaches EOF, including `offset>1` pages and clipped-page artifact passes that scan to EOF. `read(hash_only=true)` streams a regular file path with bounded memory and returns only its SHA-256 and byte size — the explicit way to obtain `expectedHash` after a partial read. It is mutually exclusive with `offset`/`limit`/`byte_offset`/`byte_limit` and does not work on resource URIs. Any `scheme://` URI dispatches to the plugin that registered the scheme; disabling that plugin makes the scheme unsupported with no fallback.
- Resource readers `tool://`, `artifact://`, `transcript://`: the default namespaces this plugin serves (tool docs, stored-artifact pages, lineage-gated transcript pages). Each parses its own URI and default; `read` itself knows no scheme table.
- Tool `write` (`WRITE`, never replayed): write a UTF-8 text file inside the workspace. It commits the caller's text verbatim, including its line endings, so it can create a mixed-ending file; `edit` refuses mixed-ending inputs instead of normalizing them.
- Tool `edit` (`WRITE`): two modes sharing one mutation core. Text mode replaces exact current text (single replacement or ordered batch). Range mode (`mode="range"`) applies 1-based line/character edits across one or more files; `positionEncoding` may be `utf-8`, `utf-16`, or `utf-32`. Every edit carries `expectedHash`; `version` optionally binds it to the LSP `workspace.document_version` answer. Text matching is EOL-insensitive: content and `oldString` are normalized to LF for matching and committed bytes use the file's one uniform ending (LF, CRLF, or lone CR). A mixed-ending file is refused with a diagnostic, never silently normalized. Success output names the new SHA-256 of every changed file.
- Tool `glob` (`READ`): find files matching a glob pattern.
- Tool `grep` (`READ`): search workspace files; literal by default, `regex=true` for regular expressions.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Path policy: `write` and both `edit` modes refuse any resolved target outside the workspace with a typed `ToolFailure`; a path the filesystem cannot resolve (such as one with an embedded NUL) is reported as `invalid_path`, not as an external target. `read`, `glob`, and `grep` keep external read access; the runtime's permission tiers govern it.
- File mutations validate explicit caller evidence and exact matching — replacement modes compare caller-supplied content hashes, and no mode requires a prior session read. Read observations are historical facts, not write permissions (see [facts](../../../../docs/contracts/facts.md)). Stale-hash, commit-race, ambiguous-match, and mixed-line-ending failures surface as typed `ToolFailure` results with retry guidance — the boundary this adapter adds.
- `read` stops making page decisions after the requested page plus a cheap one-byte EOF probe. It may read ahead up to one 8 KiB raw chunk, but it never decodes unseen later bytes. It never materializes an unbounded line: a visible line beyond 2000 characters switches immediately to the full-file artifact pass, renders a truncated prefix, spills the whole file for byte paging in one pass (whose digest and byte count must match the stored artifact, and whose visible prefix is checked against the initial page read), and does not claim a whole-file hash. The rendered content page reserves the `TextOutput` envelope (`MAX_RESOURCE_PAGE_BYTES`) so a near-budget whole-file read is not projection-clipped.
- `TextOutput.text` is the selected page. Its typed body retains machine fields such as `raw_content`, line records, and the whole-file hash at the canonical result artifact when Core omits that redundant body; omission alone does not mark the page or `ReadObservation` clipped.
- Raster reads publish original bytes through streaming artifact ingress rather than an arbitrary media-size cutoff. Real Core/native Anthropic and Google routes preserve valid decoded PNG pixels above the 256 KiB artifact-page size; complete bytes remain pageable by digest.
- Multi-file range commits preflight every path, hash, document version, and range before the first write. A later I/O failure may leave an applied prefix; the failure reports that prefix, its changes, and the uncommitted remainder.
- Query-absence behavior: `read`/`write`/text `edit` never consult a workspace query; range `edit` with `version` fails closed (`document_version_unavailable`) when no provider answers, while edits carrying only a content hash (no version) work without any provider. A provider hash or version mismatch is a typed stale failure; a malformed answer is a visible contract failure.
- Write-class tools declare `WRITE` and route through the core's approval check; see [plugin authoring](../../../../docs/contracts/plugin-authoring.md) for the surrounding contract.
