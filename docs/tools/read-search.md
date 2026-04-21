# Read / Search Tools

This page covers the most common read-only context collection tools:

- `read_file`
- `list`
- `glob`
- `grep`

All of them are currently `read_only=true`, so they are the default low-risk tools for gathering context.

## Selection order

### `read_file`

**Use when:** you already know which file you need and want the full file content.
**Do not use when:** you need directory discovery, broad search, or fuzzy location.

- Input: `{"path": "relative/path.txt"}`
- Key return fields:
  - `content`: full file contents
  - `data.path`
  - `data.line_count`

### `list`

**Use when:** you need an initial view of the directory structure.
**Do not use when:** you already know the filename pattern or need content search.

- Input:
  - `path`: optional directory path
  - `ignore`: optional extra ignore glob patterns
- Key return fields:
  - `content`: tree-like directory listing
  - `data.path`: actual root that was listed
  - `data.count`
  - `data.truncated`

### `glob`

**Use when:** you know a filename or extension pattern and want matching files.
**Do not use when:** you need to search file contents.

- Input:
  - `pattern`
  - `path`: optional search root
- Key return fields:
  - `content`: matching relative paths
  - `data.matches`
  - `data.count`
  - `data.truncated`

By default, results are capped and common generated directories are skipped.

### `grep`

**Use when:** you know a single target file and want to search for a literal string inside it.
**Do not use when:** you need repo-wide search or regex-style search.

- Input:
  - `pattern`
  - `path`
- Key return fields:
  - `content`: summary plus a small preview
  - `data.match_count`
  - `data.matches[]`: `line`, `text`, and `columns`

## Practical selection rules

- Need the directory tree: start with `list`
- Need files by pattern: use `glob`
- Need literal search inside one known file: use `grep`
- Need full contents of one known file: use `read_file`

## Anti-patterns

- Using `shell_exec ls` instead of `list`
- Using `read_file` to scan a whole repository
- Using `glob` to search contents
- Using `grep` as if it were repo-wide search
