# files

Workspace file read, write, edit, and search tools.

This package is a thin adapter: `create_plugin` wraps the shared `voidcode.capabilities.files` tools so file diagnostics surface as typed model-visible failures.

## Surface

- Tool `read` (`READ`): read a file or resource URI. Regular-file pages report a SHA-256 content hash callers can pass back as `expectedHash`; attachment and resource-URI pages carry no such hash authorization.
- Tool `write` (`WRITE`, never replayed): write a UTF-8 text file inside the workspace.
- Tool `edit` (`WRITE`): replace text (single replacement or ordered batch) or apply a marker patch. Single/batch replacement requires the caller's `expectedHash` to match current bytes plus exact old-string matching; patch mode takes only `patch` text and validates through exact marker preflight plus checked commits, with no mandatory `expectedHash`.
- Tool `apply_workspace_edit` (`WRITE`): apply validated LSP text edits; each edit is checked against its caller-supplied `expectedHash` and, when given, an LSP document version from the document-state query.
- Tool `glob` (`READ`): find files matching a glob pattern.
- Tool `grep` (`READ`): search workspace files; literal by default, `regex=true` for regular expressions.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- File mutations validate explicit caller evidence and exact matching — replacement modes compare caller-supplied content hashes, patch mode preflights exact marker chunks and re-checks bytes at commit, and no mode requires a prior session read. Read observations are historical facts, not write permissions (see [facts](../../../../docs/contracts/facts.md)). Stale-hash, commit-race, and ambiguous-match failures surface as typed `ToolFailure` results with retry guidance — the boundary this adapter adds.
- Query-absence behavior: read/write/edit never consult a workspace query; `apply_workspace_edit` with a version fails closed (`document_version_unavailable`) when no provider answers, while hash-only edits (no version) work without any provider.
- Write-class tools declare `WRITE` and route through the core's approval check (`_needs_approval`: external mutations need approval unless policy says otherwise); see [plugin authoring](../../../../docs/contracts/plugin-authoring.md) for the surrounding contract.
