# Capabilities quality audit

Scope: every module and packaged data file under `src/voidcode/capabilities/`.
Review correctness, ingress validation, cancellation/cleanup, output bounds, unused
fields, duplicated code and unnecessary mechanisms. Prefer removal and existing
stdlib/dependencies; do not add compatibility paths. A passing suite alone does
not establish source review.

## Inventory

| File | Current audit status |
| --- | --- |
| `__init__.py` | Reviewed: documentation-only shared capability boundary |
| `exec/__init__.py` | Reviewed: stateless factory registers shell/background tools and identity-preserving deactivation; no resources, imports of concrete peers or duplicated config |
| `exec/args.py` | Reviewed source/consumers: three small nonempty field validators used by strict exec argument models |
| `exec/background_process.py` | Reviewed: strict args/state, identity persistence, host-owned lifecycle, cancellation ingress/readiness, bounded log prefix/cursors, stdin counts and confirmed-stop projection |
| `exec/shell_exec.py` | Reviewed: strict ingress, governed dispatch, bounded previews/artifacts, progress decoding, exit/timeout/cancel/capture-limit projections; command-name interception removed |
| `files/__init__.py` | Reviewed: typed Tool wrapper preserves diagnostic failures; plugin tool factory |
| `files/_replace.py` | Reviewed: checked replacement and exclusive atomic creation; cleanup, permissions and race regressions; documented metadata/compare-window limits |
| `files/apply_patch.py` | Reviewed: marker-only grammar, exact staged preflight, hash/checked commits, cancellation and truthful partial operations; removed unified/Git execution, path decoding and format adapters |
| `files/apply_workspace_edit.py` | Reviewed: single strict ingress/version field, frozen arguments, checked replacement, shared diffs, LSP coordinates, optional-query failures, cancellation and truthful structured partial commits; bounded model summaries vs complete client diff bodies reconciled |
| `files/edit_tool.py` | Reviewed: strict generated ingress, exact replacement, checked commit, diagnostic/cancellation behavior, standard diff rendering/counts and marker delegation; short model summaries vs complete structured client diff bodies reconciled |
| `files/errors.py` | Reviewed: retain only classified diagnostic exception and raising helper |
| `files/gitignore.py` | Reviewed: root-only pathspec matching; removed unused pattern snapshot/property; syntax/order/escape tests |
| `files/glob_tool.py` | Reviewed: strict ingress, root-relative patterns, traversal/ignore rules, bounded path collection, ordering/error results and cooperative cancellation |
| `files/grep_tool.py` | Reviewed: strict ingress, literal/regex matching and filters, bounded line/context/result collection, immutable rendering, error handling and cooperative cancellation |
| `files/read_formats.py` | Removed: notebook/PDF/SQLite parsing belongs to CLI tools |
| `files/read_tool.py` | Reviewed simplified source: regular files and resource URIs; strict five-argument ingress, bounded UTF-8 pages, cancellation, image references, content observations and SDK history |
| `files/write_tool.py` | Reviewed: hash/containment/symlink/atomic replace/cancellation; fail visibly on mode errors and always clean temporary files |
| `workspace_queries.py` | Reviewed current workspace: strict requests, distinct outcomes, SHA-256/identity checks and required progressing cursors; shared capability contract; no concrete peer imports |
| `history_renderer.py` | Moved out of capabilities: SDK services now return rendered history from core; existing byte/projection fixes retained |
| `frontmatter.py` | Reviewed source/consumers: safe YAML, duplicate/string-key validation, framing/body/bounds; shared parser tests |
| `providers/__init__.py` | Reviewed: shared registration/facts/view wiring and ExitStack-owned provider teardown |
| `providers/dispatch.py` | Reviewed: strict registration ingress, explicit auth conflict handling, typed materialization/selection, locked lazy cache/teardown and usage wrappers |
| `providers/native/__init__.py` | Reviewed: documentation-only namespace, no side effects |
| `providers/native/_resources.py` | Reviewed: contained package paths and JSON loading; removed unused installed-data locator/text wrapper |
| `providers/native/_result_sanitize.py` | Removed: only consumer sat behind an unreachable metadata.data dict check; SDK content is already projected by core |
| `providers/native/_wire_common.py` | Reviewed: usage counts, tool IDs, request headers, bounded queue/stream timeout/error cleanup, owned-resource initialization |
| `providers/native/anthropic_native.py` | Reviewed: request/tool/cache assembly, SDK auth/stream ownership, signed/redacted replay, exact tool stream identity, explicit terminal handling, unknown-preserving usage and thinking budget controls; model-specific thinking constraints delegated visibly to API |
| `providers/native/claude_tokenizer/__init__.py` | Reviewed: family configuration, lazy load and count dispatch |
| `providers/native/claude_tokenizer/constants.py` | Reviewed: marker escaping and explicit measured Unicode tables |
| `providers/native/claude_tokenizer/engine.py` | Reviewed: count-only DP, byte floor, frame tail and bundled CTOK loading |
| `providers/native/claude_tokenizer/normalize.py` | Reviewed: normalization, classes, runs and boundary/case markers |
| `providers/native/claude_tokenizer/notation.py` | Removed: unused token rendering/parsing API |
| `providers/native/config.py` | Reviewed: strict ingress, field/table wiring, typed converters, env resolution, custom names, credential exclusivity and representations; removed dead framework/default paths |
| `providers/native/errors.py` | Reviewed: classified errors, SDK text extraction and structured retry hints; removed prose scheduling and duplicate conversion functions |
| `providers/native/google_native.py` | Reviewed: SDK auth/owned-client lifecycle, timeouts/errors, tool capability and ID pairing, signed Part persistence/replay, thinking controls, inclusive/unknown-preserving usage and terminal stream semantics; documented Vertex OAuth/project and bounded teardown limitations |
| `providers/native/model_catalog.py` | Reviewed static catalog fields/lookups and consumers; strict Pydantic data ingress replaces permissive field conversion; removed entire unreferenced remote discovery implementation |
| `providers/native/model_catalog_data.json` | Reviewed packaged schema and generator/consumer contract: 35 providers/1170 rows, strict metadata, protocol/effort/limit/price consumers and scheduled snapshot PR; no live freshness claim |
| `providers/native/model_routing.py` | Reviewed: routed alias/catalog fallback, typed unsupported wires, serialized cache creation and teardown |
| `providers/native/naming.py` | Reviewed source/callers: provider normalization, preserved model casing, table-derived labels/reservations and unknown-id diagnostics; removed dead canonical-reference wrapper |
| `providers/native/openai_native.py` | Reviewed: SDK ownership/auth, schema/history/alias projection, reasoning controls, usage and strict response/stream identity/terminal handling; documented protocol limits |
| `providers/native/opencode.py` | Reviewed: current protocol-specific inference defaults, SDK-level Bearer auth, explicit host precedence, one-pass aliases and shared protocol clients |
| `providers/native/opencode_go.py` | Reviewed: shared Console wire factory, Go catalog identity, explicit host precedence, protocol admission and minimal feedback convention |
| `providers/native/openrouter.py` | Reviewed: small direct adapter using vendor endpoint normalization and native OpenAI transport |
| `providers/native/provider_config.py` | Reviewed: table-owned wire defaults, endpoint/gateway defaults and Copilot token resolution; removed dead projections |
| `providers/native/provider_table.json` | Reviewed with parser/consumer and packaged-table tests; source/provenance retained in JSON |
| `providers/native/provider_table.py` | Reviewed: typed rows, duplicate/unknown IDs and config/catalog consumers; removed unused notes and forwarding helper |
| `providers/native/reasoning_effort.py` | Reviewed: canonical vocabulary, unknown-preserving clamping, explicit-off and binary/protocol mapping; removed rank maps/aliases/runtime type imports |
| `providers/native/thinking_rules.json` | Reviewed with parser/consumer and packaged-table tests; source/provenance retained in JSON |
| `providers/native/thinking_rules.py` | Reviewed: model-name overlays/effort maps removed; upstream capabilities select validated protocol defaults and named budgets; Kimi cap is an explicit local compatibility convention, not a universal vendor requirement |
| `providers/native/tokenizer.py` | Reviewed: offline encoding dispatch, lazy loading, normalization and fallback; no runtime consumer |
| `providers/native/tokenizer_data/Cl100kBase.utok1.bz2` | Reviewed: bundled blob/manifest contract and actual encoding consumers; no live oracle claim |
| `providers/native/tokenizer_data/DeepSeekV3.utok1.bz2` | Reviewed: bundled blob/manifest contract and actual encoding consumers; no live oracle claim |
| `providers/native/tokenizer_data/Glm5.utok1.bz2` | Reviewed: bundled blob/manifest contract and actual encoding consumers; no live oracle claim |
| `providers/native/tokenizer_data/KimiK2.utok1.bz2` | Reviewed: bundled blob/manifest contract and actual encoding consumers; no live oracle claim |
| `providers/native/tokenizer_data/O200kBase.utok1.bz2` | Reviewed: bundled blob/manifest contract and actual encoding consumers; no live oracle claim |
| `providers/native/tokenizer_data/Qwen3.utok1.bz2` | Reviewed: bundled blob/manifest contract and actual encoding consumers; no live oracle claim |
| `providers/native/tokenizer_data/__init__.py` | Reviewed: bundled blob/manifest contract and actual encoding consumers; no live oracle claim |
| `providers/native/tokenizer_data/ctok_v3.bin.bz2` | Reviewed: bundled blob/manifest contract and actual encoding consumers; no live oracle claim |
| `providers/native/tokenizer_data/ctok_v4_7.bin.bz2` | Reviewed: bundled blob/manifest contract and actual encoding consumers; no live oracle claim |
| `providers/native/tokenizer_data/manifest.json` | Reviewed: bundled blob/manifest contract and actual encoding consumers; no live oracle claim |
| `providers/native/tokenizer_routing.json` | Removed: no reader; generator no longer depends on local model-cache database |
| `providers/native/trace.py` | Reviewed: opt-in, whole-record preview budget, JSON encoder, private append and isolated warning-only failures; documented per-file growth/trusted-parent limits |
| `providers/panels.py` | Reviewed: names/catalog-only configuration facts, typed paged transcript dispatches, read-only commands/views; removed guessed credential status |
| `providers/pricing.py` | Reviewed: price once at report time, preserve prepriced/unknown usage, event/result wrappers |

