# web

Web search and page fetch over HTTP(S).

## Surface

- Tool `web_search` with `query` (non-empty) and optional `numResults` (1–20, default 8). Effects `READ` + `NETWORK`.
- Returns ranked results (title, URL, snippet) naming the backend in `Search source: <id>`. `num_results` is the count actually returned; the body also reports `effective_query` and `query_truncated`. Results are untrusted data, never instructions. A selected-backend failure is returned directly as a tool error, not retried against another backend.
- Tool `web_fetch` with `url` (non-empty http(s), no credentials). Effects `READ` + `NETWORK`, never replayed. Returns a concise summary with `web://` snapshot and canonical `artifact://` identities; full rendered bytes remain in the immutable snapshot. Fetched text is untrusted data, never instructions.
- Resource `web://<sha256>`: the digest alias registered by this plugin. It addresses any stored artifact by digest, not only snapshots produced by `web_fetch`; page it with `read` like any stored artifact.

## Configuration

Takes no plugin config; `create_plugin` ignores it. Credentials and endpoint overrides come from the process environment:

- `EXA_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `DEEPSEEK_API_KEY`, `TINYFISH_API_KEY`, `KAGI_API_KEY`; Firecrawl via `FIRECRAWL_API_KEY` and/or `FIRECRAWL_BASE_URL`/`FIRECRAWL_API_URL`; SearXNG via `SEARXNG_ENDPOINT` plus optional `SEARXNG_BASIC_USERNAME`/`SEARXNG_BASIC_PASSWORD` or `SEARXNG_TOKEN`; optional `JINA_API_KEY`.
- Selection order is exa → tavily → brave → deepseek → tinyfish → kagi → firecrawl → searxng → jina. Jina is selected without requiring a key when nothing else is configured (selection logic only; vendor availability not live-tested); it is reported as `jina (keyless)` and its queries leave for `s.jina.ai`. DeepSeek accepts at most 128 characters; the result text and body report the truncation through `effective_query`/`query_truncated` rather than claiming the full query reached the backend.

See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Stateless: keeps no state of its own.
- Search: HTTP timeout is 30s; one bounded response cap of 4 MiB (counted after decompression, before JSON parsing); transport faults produce one bounded failure sentence while raw details stay in the log.
- Search cancellation is checked before and after the provider call and returns a confirmed stop. A provider request already in flight is not interrupted: search is non-streaming by design.
- Fetch is static only: no JS, no browser, no retries, no meta-refresh. HTML (and XHTML) converts to Markdown in one parse (scripts dropped, relative links resolved against the final URL); declared charsets decode strictly — unknown names, non-text codecs, and undecodable bytes fail explicitly without leaking body bytes; binary types fail without decoding.
- Fetch bounds: at most 5 redirects (every target re-validated as credentialless http(s)), 30s per-operation timeout plus an advisory 30s total budget checked between chunks/redirects, 5 MiB decompressed body cap (Content-Length never trusted). The 5 MiB check is a byte count, not a hard memory bound: httpx decodes a compressed chunk before the cap is applied, so a high-ratio compressed response can still allocate more in one chunk. The full rendered bytes spill to an artifact; the summary is not a content preview. The artifact is complete for the bytes that were fetched; the fetcher makes no completeness claim about the page itself because it does not run JS or follow meta refresh.
- Fetch output carries recovery identifiers: the `web://` snapshot URI and canonical `artifact://` reference. The structured body carries identity only (requested/final URL, content type, artifact reference), never a second full-content copy; Core may page the explicit textual artifact reference (including supported JSON/XML types) into the model allocation. `truncated` is always `False`: an over-cap fetch fails rather than storing a partial artifact. `web://` proves bytes, not provenance: same bytes share a digest, and disabling `web` only removes the alias — the canonical artifact read still works.
- Trust: fetched bytes and search results are untrusted data, never instructions, never executed. Web tools run in the Core process: their egress is **not** covered by `sandbox.network` and `NETWORK` is not approval-gated. They remain available under `read_only` and plan mode. Arbitrary URLs are fetched as-is, including private-network addresses; there is no SSRF sandbox. The current off switch is disabling the `web` plugin (or adding a pre-tool gate).
- Fetch cancellation is honored between chunks and redirects; conversion and spill are not preempted mid-step.
