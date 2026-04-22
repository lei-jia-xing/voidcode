# Web / Code Research Tools

This page covers external research tools:

- `web_search`
- `web_fetch`
- `code_search`

These tools collect external evidence. They do not describe the current workspace state.

## `web_search`

Use when you do not know the URL and need to discover web sources.

Do not use it when the URL is already known; use `web_fetch` instead.

Key fields:

- `content`
- `data.query`
- `data.num_results`
- `data.source`

## `web_fetch`

Use when you have a specific `http://` or `https://` URL and need page content.

Do not use it for localhost, private network, link-local, reserved, metadata, or internal targets.

Key fields:

- `content`
- `data.url`
- `data.content_type`
- `data.format`
- `data.byte_count`

## `code_search`

Use for external programming examples or implementation references.

Do not use it for repository search. Local code facts should come from `glob`, `grep`, `read_file`, `ast_grep_search`, or `lsp`.

Key fields:

- `content`
- `data.sources`
- `data.snippet_count`
- `data.source`

## Practical selection rules

- Need to discover sources: use `web_search`
- Already have a URL: use `web_fetch`
- Need external code examples: use `code_search`
- Need local repository facts: use workspace tools, not web tools
