# Capabilities quality audit

Scope: every module and packaged data file under `src/voidcode/capabilities/`.
Review correctness, ingress validation, cancellation/cleanup, output bounds, unused
fields, duplicated code and unnecessary mechanisms. Prefer removal and existing
stdlib/dependencies; do not add compatibility paths. A passing suite alone does
not establish source review.

## Inventory

| File | Current audit status |
| --- | --- |
| `__init__.py` | Pending full source review |
| `exec/__init__.py` | Pending full source review |
| `exec/args.py` | Reviewed source/consumers: three small nonempty field validators used by strict exec argument models |
| `exec/background_process.py` | Pending full source review |
| `exec/shell_exec.py` | Pending full source review |
| `files/__init__.py` | Pending full source review |
| `files/apply_patch.py` | Partial: read guards and fuzzy repair hints removed; final mutation/ingress review pending |
| `files/apply_workspace_edit.py` | Pending full source review |
| `files/edit_tool.py` | Partial: read guards and fuzzy repair hints removed; final mutation/ingress review pending |
| `files/errors.py` | Reviewed: retain only classified diagnostic exception and raising helper |
| `files/gitignore.py` | Reviewed: root-only pathspec matching; removed unused pattern snapshot/property; syntax/order/escape tests |
| `files/glob_tool.py` | Partial: strict search ingress and error-visible traversal reviewed; output bounds pending |
| `files/grep_tool.py` | Partial: strict search ingress/default path reviewed; traversal completeness pending |
| `files/read_formats.py` | Pending full source review |
| `files/read_tool.py` | Pending full source review |
| `files/write_tool.py` | Reviewed: hash/containment/symlink/atomic replace/cancellation; fail visibly on mode errors and always clean temporary files |
| `frontmatter.py` | Reviewed source/consumers: safe YAML, duplicate/string-key validation, framing/body/bounds; shared parser tests |
| `providers/__init__.py` | Pending full source review |
| `providers/dispatch.py` | Pending full source review |
| `providers/native/__init__.py` | Pending full source review |
| `providers/native/_resources.py` | Reviewed: contained package paths and JSON loading; removed unused installed-data locator/text wrapper |
| `providers/native/_result_sanitize.py` | Pending full source review |
| `providers/native/_wire_common.py` | Reviewed: usage counts, tool IDs, request headers, bounded queue/stream timeout/error cleanup, owned-resource initialization |
| `providers/native/anthropic_native.py` | Pending full source review |
| `providers/native/auth.py` | Pending full source review |
| `providers/native/claude_tokenizer/__init__.py` | Pending full source review |
| `providers/native/claude_tokenizer/constants.py` | Pending full source review |
| `providers/native/claude_tokenizer/engine.py` | Pending full source review |
| `providers/native/claude_tokenizer/normalize.py` | Pending full source review |
| `providers/native/claude_tokenizer/notation.py` | Pending full source review |
| `providers/native/config.py` | Pending full source review |
| `providers/native/errors.py` | Reviewed: classified errors, SDK text extraction and structured retry hints; removed prose scheduling and duplicate conversion functions |
| `providers/native/google_native.py` | Pending full source review |
| `providers/native/model_catalog.py` | Reviewed static catalog fields/lookups and consumers; removed entire unreferenced remote discovery implementation |
| `providers/native/model_catalog_data.json` | Pending consumer/data-contract review |
| `providers/native/model_routing.py` | Reviewed: routed alias/catalog fallback, typed unsupported wires, serialized cache creation and teardown |
| `providers/native/naming.py` | Pending full source review |
| `providers/native/openai_native.py` | Pending full source review |
| `providers/native/opencode.py` | Pending full source review |
| `providers/native/opencode_go.py` | Pending full source review |
| `providers/native/openrouter.py` | Pending full source review |
| `providers/native/provider_config.py` | Reviewed: table-owned wire defaults, endpoint/gateway defaults and Copilot token resolution; removed dead projections |
| `providers/native/provider_table.json` | Reviewed with parser/consumer and packaged-table tests; source/provenance retained in JSON |
| `providers/native/provider_table.py` | Reviewed: typed rows, duplicate/unknown IDs and config/catalog consumers; removed unused notes and forwarding helper |
| `providers/native/reasoning_effort.py` | Pending full source review |
| `providers/native/thinking_rules.json` | Reviewed with parser/consumer and packaged-table tests; source/provenance retained in JSON |
| `providers/native/thinking_rules.py` | Reviewed: default/scoped overlays, shared budgets, effort maps and output caps; malformed budget tables rejected |
| `providers/native/tokenizer.py` | Pending full source review |
| `providers/native/tokenizer_data/Cl100kBase.utok1.bz2` | Pending consumer/data-contract review |
| `providers/native/tokenizer_data/DeepSeekV3.utok1.bz2` | Pending consumer/data-contract review |
| `providers/native/tokenizer_data/Glm5.utok1.bz2` | Pending consumer/data-contract review |
| `providers/native/tokenizer_data/KimiK2.utok1.bz2` | Pending consumer/data-contract review |
| `providers/native/tokenizer_data/O200kBase.utok1.bz2` | Pending consumer/data-contract review |
| `providers/native/tokenizer_data/Qwen3.utok1.bz2` | Pending consumer/data-contract review |
| `providers/native/tokenizer_data/__init__.py` | Pending consumer/data-contract review |
| `providers/native/tokenizer_data/ctok_v3.bin.bz2` | Pending consumer/data-contract review |
| `providers/native/tokenizer_data/ctok_v4_7.bin.bz2` | Pending consumer/data-contract review |
| `providers/native/tokenizer_data/manifest.json` | Pending consumer/data-contract review |
| `providers/native/tokenizer_routing.json` | Pending consumer/data-contract review |
| `providers/native/trace.py` | Pending full source review |
| `providers/panels.py` | Pending full source review |
| `providers/pricing.py` | Reviewed: price once at report time, preserve prepriced/unknown usage, event/result wrappers |

