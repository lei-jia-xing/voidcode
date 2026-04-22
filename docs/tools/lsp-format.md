# `lsp` / `format_file`

`lsp` and `format_file` are runtime-managed code intelligence and formatting tools. They are exposed only when the corresponding runtime subsystem is available.

## `lsp`

Use `lsp` for language-server facts:

- definition
- references
- hover
- document symbols
- workspace symbols
- implementation
- call hierarchy

Do not use it for literal text search or directory discovery.

`lsp` is read-only.

## `format_file`

Use `format_file` when the intended operation is only formatting a single file.

Do not use it to make semantic content changes. Content changes should go through `edit`, `multi_edit`, or `apply_patch`, with formatting as a follow-up when needed.

`format_file` is `read_only=false`, so it may trigger approval.

## Return value

For `lsp`, the key source of truth is:

- `data.lsp_response`

For `format_file`, important fields include:

- `data.path`
- `data.language`
- `data.command`
- `data.attempted_commands`
- `data.stdout`
- `data.stderr`

## Boundary with neighboring tools

- Literal text search: use `grep`
- Structural code search: use `ast_grep_search`
- Definition / references / hover: use `lsp`
- Formatting only: use `format_file`
- Semantic edits: use `edit`, `multi_edit`, or `apply_patch`