## Changes retained

- File mutation uses one checked replacement path. Removed heuristic repair
  scoring, duplicate diff implementations, mandatory patch hashes, unified-diff
  adapters and Git subprocess dependence. Marker patches retain exact preflight,
  cancellation and truthful partial-commit results.
- Strict ingress models are authoritative; removed hand-built schema mirrors,
  permissive conversions and unused configuration/framework fields.
- File reads preserve content hashes, bounded pages and long-line recovery.
  Removed notebook/PDF/SQLite/archives, directory trees, format selectors and an
  unused persisted-body decoder. Artifact pages preserve incomplete UTF-8 and
  budget replacement-character expansion (`65da3ab`). Image artifacts reuse
  their existing reference and inline images use the already-read first page.
- Resource schemes are `artifact://`, `tool://`, `rule://` and `transcript://`.
  Producers, readers, scoped queries, history links and documentation share the
  new spelling; no old aliases remain. Core path canonicalization preserves
  resource URIs generically instead of hardcoding a product scheme.
- Execution delegates process ownership to host services. Removed shell
  command-name interception and unused adapters; bounded progress decoding,
  readiness detection, log cursors and confirmed-stop projections were corrected.
- Provider clients have explicit resource ownership and serialized lazy creation.
  Routed clients are shared by protocol, aliases resolve once, explicit endpoints
  stay authoritative, and gateway Bearer authentication is set in SDK clients.
  OpenCode protocol defaults were reconciled in `8703900`.
