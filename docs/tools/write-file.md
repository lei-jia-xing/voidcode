# `write_file`

`write_file` writes a complete UTF-8 text file inside the workspace. It is the right tool when the whole target content is known and replacing the entire file is intentional.

## When to use it

Good fit:

- creating a new file,
- replacing a small file in full,
- generating a complete standalone document or fixture,
- writing content where partial replacement would be more fragile than full replacement.

Bad fit:

- focused changes in an existing file,
- changing one function, paragraph, or config entry,
- any case where you have not read an existing target before overwriting it.

## Risk profile

`write_file` is `read_only=false`, so it may trigger approval.

The main risk is accidental overwrite. For existing files, prefer `edit`, `multi_edit`, or `apply_patch` unless the task is explicitly a full rewrite.

## Input

```json
{
  "path": "docs/new.md",
  "content": "# New\n"
}
```

## Return value

On success, expect:

- `content`: written file content
- `data.path`
- `data.byte_count`

## Boundary with neighboring tools

- New file or intentional full replacement: use `write_file`
- One focused replacement: use `edit`
- Several same-file replacements: use `multi_edit`
- Multi-file or patch-shaped work: use `apply_patch`
