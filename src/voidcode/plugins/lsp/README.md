# lsp

Code intelligence through workspace language servers, plus the document-state query other tools rely on.

## Surface

- Tool `lsp` (effects `READ` + `EXECUTE`) with `operation` `diagnostics` | `goToDefinition` | `findReferences` | `rename` | `codeAction` | `willRenameFiles` | `hover` | `documentSymbol` | `workspaceSymbol` | `goToImplementation` | `incomingCalls` | `outgoingCalls`. Required: `operation`, `path` (workspace-relative, also drives server resolution). Position ops need 1-based `line` + `character`; `rename` needs `newName`, `willRenameFiles` needs `newPath`; results page with `offset`/`limit` (≤ 50).
- Command `/lsp` (no arguments): lists the servers this workspace resolves to with status, command, extension filter, and last error. Passive — listing never spawns a server.
- Query `workspace.document_version`: answers the caller's already-opened document state (path, version, content hash, position encoding) without starting a server or sending `didOpen`/`didChange`. This is what `apply_workspace_edit` checks against.

## Configuration

Plugin-only keys (strict; `providers` accepted and ignored):

- `enabled` (bool, default `true`): `false` disables LSP support.
- `servers`: map of server name to override — `preset`, `command`, `extensions`, `languages`, `root_markers`, `settings`, `init_options`. Empty means derive from workspace markers; names must be non-empty.

See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- The plugin owns the session-scoped connection manager: servers spawn lazily on first request over supervised host service processes, document state and request timeouts are tracked, and `close` shuts everything down.
- Filesystem-nonmutating but not read-only-admitted: `rename` and `willRenameFiles` return computed edits for the caller to apply and nothing here writes files — but the tool declares `READ` + `EXECUTE` for the supervised server processes it spawns, so it routes through `EXECUTE` approval and a read-only stance denies it. Failures surface as tool errors, not exceptions.