## Removed heuristic repair hints

Removed sliding-window similarity searches, arbitrary 0.58 scoring, candidate
sorting/previews/diffs, line-prefix guessing and a duplicated implementation in
`edit_tool.py`. Exact-match failures remain actionable with a short retry
instruction. Classified errors remain consumed by public facts and effectiveness
statistics, and live in `files/errors.py` rather than a repair framework.

Verification: 72 related file, AST and search tests passed; Ruff and source type
checks passed. The earlier mandatory-read removal independently passed the full
suite (1451 passed, 1 skipped) and added focused mutation regressions.

## Atomic write cleanup

Replaced the custom streaming hash loop with `hashlib.file_digest`. Removed
repeated temporary-file deletion in favor of one `finally`, including control
exceptions. Mode-setting failures now abort instead of silently replacing an
executable file with the wrong permissions. Cancellation text says the file has
not been committed, since parent-directory creation may already have occurred.
Regression cases inject chmod/replace failure and KeyboardInterrupt during fsync;
the original content/mode and temporary-file cleanup are checked.

## Search and provider resource simplification

Removed unused gitignore pattern storage and provider installed-data lookup.
JSON resources use one direct package-contained read. Grep directory traversal
and text-file I/O errors now become explicit classified failures; non-UTF-8 files
remain skipped. Removed generic no-match/refinement advice and its diagnostics
body field. Match counts and truncation bounds already carry the actual facts.
Glob traversal completeness and search output bounds still need review.

Verification: 43 search/gitignore/provider-data tests passed, including injected
directory-walk and file-read failures. Ruff, formatting and source type checks passed.

## Glob traversal and shared ingress

Replaced `Path.glob` (which suppresses scan errors internally) with `Path.walk`
and `Path.full_match`: both standard library, with explicit scan-error handling.
Ignored/hidden directories are pruned before descending, and selection is stable.
Patterns must be relative; the `path` argument chooses the search directory.
Unreadable/invalid UTF-8 `.gitignore` files now fail visibly when ignore handling
is requested. Root-only scope remains explicit. Reviewed the shared frontmatter
parser and its two agent/skill consumers, plus exec argument validator uses.

Verification: 77 search/gitignore/frontmatter/exec tests and 5 built-in/core
composition integration tests passed. Ruff, formatting and source type checks passed.

## Provider resource lifecycle

