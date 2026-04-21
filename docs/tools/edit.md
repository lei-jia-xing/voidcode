# `edit`

`edit` is the default single-file text replacement tool. It is best when you already know the target file and can point to a stable piece of old text that should be replaced.

## When to use it

Good fit:

- you have already read the target file,
- you can provide a stable `oldString`,
- you only need one focused replacement in one file,
- you want structured diff output and replacement metadata.

Bad fit:

- you are writing before reading,
- you need several dependent edits in the same file (prefer `multi_edit`),
- you need a cross-file or patch-level change (prefer `apply_patch`).

## Input

```json
{
  "path": "src/example.py",
  "oldString": "old text",
  "newString": "new text",
  "replaceAll": false
}
```

Fields:

- `path`: file path relative to the workspace
- `oldString`: text to match and replace
- `newString`: replacement text
- `replaceAll`: optional, defaults to `false`

## Matching behavior

`edit` is more than a raw exact string replace. The current implementation tries several matching strategies, including:

- exact matching
- line-trimmed matching
- block-anchor matching with small typo tolerance
- whitespace-normalized matching
- indentation-flexible matching
- escape-normalized and context-aware fallbacks

This makes it more forgiving than a naive string replacement, but it also means:

- there is still risk if you did not read enough context first,
- the tool refuses to guess if multiple matches are found and `replaceAll=false`.

## Return value

On success, expect:

- `content`: `Edit applied successfully.` (with replacement count if needed)
- `data.path`
- `data.additions`
- `data.deletions`
- `data.match_count`
- `data.diff`

If formatter-aware closure is active, it may also return:

- `data.formatter`
- `data.diagnostics`

## Formatter-aware closure

The current implementation does the following:

1. write the new content,
2. if hooks / formatter presets are configured, try a formatter,
3. read the file again,
4. build the final diff and result from the **final on-disk file state**.

The main rule for agents is:

> The file state after `edit` finishes is the truth. Do not assume the pre-formatter text is still present.

## Common failures

- `Multiple matches found. Use replaceAll to replace all occurrences.`
- `Could not find oldString in the file using replacers.`
- `edit only allows paths inside the workspace`
- `edit target does not exist: ...`

## Boundary with neighboring tools

- One focused replacement in one file: prefer `edit`
- Several dependent edits in one file: prefer `multi_edit`
- Patch-level or cross-file changes: prefer `apply_patch`
- Creating a new file from scratch: prefer `write_file`
