# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).


## [Unreleased]



### Added

- **provider:** count tokens with oh-my-pi's real tokenizers (O200kBase, Cl100kBase, Glm5, KimiK2, Qwen3, DeepSeekV3), selected per model from the catalog's new `tokenizer` field; models without one keep the `(utf8_bytes + 3) >> 2` fallback, and the four Claude encodings stay on it because their vocabulary container is not portable. Vocabularies ship bz2-compressed (3.8 MB) and load lazily; construction is fully offline.

- **runtime:** add keep-alive subagent contract, storage, and steer surface

- **tools:** add background task steer tool

- **tools:** add invocation-level outputSchema validation to task delegation

- **runtime:** resolve child transcripts via voidcode://transcript/<id>

- **runtime:** resolve tool-output artifacts via voidcode://artifact/<id>

- **runtime:** add essential/discoverable tool split with on-demand dispatch

- **runtime:** add incremental event persistence primitives

- **runtime:** resume interrupted sessions and seal terminal state

- **runtime:** add tool effectiveness stats and context projection strategies

- **provider:** source model metadata from generated catalog with reasoning-effort support

- **tools:** restore background process tools with stdin interaction

- **events:** register parallel task group completion

- **runtime:** harden harness runtime adoption (#487)

- **runtime:** finalize harness policy safeguards (#488)

- **cli:** expose the resolved provider endpoint (`endpoint.base_url`, `endpoint.source`, `endpoint.discovery_base_url`) in `voidcode provider inspect`

- **frontend:** generate the runtime API types from the backend schema instead of hand-maintaining them: `scripts/generate_frontend_api_types.py` drives `GET /api/openapi.json` through the transport's ASGI interface (no server, no port) and writes `frontend/src/lib/runtime/generated/api.d.ts` atomically, deterministically (sha256-stable across runs and working directories) and with the pinned `openapi-typescript`; 93 schemas / 38 paths / 40 operations, the hand-written duplicates are removed and what remains are bindings plus four documented narrowings (`EventEnvelope` receive stamp, `ReviewChangedFile` enum, `RuntimeRequest` / `QuestionAnswer` / `ApprovalDecision` request bodies, `RuntimeStreamChunk`); the generated `.d.ts` is compile-time only with zero bundle cost, and the drift gate runs in `mise run check`, CI and a `frontend-api-types` pre-commit hook (~5.5 s)

- **provider:** add 17 W6 provider ids our existing wires can serve, each enabled only after its own listing route answered an unauthenticated probe: `aiand`, `alibaba-token-plan`, `baseten`, `cline-pass`, `coreweave` (upstream key `wandb`), `gmi-cloud` (`gmicloud`), `huggingface`, `kilo`, `novita` (`novita-ai`), `nvidia`, `venice`, `wafer-serverless` (`wafer.ai`), `xiaomi`, `xiaomi-token-plan-ams` / `-cn` / `-sgp`, `zenmux` — one `provider_table.json` row plus its `providers.<id>` payload field each, so the registry adapter, label, schema key, credential environment variable and catalog keys all derive from the table (no per-provider module). Enablement evidence is per row: an unauthenticated `GET <base_url>/models` returned `200` + JSON listing (aiand, cline-pass, huggingface, kilo, novita, nvidia, venice, wafer-serverless, zenmux) or `401`/`403` with the API's own credential error (alibaba-token-plan, baseten, gmi-cloud, xiaomi, xiaomi-token-plan-*, coreweave's geo 403), recorded verbatim in that row's `notes`; the shipped catalog grew 432 → 1322 models across 35 providers, regenerating deterministically (identical md5 across two runs). Candidates that did not earn a row this round stay in `.omo/plans/w6-additions-draft.json` with the probe result that held them back (see `provider/README.md` → 「模型发现策略」)



### Changed


- **provider:** consume the shipped catalog's limits and rates instead of treating them as display data: compaction is now sized by the model's `max_input_tokens` (derived as `context_window - max_output_tokens` when upstream carries no `limit.input`, else the `context_window`) through `stream_prep._context_budget_for` → `prepare_provider_context(context_window=...)`, while an explicit caller `context_window` / `threshold_*` / `reserve_tokens` still wins and a model the catalog does not describe leaves compaction unsized; a new pure `provider/pricing_rules.py::usage_cost_usd` prices each turn from the catalog's flat `cost_per_*` rates plus the optional long-context tier of the checked-in `provider/pricing_rules.json` (OMP's `long-context-cost` axis only — the flat card is replaced when the prompt's `uncached + cache_read + cache_write` input strictly exceeds the row's `threshold`, equality counting only for an `inclusive` row, and xai rows express their tier as a ×2 `multiplier` over the model's own base rates), and the resulting USD rides the existing `provider_usage` record as `latest.cost_usd` plus an accumulating `cumulative.cost_usd`, computed once from the turn's own usage and never repriced; the mirror's `context_over_200k` / `tiers[]` are deliberately never read and only OMP-attested rows exist (regression-guarded by an oracle test), and no vision gate was added because no image ever reaches a provider request — the read tool returns attachments as tool-result data and provider segments are text-only

- **provider:** make request construction and thinking semantics data-driven: the checked-in `provider/thinking_rules.json` (read only by `provider/thinking_rules.py`, `thinking_rule_for(provider_id, model_id)`) picks one row per provider plus model matcher (`exact` / `prefix` / `substring` / `token` / `glob`, the shared `model_match.py` matcher; the provider's default row first, then the first scoped match) and carries the row's `mode` (`effort` / `binary` / `budget` / `google-level` — the four knobs the three wires can actually emit), `budgets` (a named top-level `budget_tables` entry — `anthropic-thinking`, `google-thinking` — or an inline per-effort map), `disable_mode` (one of the five spellings our providers can select: `lowest-effort` / `none-effort` / `openrouter-enabled-false` / `zai-thinking-disabled` / `qwen-enable-thinking-false`), `effort_map`, `max_tokens_field` and `requires_effort`, so no adapter hardcodes a mode, a budget, a disable spelling or an effort map; the reasoning ladder is OMP's six members (`minimal` / `low` / `medium` / `high` / `xhigh` / `max`) and `off` is no longer a ladder member — it is the "reasoning disabled" request state (what OMP's `undefined` means), still accepted from CLI/config/frontend and resolved through the row's `disable_mode`, with a `requires_effort` row degrading it to the model's lowest supported level instead; thinking budgets now come from OMP's tables, which moves the numbers (`anthropic-thinking` `low` 2048 → 4096, `medium` 4096 → 8192, `high` 8192 → 16384; `google-thinking` `low` 2048 → 4096, `medium` 4096 → 8192, `high` 8192 → 16384, `xhigh` 8192 → 24575, `max` 8192 → 32768); every wire now sends its output cap from the catalog's `max_output_tokens` under the field name the row's `max_tokens_field` gives (`max_tokens` where the row names it — `deepseek`, `fireworks`, `mistral`, `moonshot`, `zai`, `zhipuai`, `opencode-go` — else `max_completion_tokens`), and the Anthropic wire raises a cap too small for the thinking budget to `budget + 4000` (`ensureMaxTokensForThinking`); reasoning capability now comes only from the catalog's `supports_reasoning` / `supports_reasoning_effort`, so the provider-level allowlist/denylist and the `glm-5` / `glm-z1` prefix heuristic are deleted — a model the catalog does not describe is unknown and forwarded unverified rather than denied; a W3 review remediation then corrected five things against OMP's own predicates: the `max_tokens_field` rows now follow `useMaxTokens` exactly (`max_tokens` for `deepseek`, `fireworks`, `mistral`, `moonshot`, `zai`, `zhipuai` and `opencode-go`), no output cap is sent by default (the kimi family is the only always-send exception, `alwaysSendMaxTokens = facts.is("kimi")`), the Anthropic thinking buffer can only raise a cap up to the model's own maximum and never shrinks it (`64000` is the unknown-model fallback, not a ceiling), the `reasoning_content`-with-tool-calls rule is data (`reasoning_content_field` + `requires_reasoning_content_for_tool_calls`) instead of a provider-name heuristic, and the unreachable disable spellings (`omit`, `cline-enabled-false`, `venice-disable-thinking`, `qwen-template-false`, `chat-template-thinking-false`) and thinking modes (`anthropic-adaptive`, `anthropic-budget-effort`) were deleted rather than left as dead branches, with `qwen-enable-thinking-false` added and a mode/wire invariant test over the whole catalog

- **provider:** rename the built-in provider ids to their vendor names — `grok`→`xai`, `kimi`→`moonshot`, `opencode`→`opencode-zen`, `copilot`→`github-copilot`, `kimi-coding`→`kimi-code` — so the registry key, the `providers.<id>` config key, the `provider/model` prefix, the `<ID>_API_KEY` prefix and the model-catalog key all spell one id per vendor (labels `xAI`, `Moonshot`, `OpenCode Zen`, `GitHub Copilot` and `Kimi For Coding`); `moonshot` reads `MOONSHOT_API_KEY` and falls back to `KIMI_API_KEY`, `github-copilot` reads only `GITHUB_COPILOT_TOKEN`, `minimax` no longer merges `minimax-cn` — they are two providers with their own config block, credential variable and catalog entry (`minimax`, OpenAI wire at `https://api.minimax.io` with `MINIMAX_API_KEY`; `minimax-cn`, Anthropic wire at `https://api.minimaxi.com/anthropic` with `MINIMAX_CN_API_KEY`) — and the `qwen` catalog is now built from the `alibaba-cn` upstream source key instead of `alibaba-coding-plan`; the 21 built-in ids are `openai`, `anthropic`, `google`, `deepseek`, `xai`, `qwen`, `zai`, `zhipuai`, `moonshot`, `minimax`, `minimax-cn`, `opencode-zen`, `opencode-go`, `github-copilot`, `groq`, `together`, `fireworks`, `mistral`, `openrouter`, `endpoint` and `kimi-code`, so the old ids fail with the unknown-provider error that lists the canonical ids instead of resolving (breaking — schema v1, forward-only, no aliases)

- **provider:** resolve each model's wire from data instead of the OpenCode gateways' hardcoded model lists — the generated catalog now carries an `api` field on every entry, resolved at generation time in OMP's precedence (an `api_routes.json` pin, else the upstream `provider.npm` wire, else the provider table's wire), plus `display_name`, `modalities_output` and `max_input_tokens`; the checked-in `api_routes.json` mirrors OMP's `api-routes` (per provider, declaration-order first-match-wins, `exact` / `prefix` / `substring` / `token` / `glob` matchers with optional `strip_prefix`, falling back to the provider's own wire), and OpenCode Zen / OpenCode Go dispatch now reads the catalog row's `api`, else an `api_routes` pin, else the provider's own wire, so a model whose upstream wire VoidCode does not implement (the OpenAI Responses API) fails typed as `unsupported_feature` (non-retryable, fallback allowed) instead of degrading to chat-completions; `opencode-go/minimax-m3` now speaks chat-completions (was Anthropic), matching OMP `behavior.kdl:209`; the new `provider_table.json` field `wire_source` records whether a provider's row `api` drives dispatch (`model`, the two OpenCode gateways and every other provider) or whether the row keeps upstream truth this build does not serve while dispatch stays on the table wire (`provider`, currently `github-copilot` only — its copilot Anthropic route needs a credential envelope, `Authorization: Bearer`, Copilot identity headers and a negotiated integration id, none verifiable without a live Copilot token)