Source review found that registered providers attempted teardown, but routed and
native providers did not propagate it to owned SDK clients. Added close methods
through routed caches, native transport holders and OpenAI/Anthropic/Google
clients. Borrowed clients/transports are not closed. Cache cleanup clears owned
references before closing; ExitStack ensures every cached provider is attempted
even when one close raises. The plugin-level close uses the same stdlib behavior.
Removed unused metadata parameter/helper from catalog wire routing.

Seven lifecycle tests passed, including real OpenAI/Anthropic SDK pool closure,
borrowed-pool preservation, Google ownership and cleanup after a close failure.
First-use cache/client concurrency and full adapter source review remain pending.

Validation: all 85 existing and new provider tests passed (31.83s); Ruff,
formatting and source type checks passed.

## Provider first-use concurrency

Serialized first-use registered/routed provider caches, native transports and SDK
clients with stdlib locks. Request execution runs outside initialization locks.
The same locks protect detach-on-close; normal runtime draining remains responsible
for waiting for active requests before teardown. Google now reuses the shared
owned-resource holder instead of maintaining a duplicate one-slot class.
Eight concurrent workers exercise each of seven initialization layers, asserting
one retained resource and one close. Fourteen lifecycle tests pass. Full wire
adapter source/error/stream review is still pending.

Validation: 92 provider tests passed (33.19s); Ruff, formatting and source type
checks passed. Shared stream-helper review identified duplicate timeout cleanup
and numeric usage edge cases for the next batch.

## Shared stream and usage review

Removed timeout-branch cleanup duplicated by `finally`, and removed a completion
sentinel whose value was never inspected (the queue already tags completion).
The queue remains bounded and worker errors/control exceptions propagate.
Usage counts accept nonnegative integers and integral floats; booleans, negative,
fractional and non-finite values remain unknown without raising conversion errors.
Google uses this common parser and no longer invents zero cache reads when deriving
uncached usage. Unknown cache counts leave uncached usage unknown, matching pricing.
Seventeen focused count/stream tests passed, including timeout cleanup exactly once
and worker release. Reviewed shared model matching and its policy-table callers;
kept its small explicit star-only matcher because fnmatch adds different syntax.

Validation: 109 provider/usage/stream tests passed (32.31s). Ruff, formatting
and source type checks passed.

## Policy tables and pricing

Reviewed route selection, provider-table consumers, thinking default/scoped
application and pricing thresholds/unknown buckets. Removed runtime `source`
fields and provider `notes` that have no consumer, keeping citations in shipped
JSON. Removed the provider-table payload forwarding function and redundant checks
after thinking values have already been validated. Non-finite price rates and
multipliers are rejected; malformed budget-table containers fail rather than
silently becoming empty. Added threshold-boundary/unknown-count and malformed
policy ingress tests. Data review covers packaged contracts and consumers, not a
claim that external vendor pricing was refreshed.

Validation: 117 provider/usage/stream tests passed (32.29s), including table
thresholds and malformed-number/budget ingress. Ruff, formatting and source type
checks passed. Local-loopback tests ran with the required network permission.

## Tokenizer reachability and model catalog

Confirmed no runtime/test caller of `tokenizer.count_tokens` or the vendored
Claude counting engine. Their approximately 1600 lines and binary vocabularies
are candidates for removal; a user choice is pending because NOTICE explicitly
states exact counting as a project priority. Catalog generation still consumes
`tokenizer_routing.json`, so that pipeline is separate from runtime reachability.
No tokenizer implementation/data was deleted while awaiting the choice.

Static catalog loading accepted seven declared capability fields but omitted them
from constructed metadata. Added those fields; malformed provider/model rows now
fail instead of disappearing. Non-finite catalog rates remain unknown. Removed
the unused `ProviderModelCatalog` dataclass and completed missing remote metadata
projection fields. Google discovery no longer invents vision/JSON/status values.
Remote payload strictness and bounded network reads remain to review.

Google generation methods are also not treated as proof of tool support or as
a negative streaming claim. A listed stream method is affirmative evidence;
omission leaves support unknown.