- Provider projections preserve unknown usage, signed reasoning replay, original
  tool identities and terminal stream failures. Malformed IDs/indexes fail
  visibly. Removed unreachable result sanitization, dead authorization/default
  paths and unconnected remote model discovery.
- Catalog metadata comes from the generated models.dev snapshot with scheduled
  update PRs and strict typed loading. Removed unused tokenizer model-routing
  data and its local-database generator dependency. Offline tokenizers retain
  count-only APIs; discarded reconstruction APIs and old Unicode shims were removed.
- Optional workspace queries distinguish unavailable implementation, absent
  resource, malformed answer and handler failure. Answer identity, hashes and
  required progressing cursors are checked (`9f87998`). History rendering now
  belongs to core and is consumed through bounded SDK history services.

Historical implementation notes and individual probe results are available in
this file's Git history; they are not a second current-state specification.

## Verification

No test cases were added for this audit. Existing cases were updated when their
contracts or paths changed, and targeted manual probes covered new findings.

Current verification:

- Whole-tree Ruff and source ty pass.
- Frontend lint, typecheck, 16 tests and generated API consistency pass.
- Manual invalid-byte and mixed Chinese/emoji artifact pagination stayed below
  50 KiB and reconstructed the complete replacement-decoded input.
- The complete pre-simplification Python suite passed: 1,632 passed, one skipped
  in 380.63 seconds. This is baseline evidence; verification of format removal,
  directory removal and URI changes has passed 337 relevant existing cases.
  This includes loopback integration for optional queries, transcript and public
  read facts. Directory/legacy-URI rejection, the five-argument schema, plain-text
  notebook reads and generic core URI preservation also passed manual probes.
  Final full-suite verification of this new source is pending.