- **provider:** derive model discovery from the provider's own `base_url` and delete the user-facing `discovery_base_url` field from every provider config, the config schema and `provider inspect`; the listing URL is always `<resolved base_url>` plus the wire's own path, so the field was a tri-state smuggled into a URL (breaking config surface — schema v1, forward-only, no compat). "No listing" is now a computed policy rather than a magic empty string (`copilot`; Google without a credential), and listing headers key off the wire (`anthropic_messages_compatible`) instead of the provider name, so every Anthropic-wire vendor gets `anthropic-version` plus its own credential header (`x-api-key`, `kimi-coding`'s `Authorization: Bearer`, `minimax-cn`'s `X-Api-Key`). Listing is enabled for `kimi-coding`, `minimax-cn`, `minimax`, `fireworks` and `opencode-go`, whose routes were verified to exist

- **runtime:** name the concrete remediation for an unconfigured or under-credentialed provider in readiness, validation, and doctor output — the config file, the exact `providers.<name>.api_key` path (or `providers.custom.<name>.api_key` for a custom provider), and a runnable command — instead of placeholder text such as `configure a provider/model`; the exit codes (`11` for a blocked `run`, `12` for `doctor`) and the default execution engine are unchanged

- **runtime:** connect to MCP servers only when a run needs them: a run whose configured servers are all covered by the persisted discovered-tool catalog connects to nothing (measured run-start latency 1.53 s / 6.9 s → 0), while an uncovered or reconfigured server set still discovers once at run start and persists the surface, so the first turn sees the MCP tools; disabled configs never connect, and a failed server is never cached