Validation: 121 provider tests passed before the final Google inference tightening;
a follow-up directory test exposed the intentional empty-metadata omission, and
the corrected fixture with a real context limit passed all 16 directory tests.
The 21 other auth/catalog tests also passed in that follow-up run. Final Ruff,
formatting and source type checks passed.

## Remove unconnected remote model discovery

A repository-wide caller search found that `discover_available_models`, its
fetchers/plans/protocol parsers and results have no runtime, script or external
SDK registration consumer. The public provider/model panels only use static
catalog queries. Removed that entire internal subsystem rather than adding
response bounds/validation to unused code. Removed its unused endpoint projector,
listing support predicate and listing-only Anthropic config adapter/constants.
The previous Google discovery regression tested a removed private parser and was
removed with it. Existing static metadata regressions remain. This supersedes the
prior outstanding remote-discovery parser/bounds review: the implementation no
longer ships. Tokenizer deletion remains a separate pending user choice.

Validation: all 120 remaining provider/usage/stream tests passed (32.90s).
Ruff, formatting and source type checks passed. Static catalog and actual wire
behavior remain exercised; no tokenizer implementation was modified.

## Direct provider construction

Removed OpenAI/Google/endpoint pass-through classes and their unused metadata
configuration projections. Registry constructs the actual typed native providers.
Removed Copilot's multi-model routing/cache layer: its current implementation
serves only the provider-selected chat wire. One native provider now serves all
models, preserving token environment fallback and provider-owned endpoint.
Gateway default application uses dataclasses.replace rather than manually copying
selected endpoint fields (which dropped organization/project/cache configuration).
The four deleted adapter modules have no SDK/public plugin registration contract.

Validation: 120 provider tests passed (33.02s); the final gateway-default change
then passed 15 lifecycle tests, including real SDK organization/project preservation.
Ruff, formatting and source type checks passed.

## Error parsing and unused retry configuration

Removed natural-language retry timing guesses, date/timezone assumptions and unit
regex tables. Keep structured fields/headers and reject negative/non-finite waits.
Unified API/stream error parsing and conversion; Google streaming failures now
retain stream identity. No new retry library was introduced: source inspection
confirmed there is no provider retry executor in the current core or dispatch.
The prior assertion that core already owns an executing retry loop was incorrect.

Removed unused `transient_retry` configuration payload/domain/default classes,
merge/parse/serialize branches and orphan numeric validators. Strict ingress now
rejects this ineffective option rather than accepting a promise that never runs.
Updated configuration documentation. SDK error hints remain descriptive contract
fields, not an automatic replay mechanism. A future explicit retry feature may use
Tenacity, but adding it during cleanup would expand product behavior.

Validation: 131 provider tests passed (33.04s), followed by 11 focused error
tests including rejection of the ineffective transient_retry setting. Ruff,
formatting and source type checks passed.

## Provider configuration dead-path removal and repository checkpoint

Full suite at `7654b13`: 1527 passed, 1 skipped (361.55s), with a
CoreActivationDriver/store-closed thread warning in SDK facts teardown.
Removed unreferenced environment-only config construction, config merging,
serialization and fallback config parsing/serialization. Removed merge-only
explicitness flags and their post-init mutation, plus a duplicate auth type alias.
Active typed config ingress and auth resolution remain. Config module loses
approximately 490 lines; its remaining ingress helpers still need final review.

Validation after removal: 136 provider/built-in/SDK-fact tests passed (39.96s).
The same core shutdown warning reproduced; it is not hidden or counted as a
clean gate. It is a separate core teardown issue to resolve before final auditing.
Ruff, formatting and source type checks passed.

Public catalog follow-up: the existing generator reads a third-party models.dev
mirror and has no scheduled refresh workflow. User requested upstream model data
to reduce manual JSON maintenance. Investigated official models.dev JSON APIs
and OpenRouter model metadata; automated snapshot generation is the proposed next
step. No source/refresh behavior has been changed yet.

## Official model catalog synchronization

