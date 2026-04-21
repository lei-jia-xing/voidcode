# `multi_edit`

`multi_edit` is for **multiple sequential edits in the same file**. It is effectively a runtime-owned closure around repeated `edit` operations, which makes it useful when several related changes belong together.

## When to use it

Good fit:

- the same file needs two or more related edits,
- later edits depend on the file state produced by earlier edits,
- you want to reduce drift from issuing several separate `edit` calls.

Bad fit:

- you only need a single replacement (`edit` is simpler),
- you need cross-file changes (`apply_patch` or separate file-oriented operations are better).

## Input

```json
{
  "path": "src/example.py",
  "edits": [
    {"oldString": "a", "newString": "b"},
    {"oldString": "c", "newString": "d", "replaceAll": true}
  ]
}
```

## Execution semantics

- `multi_edit` applies edits in order
- each step runs against the file state produced by the previous step
- after all edits complete, it summarizes the diff from the original file to the final file
- if formatter-aware closure is configured, formatting happens once after the sequence

This makes `multi_edit` a better fit than “agent manually sends several `edit` calls” when the file-local change set is really one operation.

## Return value

On success it returns:

- `data.path`
- `data.applied`
- `data.edits[]`: structured results for each inner edit
- `data.additions`
- `data.deletions`
- `data.diff`

If formatter-aware closure runs, it may also include:

- `data.formatter`
- `data.diagnostics`

## When it is better than `edit`

- you already know several edits belong to one file
- you do not want the agent to manually reconstruct context between separate edits
- you want one final aggregated diff

## When not to use it

- bundling unrelated edits into one large multi-step request
- using it instead of a patch for cross-file changes
- constructing a long edit array before reading the file carefully
