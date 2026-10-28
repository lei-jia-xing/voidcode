# web

Web search through the first configured backend, with a keyless fallback.

## Surface

- Tool `web_search` with `query` (non-empty) and optional `numResults` (1–20, default 8). Effects `READ` + `NETWORK`.
- Returns ranked results (title, URL, snippet) naming the backend in `Search source: <id>`. A selected-backend failure is returned directly as a tool error, not retried against another backend.

## Configuration

Takes no plugin config; `create_plugin` ignores it. Credentials and endpoint overrides come from the process environment:

- `EXA_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `DEEPSEEK_API_KEY`, `TINYFISH_API_KEY`, `KAGI_API_KEY`; Firecrawl via `FIRECRAWL_API_KEY` and/or `FIRECRAWL_BASE_URL`/`FIRECRAWL_API_URL`; SearXNG via `SEARXNG_ENDPOINT` plus optional `SEARXNG_BASIC_USERNAME`/`SEARXNG_BASIC_PASSWORD` or `SEARXNG_TOKEN`; optional `JINA_API_KEY`.
- Selection order is exa → tavily → brave → deepseek → tinyfish → kagi → firecrawl → searxng → jina. Jina is selected without requiring a key when nothing else is configured (selection logic only; vendor availability not live-tested). DeepSeek queries truncate to 128 characters.

See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Stateless: keeps no state of its own. HTTP timeout is 30s; transport faults produce one bounded failure sentence while raw details stay in the log.
