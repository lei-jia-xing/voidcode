# web

Web search and page fetch over HTTP(S).

## Surface

- Tool `web_search` with `query` (non-empty) and optional `numResults` (1–20, default 8). Effects `READ` + `NETWORK`.
- Returns ranked results (title, URL, snippet) naming the backend in `Search source: <id>`. A selected-backend failure is returned directly as a tool error, not retried against another backend.
- Tool `web_fetch` with `url` (non-empty http(s), no credentials). Effects `READ` + `NETWORK`, never replayed. Returns a concise summary with `web://` snapshot and canonical `artifact://` identities; full rendered bytes remain in the immutable snapshot.
- Resource `web://<sha256>`: already-fetched rendered snapshots owned by this plugin; page them with `read` like any stored artifact.

## Configuration

Takes no plugin config; `create_plugin` ignores it. Credentials and endpoint overrides come from the process environment:

- `EXA_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `DEEPSEEK_API_KEY`, `TINYFISH_API_KEY`, `KAGI_API_KEY`; Firecrawl via `FIRECRAWL_API_KEY` and/or `FIRECRAWL_BASE_URL`/`FIRECRAWL_API_URL`; SearXNG via `SEARXNG_ENDPOINT` plus optional `SEARXNG_BASIC_USERNAME`/`SEARXNG_BASIC_PASSWORD` or `SEARXNG_TOKEN`; optional `JINA_API_KEY`.
- Selection order is exa → tavily → brave → deepseek → tinyfish → kagi → firecrawl → searxng → jina. Jina is selected without requiring a key when nothing else is configured (selection logic only; vendor availability not live-tested). DeepSeek queries truncate to 128 characters.

See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Stateless: keeps no state of its own.
- Search: HTTP timeout is 30s; transport faults produce one bounded failure sentence while raw details stay in the log.
- Fetch is static only: no JS, no browser, no retries. HTML (and XHTML) converts to Markdown in one parse (scripts dropped, relative links resolved against the final URL); declared charsets decode strictly — unknown names, non-text codecs, and undecodable bytes fail explicitly without leaking body bytes; binary types fail without decoding.
- Fetch bounds: at most 5 redirects (every target re-validated as credentialless http(s)), 30s per-operation timeout plus an advisory 30s total budget checked between chunks/redirects, 5 MiB decompressed body cap (Content-Length never trusted). The full rendered bytes always spill to an artifact; the summary is not a content preview and never implies the snapshot is complete.
- Fetch output carries recovery identifiers: the `web://` snapshot URI and canonical `artifact://` reference. The structured body carries identity only (requested/final URL, content type, artifact reference), never a second full-content copy; Core may page the explicit textual artifact reference (including supported JSON/XML types) into the model allocation. `web://` proves bytes, not provenance: same bytes share a digest, and disabling `web` only removes the alias — the canonical artifact read still works.
- Trust: fetched bytes are untrusted data, never instructions, never executed. Arbitrary URLs are fetched as-is, including private-network addresses: there is no SSRF sandbox and NETWORK is not approval-gated, so treat fetch targets like shell egress.
- Cancellation is honored between chunks and redirects.