- **cli:** split `cli/app.py` (124 KB) into one module per command plus shared plumbing, replace the monolithic smoke suite with a per-command contract suite, print the discarded-attempt notice in the CLI transcript, and derive the first-run provider remediation actions from the runtime's own guidance (breaking for importers of `voidcode.cli.app` internals)

- **runtime:** answer a question on a session with nothing pending as `409` with `code=no_pending_question` instead of an indistinguishable `404`, run the served process with uvicorn lifecycle handling enabled so shutdown releases the workspace coordinator, and delete the `voidcode.runtime.http` compatibility facade (breaking for importers of that module)

- **agent:** parse custom agent manifests, slash commands, and skill manifests as real YAML through one shared, safe frontmatter entry point (`voidcode/frontmatter.py`) instead of three hand-rolled YAML-subset parsers; duplicate keys, non-string keys, malformed YAML, and oversized frontmatter now fail fast for every domain, and a command that declares frontmatter without a template body is rejected (breaking for inputs that relied on simplified-parser semantics: an unquoted ` #` now starts a comment, `: ` and leading indicator characters must be quoted, and implicitly typed scalars such as `yes` or `2024-01-01` are no longer accepted where a string is required)

- **tools:** render `web_fetch` HTML responses as Markdown with markdownify instead of regex line heuristics, so heading levels, link targets, code fence languages, and GFM tables survive the conversion; `format=text` output is unchanged

- **runtime:** enable LSP and MCP tooling by default

- **tools:** rename write_file tool to write (breaking)

- **runtime:** replace WorkflowMode with resolved mode aggregation (breaking)

- **runtime:** remove continuation loop and start-work workflow (breaking)

- **runtime:** enforce strict persisted contracts and extract capability materialization boundaries (breaking)

- **runtime:** decompose SqliteSessionStore into domain mixins

- **runtime:** slice execute_graph_loop into collaborative methods

- **runtime:** make collaborator dependencies explicit

- **runtime:** converge child preset and terminal-set derivations to a single authority

- **runtime:** remove category routing in favor of subagent_type

- **command:** trim slash commands to init and plan
- **runtime:** remove LangGraph dependency and replace deterministic execution with the plain-Python graph loop; provider-backed execution remains runtime-owned
- **provider:** replace LiteLLM with official provider SDKs (openai, anthropic, google-genai); rename the `litellm` provider to `endpoint` and the `LITELLM_*` env vars to `ENDPOINT_API_KEY` / `ENDPOINT_BASE_URL` (breaking)
- **provider:** drop built-in default model maps in favor of provider model discovery (breaking)
- **provider:** resolve each provider's own default base URL instead of falling back to `api.openai.com` for every OpenAI-compatible vendor whose config block is absent; `copilot` now defaults to `https://api.individual.githubcopilot.com` so Copilot credentials never point at OpenAI, and a provider with neither a configured nor a default endpoint fails with the new `not_configured` error (non-retryable, fallback allowed) instead of borrowing another vendor's host (breaking)
- **provider:** disable model discovery for `google` `service_account` auth instead of issuing an unauthenticated probe
- **provider:** route `opencode-go` and `opencode` (Zen) per model instead of one wire per provider, mirroring OMP's per-model catalogs: `opencode-go/minimax-m3` and Zen's `claude-*` / `qwen3.5-plus` / `qwen3.6-plus` speak Anthropic Messages at the gateway host root, Zen's `gemini-*` models speak the Google generative-ai wire at the gateway endpoint, and models whose upstream wire is the OpenAI Responses API (`opencode-go/gpt-5.6-luna`, Zen `gpt-*` / `grok-*` / `muse-spark-*`) fail with `unsupported_feature` instead of silently downgrading to chat-completions
- **provider:** resolve `google` `service_account` auth from the configured `auth.service_account_json_path` as cloud-platform credentials on the Vertex AI surface (a missing or unreadable file is a non-retryable `missing_auth` error instead of a silent `GOOGLE_API_KEY` substitution), select Vertex for API-key-free `project`/`region` configs, and honour `providers.google.base_url` as a complete endpoint root that the SDK does not extend with its own version segment

