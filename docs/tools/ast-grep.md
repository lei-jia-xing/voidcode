# `ast_grep_*`

The `ast_grep_*` tools are for structural code search and rewrite. They are useful when syntax shape matters more than literal text.

This page covers:

- `ast_grep_search`
- `ast_grep_preview`
- `ast_grep_replace`

## Selection order

### `ast_grep_search`

Use when you need to find code by structure.

Good fit:

- finding classes, calls, imports, decorators, or expression shapes,
- avoiding brittle literal search for syntax-aware patterns,
- collecting candidate locations before editing.

Bad fit:

- plain text search,
- documentation search,
- searching one known file for a literal string.

`ast_grep_search` is read-only.

### `ast_grep_preview`

Use before structural replacement. It previews a rewrite without changing files.

Good fit:

- checking the number of replacements,
- inspecting matched locations,
- deciding whether a rewrite is too broad.

`ast_grep_preview` is read-only.

### `ast_grep_replace`

Use only after previewing the rewrite and confirming the scope is appropriate.

`ast_grep_replace` is `read_only=false`, so it may trigger approval. It should be reserved for structural rewrite work, not ordinary text replacement.

## Return value

Important fields include:

- `data.match_count`
- `data.replacement_count`
- `data.matches`
- `data.applied`

Prefer these structured fields over prose summaries when planning the next step.

## Boundary with neighboring tools

- Literal search in one known file: use `grep`
- Structural search: use `ast_grep_search`
- Small text replacement: use `edit`
- Structural rewrite: preview with `ast_grep_preview`, then apply with `ast_grep_replace`