All 59 current capability files are listed, including packaged tokenizer data.
A filesystem comparison found no missing inventory entries; import inspection
found no capability imports of core, concrete plugins or outer product adapters.
Changes since the last source-review checkpoint were reconciled: query contracts
moved into capabilities, history rendering moved into core services, and obsolete
parallel-tool flags were removed.

The earlier directory output-budget repair (`853de21`) is superseded by deleting
directory reads at the user's request. Format-only tests were deleted with the
feature and existing consumer fixtures were updated; no test cases were added.

## Explicit boundaries

- Plugins are trusted application code. Optional query contracts validate their
  data; they do not sandbox a handler or impose deadlines on arbitrary Python.
- Filesystem checked replacement has an unavoidable compare/replace race and
  does not promise preservation of every filesystem metadata attribute. External
  mutation during observation is not a transactional filesystem snapshot.
- Cancellation is cooperative. Blocking vendor SDK/socket operations may run
  until the underlying operation returns or its configured timeout expires.
- Provider fixture tests establish local wire behavior, not live vendor service
  compatibility. Unknown usage remains unknown. The Chat Completions adapter
  does not implement Responses or arbitrary attachment protocols. Google OAuth
  uses supplied Vertex credentials/project; interactive login/refresh is outside
  this adapter.
- The packaged catalog is a generated snapshot, not runtime discovery. New
  vendor protocols still require explicit adapter support. Kimi output caps are
  a retained local convention using catalog output limits or the stated fallback.
- Offline tokenizer assets are manifest-checked local data, not a claim of
  equivalence to a changing remote billing oracle. Trace records are bounded
  individually; trace-file retention is not automatic.
- Workspace ignore matching is root-based. Search results are bounded
  observations; narrower searches are the recovery path for omissions.