- **provider:** give provider naming one semantic on every surface: the lowercase vendor id (`minimax`) is the machine identifier (registry key, `providers.<id>` config key, `provider/model` prefix, `<ID>_API_KEY` env prefix, catalog key, `/api/providers` `name`) and one shared label table (`MiniMax`) names it for humans in `/api/providers`, `provider inspect`, and `doctor`; provider input is trimmed and lowercased at every boundary, so `MiniMax/...`, `MINIMAX/...`, and ` minimax /...` resolve to the same provider with the same endpoint, and `providers.custom.<name>` keys are canonicalised the same way; an id that is neither a built-in nor a declared `providers.custom` provider now fails loudly with the canonical ids and the declaration path instead of silently reusing `providers.endpoint`'s config (breaking for configs that relied on an undeclared prefix — use the `endpoint` id or declare the provider under `providers.custom`); model ids stay verbatim on the wire while catalog, capability, and fallback-chain matching stay case-insensitive, and the web settings save path stores `providers.<id>` instead of colliding with built-in names under `providers.custom`

- **runtime:** declare a response model for every JSON route so `/api/openapi.json` describes the real bodies (70 models; the SSE frame payloads are typed and pinned by a contract test); the wire output is unchanged, validated against the declared models with no field loss, and this is the prerequisite for generating the frontend types instead of hand-maintaining them (`docs/contracts/client-api.md`, `docs/contracts/stream-transport.md`)

- **frontend:** move server data (providers, agents, skills, sessions, status, review, workspaces, tasks, notifications, settings) into the TanStack Query cache under workspace-scoped keys, leaving the store to hold client state and the streamed-run projection; a real `AbortSignal` replaces the request-id guards, a failed workspace switch can no longer mix workspaces, browser-measured `/api/*` requests dropped 48 → 31 with every payload fetched once, and the store shrank 2086 → 1311 lines

- **runtime:** define the configuration boundary once, in `runtime/config_models.py`, and generate the shipped schema from it: the loader's accepted/rejected behaviour is byte-identical to before (a 114-case HEAD-vs-now probe with 0 mismatches, and a 2092-row model-driven corpus that fails in BOTH directions — artifact stricter and artifact looser — with the remaining loader-only rules declared individually and their reasons), so what changes is the published JSON Schema artifact (12 new `$defs`, nullability widened to match the loader, corrected enums, expressible constraints now expressed) — breaking for a consumer of the old artifact; the parity corpus replaced per-row temporary workspaces and schema compilation with one reused workspace and one compiled validator, cutting the sweep from 441 s to 5.9 s (unit loop 461 s → 53 s, `mise run check` 462 s → 70 s), and the user-config surface carries no artifact, which is documented as a decision with its compensating tests

- **provider:** align the error, retry-hint and stream-timeout rules with OMP's: every non-408/429 4xx is now terminal at classification time (`retryable=False` regardless of which message marker matched, so a permanent `400 invalid_request_error` is no longer auto-retried, while fallback stays allowed) and 402 joins 429 as a usage/limit error (`isUsageLimitStatus`, `error/rate-limit.ts:321-323`) classified as `rate_limit` instead of `transient_failure` — OMP keeps usage-limit out of the provider retry lane and hands it to credential rotation, voidcode has no rotation so the existing `background_rate_limit_retry` lane owns it, which is why the kind, not a per-caller special case, moved; `retry_after` now reads `retry-after-ms` / `retry-after` / `x-ratelimit-reset-ms` / `x-ratelimit-reset` and keeps the longest value (`utils/retry-after.ts:31-43`, with a large `-reset` counter read as an absolute epoch, `utils/retry-after.ts:104-125`), competes header timing with the message text's own hints (`Please retry in 12s`, `reset after 1h2m3s`, `Resets in 2hr 15min`, `"retryDelay": "2500ms"`, `retry-after-ms=7200000`, `reset at <stamp>` / `将在 <stamp> 重置` — `utils/fetch-retry.ts:4-29,118-215`, longest wins, a zone-less `reset at` read as UTC and only used when no relative hint exists), keeps a parsed `0` as a retry-now signal, and still caps at voidcode's documented 3600 s; the stream now has OMP's second watchdog — a 300 s first-event deadline next to the per-chunk idle timeout, reported as its own `transient_failure` message and floored at `timeout_seconds` (`max(first_event, idle)`, `utils/idle-iterator.ts:56-62,79-91`) so a slower first token is never cut off by a shorter inter-chunk gap, with caller cancellation still winning the attribution when a wait expires (`cancelled`, no retry, no fallback); the behaviours deliberately not copied are recorded in `.omo/plans/error-retry-rules-draft-notes.md` (credential-rotation lanes, the Anthropic SDK's inner retry loop and jitter, OMP's `RateLimitReason` taxonomy and 30-minute quota backoff, per-provider reset timezones, in-band 200-with-error-body) the usage envelope stays voidcode's `ProviderStreamEvent`, and a usage/limit answer is now decided by the runtime instead of the parser: `rate_limit` carries no parse-time `retryable` verdict, `PROVIDER_TRANSIENT_RETRYABLE_KINDS` no longer contains it, and `decide_provider_error_policy` has its own branch, so a 429/402 can never be a `ProviderTransientRetryDecision` — an armed rate-limit lane (background tasks) defers the retry and honours `retry_after`, otherwise the turn falls back to the next provider; a non-positive `retry-after-ms` *header* is no longer a hint (OMP `parseRetryAfterMsHeader`) while the body's `retry-after-ms=0` still means retry-now, and the in-band 200-with-error-body case stays a deliberate non-copy (`provider/README.md` → 「错误边界」/「取消、超时与不完整 tool call」)

- **frontend:** render the session's provider cost (`$1.50 spent`) in the composer's existing context line, next to the token totals and cache-hit rate: `providerUsage.ts::providerCostUsd` reads `provider_usage.cumulative.cost_usd` (falling back to `latest`) from the session payload, and an unpriced model keeps reporting nothing rather than `$0.00`, so the CLI/JSON figure and the UI agree