User selected models.dev as authoritative and authorized field adaptation.
Generator now reads `https://models.dev/api.json` directly, without zstd mirror
handling, tokenizer routing prerequisite, retag/pricing-only modes or hand-set
per-model default effort. Shares the actual canonical effort ladder with runtime.
Maps upstream structured-output capability; converts finite nonnegative per-million
prices once. Validates generated metadata with the runtime loader before writing.
Official snapshot has 1170 coding models across 35 supported providers. Added daily
workflow that tests and proposes the generated snapshot in one reusable PR branch.
No external PR was created during this session. Protocol quirks and context price
tier tables still need migration; this batch does not claim all JSON is upstream.

Validation: generated twice from the downloaded official response; generator fixture
checks new model propagation, capabilities, prices, unknowns and reproducibility.
133 generator/provider tests passed (33.56s); static checks passed.

## Upstream long-context pricing

Generated 150 model entries with context price tiers from the same official
models.dev snapshot as base rates. Threshold/rates use a small Pydantic dataclass
at ingress, including finite/nonnegative rate and positive integer checks.
The snapshot loader preserves typed tiers and emits JSON payloads through asdict.
Price calculation consumes metadata directly, with no provider/model matcher.
Removed `pricing_rules.py` and the hand-authored `pricing_rules.json`; calculation
now lives beside the report-time pricing wrapper. Missing tier buckets inherit
base rates; unknown required usage remains unknown. Threshold comparison is gt,
matching upstream context tier overrides; multiple levels are ordered by size.

The old real-vendor price constant test disagreed with official GPT-5.6 tier data.
Removed it in favor of fixed algorithm fixtures that test threshold boundaries and
multiple tiers, without requiring human changes whenever upstream pricing changes.
Generator fixture verifies original tier conversion and reproducible serialization.

Validation: 130 generator/provider/usage/stream tests passed (32.50s). Ruff,
formatting and source type checks passed. The daily snapshot workflow now also
validates generation of upstream context tiers via the same generator fixture.

## Upstream protocol metadata

Removed hand-authored `api_routes.json` and its loader/matcher module. Generator
uses official per-model provider.npm override, then provider npm/default wire;
there is no per-model-name rule table. Routed providers consume the generated
metadata and refuse an unimplemented wire. Removed unused prefix-strip machinery
(no shipped row enabled it); explicit model_map aliases remain the naming contract.
Regenerated the snapshot, where official protocol data differs from old OMP pins.
Generator tests cover override precedence and Responses protocol identity without
pretending that the Responses adapter is implemented. Protocol adapter host/URL
normalization and thinking parameters still need review.

Removed the unconsumed wire_source table/domain flag and its unreachable provider
override branch, plus the redundant model alias forwarding helper. The remaining
WireRouting method resolves only protocols; model identity is not rewritten there.
Validation: 130 provider tests passed (33.20s); 30 focused generator/data/lifecycle
tests passed after flag cleanup. Unsupported upstream protocols have an explicit
regression that forbids guessing the chat wire. Static checks passed.

## Thinking controls from upstream capabilities

Removed per-model name match rows from thinking_rules.json and deleted the shared
custom model matcher. The remaining 14 entries describe protocol body spellings,
output-cap fields and budget conversions only. Upstream reasoning_options provide
explicit none support/effort levels, and interleaved.field supplies reasoning field
names. Protocol identity selects Anthropic budgets or Google levels; advertised
effort support replaces provider binary-mode guesses. Removed unused effort-map
and requires-effort state. Dropped redundant default-only protocol rows.

Replaced custom ThinkingRuleRow parsers, literal validators, map parsers and
multi-pass overlays with one Pydantic-validated dataclass/type adapter. Positive
budget and strict boolean validation remain. Named budget-table expansion stays
small and explicit. Updated Grok off test: official data declares none, rather
than the old manual assumption that it must reason at low.

Anthropic effort-capable models now receive thinking=adaptive/output_config.effort;
explicit off receives thinking=disabled. This follows the official thinking docs
https://platform.claude.com/docs/en/build-with-claude/thinking and the installed
SDK parameter types. No live vendor credentials were used; tests verify actual
payload construction and local-loopback SDK requests. Kimi output-cap convention
remains a protocol quirk pending its final review.

Validation: 136 generator/provider/thinking/usage/stream tests passed (32.09s),
including Google unnamed-model level selection and Anthropic adaptive/off payloads.
Ruff, formatting and source type checks passed.