### CI


- **release:** validate built artifacts against release tags and publish distributions to PyPI

### Build

- **deps:** upgrade the toolchain (`uv` 0.12.15, `bun` 1.4.2) and 24 Python / 19 frontend dependencies, including `openai` 2.54.0 → 3.14.1, whose HTTPX2 default moves the OpenAI transport from `httpx` to `httpx2`; `typescript` stays at 6.0.3 (typescript-eslint cannot run against TS 7) and `vitest` / `@vitest/coverage-v8` at 4.1.11 (the jest-dom matcher augmentation)

- **release:** add git-cliff changelog generation

### Fixed

- **provider:** default Anthropic-wire `cache_retention` to `short` (was `none`), matching omp upstream: every Anthropic Messages request now carries the 5-minute ephemeral breakpoint unless explicitly set to `none`, and `long` selects the 1-hour TTL; no `PI_CACHE_RETENTION` env var (config key is sufficient)
- **runtime:** recover a defaulted 16384-token compaction reserve that leaves no usable budget on small windows (≤ ~19k) with the 15% proportional reserve, matching omp `resolveBudgetReserveTokens` — a 16k window now derives threshold 13600 instead of collapsing to 1 — and cap the derived threshold at `cw - 1`; an explicit `reserve_tokens` (even 16384) is always honored
- **provider:** make the checked-in rule and catalog data fail loudly instead of silently doing nothing: a provider id in `api_routes.json` / `thinking_rules.json` / `pricing_rules.json` that matches no `provider_table.json` row now raises at import (`require_provider_id`), a duplicate table id raises, and a `model_catalog_data.json` entry carrying a key the loader does not read (`max_outputs` for `max_output_tokens`) raises instead of being dropped; the model-discovery listing URL rule is now one function (`model_catalog._models_url`) shared by all three wires and matches the URL every provider was probed at — a base that already carries its version mid-path gets the listing appended to itself (`https://api.deepinfra.com/v1/openai` → `.../v1/openai/models`, live `200`), where the old rule built `.../v1/openai/v1/models` (`404`); three `muse-spark-*` API routes that an earlier `muse-spark-` prefix row already decided with the same wire are deleted, the unreachable `daybreak-blue-latest` pricing tier is deleted, and the 15 routes plus 1 thinking row that only ever apply to discovered-but-unbundled model ids are marked `discovered-only fallback (0 shipped catalog rows)` in their own `source` field; `tests/unit/provider/test_catalog_budget_invariants.py` and `test_thinking_rule_invariants.py` now assert the non-empty precondition, so a wiped or truncated artifact fails instead of passing vacuously (`.omo/plans/listing-path-probe.json`, `.omo/plans/cross-artifact-consistency.md`)

- **runtime:** cancel the in-flight tool invocation and reap it with a bounded window when the runtime timeout wins, instead of abandoning the daemon thread with no signal — a cooperative tool now observes cancellation and stops before its late write, every timeout surface (`runtime.tool_timeout`, `runtime.tool_completed`, `runtime.failed`, and the dispatched `invoke_tool` result) carries `cancellation_signalled` / `execution_stopped` / `side_effect_state`, an unconfirmed stop is reported as `side_effect_state="unknown"` with an error saying the execution may still be running instead of a plain failure, and a late completion is logged for diagnosis but never committed as the tool result (`docs/contracts/agent-tool-calling.md` → 「取消与超时（execution lifecycle）」)

- **runtime:** give every background execution one ownership lease granted at worker dispatch and validated at the single storage write gateway (`_write_connect`) plus the two task-commit entry points, so a worker whose shutdown wait expired (or whose task was seized/superseded) can no longer write child-session events, seal a child session, change task state, or append a parent completion notification; the refusal is kept as a `late_writes` diagnostic, and `interrupted` stays resumable through a new execution that takes ownership (`docs/contracts/background-task-delegation.md` → 「执行所有权与 late write」)

- **runtime:** refuse a resume that would rewrite a session another live run still owns — the acceptance run lost six persisted events and left an orphan `runtime.tool_completed` — refresh the run's replayable checkpoint at its last safe boundary so a crash inside a tool call resumes without replaying or claiming the crashed call, and make the sequence bootstrap a single `INSERT OR IGNORE` so concurrent bootstraps can no longer raise `UNIQUE constraint failed`; same-session re-entry is refused or queued per the contract (`docs/contracts/execution-lifecycle.md`)

- **frontend:** render one tool row per call when the deterministic engine announces a call before the runtime names it (including an approval-denied call), accept a pushed live frame only for the session on screen, dedupe live-only bursts by payload instead of the shared cursor, and recover the boot selection when the replayed session is gone; a raw-HTML tool payload renders as text

- **runtime:** keep the retry/fallback after a provider attempt has already surfaced streamed output and mark the restart with `discarded_streamed_output: true`, so the TUI (and the web store) retract the abandoned attempt's live projection; nothing persisted is ever discarded, and the transcript keeps its single `graph.response_ready`

- **runtime:** treat an unrecognized or absent provider finish reason as a completed, stop-equivalent terminal state instead of a user-visible failure (`error` / `cancelled` still fail, and an empty stream still fails at the graph), record `finish_reason` / `finish_reason_reported` on `graph.response_ready` so a silently truncated turn stays diagnosable, and keep the provider's raw token in metadata

- **frontend:** stop re-rendering the whole app shell, sidebar and composer on every streamed chunk by subscribing to the store fields each of them reads and memoizing the components; a 506-frame stream went from ~505 re-renders per panel to 3-4

- **frontend:** frame SSE streams with `eventsource-parser` instead of the hand-rolled line splitter; the tolerant wire contract (multi-line `data:`, comments, CRLF frames, trailing payload on close) is unchanged

- **runtime:** exit background task shutdown busy loop

- **transport:** make session replay read-only

- **runtime:** seal completed child sessions

- **provider:** persist reasoning for non-streaming child runs

- **runtime:** harden session and background-task state management

- **mcp:** retry transient discovery connection drops

- **runtime:** re-dispatch stranded background tasks and terminalize queued orphans

- **tui:** render background task events in order
- **build:** include frontend sources in source distributions

- **runtime:** preserve full reasoning output (#489)

- **provider:** send the OpenCode gateway's per-conversation `x-opencode-session`/`x-opencode-client` request headers on every OpenCode Zen and OpenCode Go wire




## [0.1.0] - 2026-05-10



### Added


- add in-process runtime streaming transport (#17) (#28)

- add runtime permission engine (#29)

- add web runtime transport foundation (#31)

- replace mocked frontend session state with runtime data (#32)

- render runtime timeline and activity panels (#33)

- add real write approval slice (#36)

- add real shell_exec approval slice (#37)

- add real grep tool slice (#38)

- add HTTP approval resolution endpoint (#39)

- add runtime config surface (#40)

- add extension infrastructure foundation (#41)

- close the final CLI streaming blockers (#42)

- render frontend final output for read-only runs (#46)

- close hooks/config MVP semantics (#48)

- add chat-first Textual TUI client (#49)

- add tui minimal implement (#52)

- abut web to backend (#53)

- close MVP client loops (#54)

- add execution engine selection foundation (#63)

- add provider-model runtime abstraction (#64)

- add provider-backed single-agent execution engine (#65)

- implement runtime-managed skill execution semantics (#73)

- **tools:** expand tool registry, normalize tool paths, and make shell_exec cross-platform (#72)

- **runtime:** manage LSP servers inside the runtime (#74)

- **runtime:** land ACP as a runtime-managed control plane (#75)

- **tui:** align the TUI with runtime sessions and events (#77)

- **runtime:** add context window management for single-agent runs (#78)

- **runtime:** add provider fallback handling for single-agent runs (#79)

- **runtime:** make execution engine step budget configurable (#80)

- **runtime:** add persisted resume checkpoints for approval resume (#81)

- **hook:** add runtime-owned formatter hook presets (#89) (#92)

- **runtime:** harden provider config resolution and persistence (#90) (#93)

- **lsp:** add preset and workspace-root capability layer (#102)

- **runtime:** add developer diagnostics for lsp and fallback paths (#103)

- **runtime:** add runtime-managed MCP tool plumbing (#105)

- add ast-grep structural search and replace substrate (#123)

- **runtime:** add context window capacity metadata

- **runtime:** add persisted tui preference config layers

- **tui:** add persisted preference commands and pickers

- improve formatter presets and expand built-in catalog (#127)

- **web:** improve session usability in the app shell (#137)

- **edit:** align formatter-aware edit results (#135)

- **runtime:** inject applied skills into provider-backed execution (#145)

- **runtime:** add background task substrate (#143)

- **runtime:** reserve async lifecycle hook surfaces (#144)

- **doctor:** add runtime capability doctor for external tool readiness (#138)

- add runtime session results and notifications (#146)

- **lsp:** expand builtin preset catalog (#149)

- **runtime:** add parent-child session lineage (#148)

- **lsp:** derive workspace defaults for common projects (#156)

- **runtime:** apply minimal leader agent preset slice (#152) (#157)

- **agent:** add declaration layer for leader config (#159)

- **agent:** add multi-role declaration skeletons (#160)

- add session continuity memory slice (#162)

- minimal runtime skill execution model (#161)

- **runtime:** enforce executable agent preset boundary (#171)

- **runtime:** enforce agent tool boundaries (#172)

- **runtime:** execute lifecycle hook surfaces (#173)

- **runtime:** accept provider credentials from environment (#176)

- **runtime:** query background tasks by parent session (#181)

- **runtime:** emit background task waiting approval event (#182)

- **runtime:** enforce runtime tool timeouts (#180) (#183)

- **runtime:** enforce stable runtime request metadata schema (#186)

- **runtime:** inject agent-facing tool guidance via sidecar files and complete second-wave docs (#192)

- **runtime:** recover delegated leader task visibility (#193)

- **runtime:** emit MCP server failure events (#195)

- deliver chat-first web shell with runtime settings support (#198)

- **runtime:** align agent prompts and remove leader_mode (#199)

- **runtime:** add question flow and runtime-backed agent tools (#201)

- **runtime:** add tool execution start observability (#202)

- **runtime:** cut over to delegated execution architecture (#207)

- **provider:** harden provider resolution and discovery semantics (#216)

- **agent:** harden builtin preset prompt semantics (#218)

- **mcp:** harden runtime sessions on official SDK (#220)

- **skills:** harden local skill subsystem contracts (#222)

- **runtime:** harden runtime with extracted collaborators (#205) (#221)

- introduce modular command system (#228)

- **runtime:** add session debug snapshot surface (#230)

- ship workspace-scoped web MVP (#231)

- **web:** add voidcode web launcher with OpenCode-aligned contracts (#233)

- **provider:** add token metadata for context compaction (#247)

- **runtime:** add agents config map (#250)

- **runtime:** add agent refs and acp status (#251)

- **web:** reflect configured provider models (#252)

- **runtime:** add token-budget tool retention (#253)

- **runtime:** add context window policy config (#260)

- **agent:** add model-aware manifest metadata (#262)

- **provider:** add provider capability inspection (#263)

- **cli:** add schema-backed config workflow (#264)

- **acp:** add stdio runtime facade (#280)

- **agent:** productize top-level planning agent (#282)

- **cli:** polish CLI reference client UX (#283)

- surface provider readiness diagnostics (#281)

- **provider:** harden model metadata for routing (#291)

- **runtime:** add background task concurrency controls (#292)

- **runtime:** route models by agent category (#293)

- **runtime:** make skills catalog-first and model-loadable by default (#295)

- **runtime:** add delegated subagent execution baseline (#297)

- **cli:** polish delegated task operator UX (#303)

- surface MCP health and config visibility (#305)

- **web:** complete runtime integration parity (#307)

- add first-task readiness diagnostics (#306)

- **runtime:** add context-pressure event, config thresholds, and non-fatal hook surface (#308)

- **web:** improve runtime tool visibility and reasoning controls (#309)

- **runtime:** harden SQLite storage operations (#315)

- **runtime:** productize context memory compaction (#316)

- **runtime:** complete background task lifecycle semantics (#317)

- **runtime:** add conversation undo (#327)

- **runtime:** add reasoning effort config (#331)

- **runtime:** make todo state runtime-owned (#332)

- **runtime:** add active run interruption (#329)

- **web:** redesign tool activity UI (#319)

- **runtime:** add portable session bundles (#339)

- **runtime:** add provider context inspector (#340)

- **runtime:** add provider failure recovery checkpoints (#341)

- **runtime:** add reasoning parts and thinking controls (#343)

- rework tool compaction and OpenCode-Go feedback (#342)

- **runtime:** runtime-owned external directory permissions with tool integration Body (#333)

- **runtime:** simplify delegated category taxonomy (#351)

- **runtime:** add background task observability (#350)

- **runtime:** add provider context diagnostic policy (#364)

- **runtime:** add tmp-first tool output artifacts (#363)

- **runtime:** stream shell exec progress (#370)

- **hook:** add builtin hook preset catalog (#379)

- **runtime:** add production-ready model-assisted continuity distillation with deterministic fallback (#377)

- **runtime:** materialize hook preset guidance (#386)

- **runtime:** expose hook preset snapshots (#387)

- **agent:** harden builtin role boundaries (#389)

- **command:** add minimal builtin prompt commands (#391)

- **runtime:** define agent capability bindings (#400)

- **runtime:** add pattern permission rules (#404)

- **cli:** add pending question answers (#406)

- **runtime:** add local custom tools (#407)

- **agent:** support local custom agent manifests (#408)

- **runtime:** add workflow preset harness (#409)

- **frontend:** expose background task output (#410)

- **cli:** add human-readable run trace (#418)

- **web:** improve runtime token status UI (#419)

- **runtime:** productionize storage and delegated retry (#445)

- **runtime:** add workflow handoff and batched tool calls (#446)

- add runtime-owned continuation loops (#447)

- **runtime:** add intensive loop verification state (#449)

- **runtime:** add context continuity safeguards (#450)

- **runtime:** add structured hook diagnostics (#452)

- **command:** add init slash command (#454)

- **runtime:** add context transform hooks (#455)

- **runtime:** add context transform registry (#456)

- **runtime:** add scoped transform policy (#457)

- **runtime:** add request transform narrowing (#458)

- **runtime:** add transform ordering diagnostics (#459)

- **runtime:** add transform failure policy (#460)

- **runtime:** add typed extension observability (#463)

- **context:** add readme context and write guards (#464)

- **tools:** add tool governance workflow guards (#465)

- **harness:** lightweight agent harness upgrades — phase 1 (plan/act, lazy skills, disciplined todos) (#467)

- **runtime:** phase 2 prompt assembly and context tier productization (#468)

- **runtime:** compact recent-tier context under pressure (#469)


- **frontend:** improve runtime visibility and child session navigation (#474)

- **runtime:** add delegated idle reminders and process guardrails (#475)

- **runtime:** add workflow mode harness integration (#480)

- **runtime:** add workspace memory capability (#481)

- polish frontend runtime UX and delegated sessions (#485)



### CI


- add opencode-review

- add opencode-triage

- fix permission for review

- correctly set ai related ci

- add qwen3.6 to review

- change qwen 3.6 to gpt5.4

- open gpt review

- remove review ci

- keep release builds on supported Python (#190)

- cancel bot's permission to pr

- remove useless opencode review

- remove issue triage

- publish release builds to PyPI via trusted publishing



### Changed


- use lib to transfer hand-write html exact

- use lsprotocol to pydantic

- adopt rapidfuzz and unidiff for tool tooling (#109)

- **mcp:** stabilize runtime boundary and extract config/schema/types (#117)

- **skills:** extract capability layer from runtime (#128)

- **acp:** extract validated ACP contracts (#147)

- **lsp:** make builtin server names the primary config path

- consolidate boundary-layer parsing with Pydantic models (#203)

- **frontend:** align web shell controls with OpenCode-style hierarchy (#232)

- **runtime:** tighten runtime config surface (#294)

- **runtime:** enforce assembled context as sole provider boundary (#296)

- **runtime:** extract tool scoping policy (#378)

- **provider:** move config projection into adapters (#421)

- **runtime:** extract permission context resolver (#441)

- **runtime:** move background task lifecycle state (#448)

- **runtime:** extract pure helpers from service.py into 14 domain modules



### Documentation


- add some plan

- remove unexist doc

- add runtime contract documents

- add runtime config and transport contracts

- align truth-source references across project docs

- clarify MVP and frontend documentation

- align approval flow contract with runtime event envelope schema (#26)

- complete runtime config contract (#27)

- define TUI MVP interaction model (#30)

- remove unexist doc

- add MVP demo verification guide (#20) (#35)

- localize repository docs and templates in Chinese (#43)

- sync web transport state with issue #23 (#45)

- add post-MVP technical design

- claude code's advise for arch

- update doc to current state

- define retention and checkpoint invalidation semantics (#86)

- update todo

- define capability-layer ownership boundaries (#94)

- define modules reponsibility

- sync docs to code

- sync #82 completion status

- add runtime-owned scheduler design spec (#101)

- add memory reference

- sync MCP integration status and clarify LangGraph scope (#114)

- add plan for agent tool

- add mcp-related doc

- **runtime:** refresh roadmap and current state

- **mvp:** mark issue 84 follow-up complete

- remove useless doc

- add tui preferences design

- update roadmap

- agent related doc update

- add background task delegation contract

- sync repo state and roadmap references

- **runtime:** align hook and config contracts

- localize agent tooling adoption plan

- **runtime:** include execution engine in config examples

- add reasoning effort decision draft

- **runtime:** recommend builtin-name LSP server config

- **current-state:** align MVP status with current runtime

- **roadmap:** refresh active backlog references

- **architecture:** sync ACP and LSP maturity notes

- sync docs with code

- make tool calls agent-consumable (#168)

- contracts update

- align transport baseline and contract status

- add agent-facing tools guide (#177)

- AGENTS.md update

- transfer top docs into en_US

- tighten doc

- add CLI and Web failure recovery runbook (#229)

- **runtime:** define deterministic engine lifecycle (#259)

- align product status with current code

- mark prompt command MVP item complete

- **runtime:** plan service decomposition (#436)



### Fixed


- emit failed terminal stream chunk

- **runtime:** fall back when persisted checkpoints are unreadable (#85)

- **apply_patch:** normalize mode-only patches and improve git error handling (#104)

- **lsp:** address shutdown cleanup and UNC URI follow-ups (#106)

- add apply_patch fuzz-style tests and normalize diff headers (#125)

- **runtime:** make queued background task cancellation atomic

- **runtime:** recheck cancellation before background dispatch

- **lsp:** match canonical Dockerfile names

- **lsp:** guard workspace-scoped lifecycle reuse (#151)

- **runtime:** complete ACP managed lifecycle slice (#155)

- **runtime:** preserve resume fallback null content

- **provider:** inject runtime continuity summary into prompts (#189)

- ruff formatte

- **runtime:** harden hook execution semantics (#217)

- **graph:** harden provider graph terminal handling (#219)

- **lsp:** harden request handling and failure bounds (#223)

- **runtime:** enforce token retention bounds

- **web:** prevent launcher e2e browser popups (#261)

- stabilize provider-backed single-agent runs (#266)

- preserve tool output fidelity (#348)

- **provider:** keep redaction sentinels out of tool arguments (#362)

- **runtime:** return approval denials as tool feedback (#369)

- **runtime:** retry transient provider failures (#373)

- **runtime:** align failure signals and tool feedback (#376)

- **runtime:** respect gitignore in review tree (#388)

- **agent:** reduce leader QA false confidence (#392)

- **runtime:** avoid shell read probes as external writes (#393)

- **tools:** clarify tool argument validation feedback (#416)

- fix refresh replay and simplify runtime provider surfaces (#420)

- **runtime:** use production-grade timeout defaults (#422)

- **frontend:** improve runtime provider settings and subagent navigation (#423)

- **tools:** improve edit mismatch diagnostics (#434)

- fix web/provider/runtime approval flow regressions (#439)

- improve cross-platform compatibility (#440)

- improve default MCP and LSP agent ergonomics (#443)

- keep MCP disabled unless configured (#444)

- **runtime:** harden long-task stability (#451)

- **runtime:** update config schema url

- **cli:** align config schema url expectations

- **runtime:** close transform block bypass for off diagnostics (#462)

- **tools:** remove shell interactivity classifier (#466)

- **tools:** harden background process cleanup (#472)

- **runtime:** stabilize delegated task terminal truth (#473)

- auto-assign web launcher ports and backfill waiting-child reminders (#476)

- **build:** package web assets into Python release artifacts (#478)

- **provider:** add opencode zen integration and readiness checks (#477)

- **build:** honor verifier timeout for silent web launchers (#479)

- **runtime:** align server startup, state DB recovery, and harden git status snapshot decoding on Windows (#482)

- **runtime:** default external directory access to allow (#483)

- **hooks:** enable runtime hooks by default (#484)

- harden delegated child session frontend flow (#486)



### Testing


- add frontend component test harness (#34)

- group graph unit coverage

- group tool unit coverage

- group runtime unit coverage

- group interface unit coverage

- group package import checks

- group project metadata checks

- add unit path helper module

- mark unit test domain packages

- centralize CLI smoke subprocess paths

- remove graph unit sys.path setup

- remove project unit path boilerplate

- remove runtime import path boilerplate

- remove runtime config path boilerplate

- remove runtime service path boilerplate

- remove tool unit sys.path setup

- capture the remaining skill execution gap (#91)

- add apply_patch test

- **grep:** add Hypothesis fuzz coverage (#131)

- **multi_edit:** add Hypothesis fuzz coverage (#132)

- **runtime:** add fuzz coverage for runtime-backed agent tools (#214)

- parallelize python test tiers (#265)

- add marker-driven backend test lanes (#344)

- **runtime:** cover background task reconciliation idempotency (#349)

- **runtime:** add provider context parity coverage (#361)

- **agent:** add manifest boundary invariant coverage (#384)

- add issue #425 contract parity coverage (#437)

- **runtime:** cover session restart persistence (#438)



### chore


- pin python version to 3.14

- amend coding-standards & gitignore

- remove uneccessary file

- add agent.md

- add team config

- readme update

- untrack .coverage

- pin uv & bun version

- stop ai ci in a period

- stop ai review

- update ignore file

- disabled review ci

- do not publish to pypi

- downgrade python to 3.13 & docs update

- tighten langgraph pyright boundary

- add codeOWNERS to review

- remove test entry in pr template

- add codex config file to ignore

- ignore .voidcode.json

- add litterred

- add linter & formmatter for frontend

- remove pyright related

- **deps:** bump actions/github-script from 7 to 9 (#395)

- add codeowner

- remove dead code

- remove dangling list tool references

- prep voidcode 0.1.0 release (#453)

- change to alpha version



### config


- migrate from mypy to basedpyright
