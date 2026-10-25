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
| `exec/background_process.py` | Partial: strict persisted identity ingress, UTF-8/CRLF log pages and final readiness draining reviewed; typed state pipeline reviewed; scan ceiling/cursor and remaining lifecycle review pending |
| `exec/shell_exec.py` | Partial: typed SDK artifact references and consumers reviewed; interception policy, progress boundaries and remaining result semantics pending |
| `files/__init__.py` | Reviewed: typed Tool wrapper preserves diagnostic failures; shared core/plugin factory |
| `files/_replace.py` | Reviewed: checked replacement and exclusive atomic creation; cleanup, permissions and race regressions; documented metadata/compare-window limits |
| `files/apply_patch.py` | Reviewed: marker-only grammar, exact staged preflight, hash/checked commits, cancellation and truthful partial operations; removed unified/Git execution, path decoding and format adapters |
| `files/apply_workspace_edit.py` | Reviewed: single strict ingress/version field, frozen arguments, checked replacement, shared diffs, LSP coordinates, optional-query failures, cancellation and truthful structured partial commits; bounded model summaries vs complete client diff bodies reconciled |
| `files/edit_tool.py` | Reviewed: strict generated ingress, exact replacement, checked commit, diagnostic/cancellation behavior, standard diff rendering/counts and marker delegation; short model summaries vs complete structured client diff bodies reconciled |
| `files/errors.py` | Reviewed: retain only classified diagnostic exception and raising helper |
| `files/gitignore.py` | Reviewed: root-only pathspec matching; removed unused pattern snapshot/property; syntax/order/escape tests |
| `files/glob_tool.py` | Reviewed: strict ingress, root-relative patterns, traversal/ignore rules, bounded path collection, ordering/error results and cooperative cancellation |
| `files/grep_tool.py` | Reviewed: strict ingress, literal/regex matching and filters, bounded line/context/result collection, immutable rendering, error handling and cooperative cancellation |
| `files/read_formats.py` | Reviewed: bounded notebook/SQLite/PDF views, source snapshot semantics, pagination/recovery, signature-only image handling, read-only transactions and extractor cleanup |
| `files/read_tool.py` | Partial: strict ingress, PDF recovery and typed/error-visible bounded directory materialization reviewed; archive member identity/byte bounds and clipped-line recovery reviewed; native file stream/hash/binary bounds and read cancellation ingress reviewed; tool documentation and transcript query ingress reviewed; native clipped-line recovery reviewed; remaining artifact/transcript projections, archive metadata/modes and result metadata pending; directory cancellation reviewed |
| `files/write_tool.py` | Reviewed: hash/containment/symlink/atomic replace/cancellation; fail visibly on mode errors and always clean temporary files |
| `frontmatter.py` | Reviewed source/consumers: safe YAML, duplicate/string-key validation, framing/body/bounds; shared parser tests |
| `providers/__init__.py` | Reviewed: shared registration/facts/view wiring and ExitStack-owned provider teardown |
| `providers/dispatch.py` | Reviewed: strict registration ingress, explicit auth conflict handling, typed materialization/selection, locked lazy cache/teardown and usage wrappers |
| `providers/native/__init__.py` | Reviewed: documentation-only namespace, no side effects |
| `providers/native/_resources.py` | Reviewed: contained package paths and JSON loading; removed unused installed-data locator/text wrapper |
| `providers/native/_result_sanitize.py` | Removed: only consumer sat behind an unreachable metadata.data dict check; SDK content is already projected by core |
| `providers/native/_wire_common.py` | Reviewed: usage counts, tool IDs, request headers, bounded queue/stream timeout/error cleanup, owned-resource initialization |
| `providers/native/anthropic_native.py` | Partial: request assembly and tool/cache gating reviewed; removed unconsumed wire fingerprints and duplicate history conversion; stream/replay/usage/transport reconciliation pending |
| `providers/native/claude_tokenizer/__init__.py` | Pending full source review |
| `providers/native/claude_tokenizer/constants.py` | Pending full source review |
| `providers/native/claude_tokenizer/engine.py` | Pending full source review |
| `providers/native/claude_tokenizer/normalize.py` | Pending full source review |
| `providers/native/claude_tokenizer/notation.py` | Pending full source review |
| `providers/native/config.py` | Reviewed: strict ingress, field/table wiring, typed converters, env resolution, custom names, credential exclusivity and representations; removed dead framework/default paths |
| `providers/native/errors.py` | Reviewed: classified errors, SDK text extraction and structured retry hints; removed prose scheduling and duplicate conversion functions |
| `providers/native/google_native.py` | Reviewed: SDK auth/owned-client lifecycle, timeouts/errors, tool capability and ID pairing, signed Part persistence/replay, thinking controls, inclusive/unknown-preserving usage and terminal stream semantics; documented Vertex OAuth/project and bounded teardown limitations |
| `providers/native/model_catalog.py` | Reviewed static catalog fields/lookups and consumers; strict Pydantic data ingress replaces permissive field conversion; removed entire unreferenced remote discovery implementation |
| `providers/native/model_catalog_data.json` | Reviewed packaged schema and generator/consumer contract: 35 providers/1170 rows, strict metadata, protocol/effort/limit/price consumers and scheduled snapshot PR; no live freshness claim |
| `providers/native/model_routing.py` | Reviewed: routed alias/catalog fallback, typed unsupported wires, serialized cache creation and teardown |
| `providers/native/naming.py` | Reviewed source/callers: provider normalization, preserved model casing, table-derived labels/reservations and unknown-id diagnostics; removed dead canonical-reference wrapper |
| `providers/native/openai_native.py` | Partial: lifecycle/stream/error work and ordered gateway feedback reviewed; removed metadata mode/attachment scaffolding and unconsumed wire fingerprints; remaining transport/payload/replay source review pending |
| `providers/native/opencode.py` | Partial: configured gateway authority, wire vocabulary, aliases, timeout/key propagation and native URL normalization reviewed; external gateway protocol/header assumptions pending |
| `providers/native/opencode_go.py` | Partial: nonnullable endpoint path and minimal feedback model set reviewed; configured host precedence reviewed and redundant route URL pins removed; external protocol quirks pending |
| `providers/native/openrouter.py` | Reviewed: small direct adapter using vendor endpoint normalization and native OpenAI transport |
| `providers/native/provider_config.py` | Reviewed: table-owned wire defaults, endpoint/gateway defaults and Copilot token resolution; removed dead projections |
| `providers/native/provider_table.json` | Reviewed with parser/consumer and packaged-table tests; source/provenance retained in JSON |
| `providers/native/provider_table.py` | Reviewed: typed rows, duplicate/unknown IDs and config/catalog consumers; removed unused notes and forwarding helper |
| `providers/native/reasoning_effort.py` | Reviewed: canonical vocabulary, unknown-preserving clamping, explicit-off and binary/protocol mapping; removed rank maps/aliases/runtime type imports |
| `providers/native/thinking_rules.json` | Reviewed with parser/consumer and packaged-table tests; source/provenance retained in JSON |
| `providers/native/thinking_rules.py` | Reviewed: model-name overlays/effort maps removed; upstream capabilities select validated protocol defaults and named budgets; Kimi cap convention pending adapter review |
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
| `providers/native/trace.py` | Reviewed: opt-in, whole-record preview budget, JSON encoder, private append and isolated warning-only failures; documented per-file growth/trusted-parent limits |
| `providers/panels.py` | Reviewed: names/catalog-only configuration facts, typed paged transcript dispatches, read-only commands/views; removed guessed credential status |
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

## Remove unused authorization framework

Repository-wide consumer audit found ProviderAuthResolver and its authorize/
callback/method/result types only referenced by their own module and unit tests.
They are absent from CLI, HTTP, SDK contracts and plugin registration. Removed
that unconnected 771-line framework and its isolated callback tests, retaining
registered-provider credential/header/redaction tests and active config parsing.
Removed Copilot refresh_token/refresh_leeway_seconds fields that no executor reads,
and their orphan positive-number validator. Supplied OAuth access tokens and
service-account loading remain supported by the actual adapters.

Unified Copilot token/token_env_var exclusivity for both labels. Fixed the old env
resolution path setting both a token value and its variable reference, which then
rejected its own result. New active-ingress tests reject unsupported refresh fields
and verify the environment token path. Files diagnostic wrapper now accepts the
SDK Tool protocol instead of Any. Namespace docstrings reflect shared capabilities.

Validation: 135 generator/provider/auth-ingress/thinking/usage/stream tests passed
(32.67s). Ruff, formatting and source type checks passed.

## Provider ingress: strict primitives instead of schema wrappers

Removed unused JSON Schema-only aliases, enum/property metadata and the custom
provider-name schema regex. Repository-wide search found no schema-generation
consumer; actual auth-method and custom-name validation still lives in ingress.
Removed four handwritten primitive validators in favor of existing Pydantic
StrictStr/StrictBool and a strict positive finite float constraint. Explicit null
and integer timeout values remain accepted. NaN and infinity, previously accepted
as timeouts, now fail at configuration entry. String-list and mapping normalizers
remain because they implement actual null and nonempty-value semantics.

Added active-ingress tests for finite/positive timeouts and rejection of primitive
coercion. Broader capability audit remains in progress.

Validation: 145 provider/generator/stream tests passed (32.36s); all 28 ingress/
error tests passed separately after adding four strict-type cases. Ruff and source
type checks passed. This is focused validation, not a full-suite completion gate.

## Provider ingress: consume validated payloads once

All eight private shape/auth converters are called only after the top-level
ProviderConfigsPayload validator has produced their typed payload models.
Removed their unreachable raw-object validation fallbacks, typed the converter
inputs, removed three unused field-path parameters, and dropped duplicate custom
mapping shape checks. Runtime callers continue through one strict public ingress.

Found a credential bug during this review: Google OAuth and service-account
configs inherited an ambient API key and then rejected themselves as conflicting.
Only api_key mode now reads its environment fallback. Explicit conflicting keys
still fail. Added OAuth/service-account ambient-key tests and an explicit-conflict
regression. Remaining converter work includes simplifying literal normalization
and reviewing endpoint/header handling; this does not complete capability audit.

Validation: 149 provider/generator/stream tests passed (32.26s); the final ingress/
error suite passed all 31 cases after the Google fix. Ruff and source type checks
passed; CodeGraph synchronized. Full-suite completion gate remains outstanding.

## Provider diagnostic representations

Reviewed config/error representations: dataclass credential fields and Pydantic
payload repr previously printed raw API keys/access tokens/Copilot tokens. Marked
those fields repr=False with standard dataclass/Pydantic options; actual values
remain available to adapters. Enabled Pydantic hide_input_in_errors so chained
validation tracebacks do not print malformed credential inputs. Regression tests
cover nested built-in/custom provider representations and a full formatted error
traceback. This covers configuration diagnostics, not arbitrary upstream messages
or credentials embedded in user-supplied URLs.

Removed sanitize_tool_arguments, which had no consumer. Result sanitization remains
used by Anthropic; corrected the obsolete legacy-port docstring. Its whole-payload
size/depth limits still require review during the remaining projection audit.

Validation: 80 auth/catalog/dispatch/panels/stream/error tests passed (21.19s),
including both new credential diagnostic regressions; Ruff and source type checks
passed. Full capability audit and its final full-suite gate remain outstanding.

## Workspace edit ingress: one authoritative schema

Replaced hand-maintained apply_workspace_edit JSON Schema with the Pydantic input
model's generated schema, retaining coordinate/version/hash descriptions. Both
nested edit and top-level argument models now forbid extra fields and primitive
coercion. Removed redundant encoding validation, unused hash regex and empty
constructor. Existing UTF-8/16/32 coordinate and version behavior stays covered.

Strict validation exposed the SDK's frozen tuple/mapping arguments. Convert them
once with the existing SDK json_wire_object before validation. This also fixes the
missing-expectedHash diagnostic precheck, which previously checked only lists and
therefore never saw normal SDK-frozen edit arrays. Regression tests verify its
structured diagnostic and that invalid later edits leave the entire batch unwritten.

Mutation review is not yet complete: direct writes can truncate on failure and
validation-to-write concurrent-change handling still needs review. Do not infer
transactional multi-file behavior from the ingress tests.

Validation: 56 files/artifact unit tests and 6 optional-query/core-builtins
integration tests passed. Ruff and source type checks passed, CodeGraph synced.
Remaining capability work and full-suite completion gate are still outstanding.

## Workspace edit commit boundary

Replaced truncating Path.write_bytes with a small shared file-replacement function:
write and fsync a temporary sibling, preserve permission bits, verify the target
still matches validated bytes, then os.replace. A finally block removes temporary
files on every exit. Symlink substitutions at this boundary are rejected. Removed
a second edit sort and post-write disk reread; returned hashes/diffs now describe
the bytes this invocation actually committed, not a later writer's contents.

Regression tests inject replacement failure and a document-query-time concurrent
writer, verifying original/concurrent bytes survive and no temporary files remain.
A successful edit preserves executable permissions and CRLF. This is per-file
replacement, not a multi-file transaction: an already-applied prefix stays applied
on later failure, as the existing error reports. The final content-check/replace
window remains best effort, matching write; no process-wide locking added.
Metadata beyond permission bits (ownership, ACLs, xattrs, hardlink identity) is not
preserved by replacement, consistent with the existing write tool. Existing edit
paths still use direct writes and will be reviewed separately.

Validation: 59 files/artifact tests and 6 core-builtins/optional-query integration
tests passed. Ruff, source type checks and CodeGraph sync passed. Full capability
audit and final full-suite gate remain outstanding.

## Exact edit commit boundary

Both single replacement and sequential same-file edit batches now reuse the file
replacement function introduced for workspace edits. This removes both truncating
writes and post-write reads. Hash and diff projections describe the committed
bytes. UTF-8 read uses newline='' so expected original bytes round-trip exactly,
including CRLF. No new writer abstraction or dependency added.

Six regression cases exercise both edit shapes under success, replacement failure,
and a concurrent writer between text preparation and commit. They verify expected
hash, CRLF, executable permissions, preservation of failed/concurrent targets and
absence of temporary residue. The compare/replace race and metadata limitations
are the same as documented for workspace edits. Patch mutation paths and edit
argument-path duplication still need review; cancellation behavior remains pending.

Validation: 65 file/artifact unit tests and 6 core-builtins/optional-query
integration tests passed. Ruff and source type checks passed; CodeGraph synced.
Full capability audit and final full-suite gate remain outstanding.

## Exact edit: consolidate single and batch execution

Single replacements and same-file batches now normalize to one list of exact
replacements, then share target resolution, expected-hash validation, UTF-8 read,
in-memory replacement, newline conversion, checked file replacement and final
projection. Removed the duplicate single-edit method and empty constructor: net
93 fewer implementation lines. Single-edit replacement failures retain their
original diagnostic; batches retain prepared-edit counts and no-write-on-preflight
failure. Only batches construct per-step diffs/results.

Result semantics are deliberate: a single replaceAll reports occurrence count;
a batch reports number of edit items and each item's occurrence count. Added
regressions for both shapes so consolidation does not silently alter that contract.
Single-edit paths now reject whitespace-only paths consistently with batches.
Patch parsing/mutation and cancellation remain pending audit.

Validation: 67 files/artifact unit tests and 6 core-builtins/optional-query
integration tests passed. Ruff/source type checks passed; CodeGraph synchronized.
Complete capability audit and final full-suite validation remain outstanding.

## Unified patch: stream input instead of a workspace scratch file

Unified patches previously overwrote workspace/.voidcode_edit.patch and removed
it after invocation, deleting any preexisting user file at that name. Concurrent
calls also shared the same filename, and initial write failures escaped cleanup.
Git already accepts patch data on stdin: both check/apply commands now receive the
normalized patch directly through subprocess.run(input=...). Removed all scratch
file creation and cleanup, including a broad swallowed cleanup exception.

Success/failure regressions preserve a preexisting binary scratch-named user file
and assert the target changes only on successful patch validation. Git remains
the unified-diff implementation. Check and apply are separate process executions;
this change does not add a transaction or solve intervening target changes. Marker
patch mutations, path metadata parsing and cancellation are still pending review.

Validation: 69 files/artifact unit tests and 6 core-builtins/optional-query
integration tests passed. Ruff and source type checks passed; CodeGraph synced.
Full capability audit and full-suite completion gate remain outstanding.

## Marker patch commit boundaries

Marker update preparation now retains the source bytes used to derive each change,
including staged results for repeated same-file edits. Updates reuse checked
replacement instead of truncating writes. Extended that same small helper to
create new files via a complete temporary sibling plus os.link: a concurrent
creator fails with EEXIST instead of being overwritten. Adds and move destinations
use this creation mode. Deletes and move sources compare the prepared bytes again
before committing. Removed no patch syntax or in-memory exact matching behavior.

Six regressions cover post-preflight competing writers for add/update/delete/move,
and successful add/move commits with temporary-file cleanup. Add retains existing
parser behavior (no implied trailing newline); moving the existing newline-terminated
source retains its trailing newline. Existing permission and write-failure helper
regressions continue covering the shared implementation.

This is not a multi-file transaction. Move creates destination before deleting
source, and checks are best effort; a writer can still race after the last check.
Marker operation ordering/path aliases and partial-commit reporting need further
review. No rollback or global filesystem locks added.

Validation: 75 files/artifact unit tests and 6 core-builtins/optional-query
integration tests passed. Ruff/source type checks passed; CodeGraph synchronized.
Full capability audit and final full-suite gate remain outstanding.

## Marker patch: one sequential preflight state

Removed separate planned-add tracking and mixed disk/staged-text lookup. Preflight
now caches bytes or absence by canonical resolved Path, applying each planned
operation to the same state. The prepared operations retain canonical paths and
the precise preceding bytes for commit checks. No new persistence/rollback layer.

Previously delete-then-update could pass preflight using the original disk file,
then delete it and fail during commit. Newly added files could not be deleted;
path aliases lost staged changes. These now follow the actual sequential plan.
Six regressions cover add/delete, invalid delete/update with no disk writes,
delete/add, move/update, duplicate aliased adds, and sequential aliased updates.
Implementation is nine lines shorter while eliminating the competing state stores.

Metadata reporting for partial commits, cancellation and the documented filesystem
race windows remain pending. Full capability audit is not complete.

Validation: 81 files/artifact unit tests and 6 core-builtins/optional-query
integration tests passed. Ruff/source type checks passed; CodeGraph synchronized.
Full-suite completion gate remains outstanding.

## Notebook views: preserve output truncation across cell selection modes

Notebook output pagination previously computed a continuation only with an explicit
cell selector. Reading a whole notebook with output_limit omitted remaining outputs
while reporting outputs_truncated=False and, on the last cell page, partial=False.
Each cell now reports its own next_output_offset and omission flag; a single bool
aggregates output truncation across the page. The top-level output continuation
remains meaningful only with an explicit cell selector. Models can use the omitted
cell's index plus its continuation to recover remaining outputs.

Regressions cover both full-notebook and single-cell reads, including following
the returned cursor until the view becomes complete. Replaced the local streaming
hash loop with hashlib.file_digest. Notebook total-output limit behavior, SQLite
budget accounting, PDF process lifetime/pagination and generic read paths still
need audit; no completion claim for read_formats as a whole.

Validation: 86 read-format/files/artifact/coverage unit tests and 7 read-loopback/
core-builtins tests passed. Ruff/source type checks passed; CodeGraph synchronized.
Complete capability audit and full-suite gate remain outstanding.

## PDF extraction: one process cleanup path and nonnegative preview budget

PDF preview could exceed its byte cap when a previous line used the entire budget:
the separator made remaining bytes -1, so encoded[:-1] included nearly all of a
subsequent long line. Clamp remaining bytes at zero; the omitted line remains
available through its unchanged next_offset and the retained full-text artifact.
Regression uses a full-budget first line followed by a 50,000-byte line.

Consolidated three kill/communicate branches into one finally: kill any still-live
extractor and wait for it on all exits. No pipes need draining (stdout/stderr are
DEVNULL). This also handles unexpected cancellation callback errors and interrupts,
which previously leaked the child. Five lifecycle regressions cover cancellation,
timeout, extracted-size overflow, unexpected callback error and KeyboardInterrupt.
Tests use controlled extractor doubles rather than depending on installed Poppler;
real-vendor/document processing is not asserted by these tests.

Remaining format review includes SQLite accounting/snapshot semantics, notebook
total-output limits, PDF clipped-line continuation and generic read control flow.

Validation: 91 format/files/artifact/coverage tests passed, followed by all 8 format
tests after adding interrupt coverage. Seven read-loopback/core-builtins tests
passed. Ruff, formatting and source type checks passed; CodeGraph synchronized.
Complete capability audit and final full-suite gate remain outstanding.

## SQLite views: budget encoded data, not source-cell bytes

Replaced type-by-type byte-count guesses with JSON encoding of the actual projected
row. BLOB hex expansion, string/control escaping, array separators, column names
and schema JSON now count toward the data budget. Native sqlite3 row values pass
through directly except for the explicit BLOB envelope; removed invented fallback
string coercion for types SQLite does not return. Non-finite floats fail at the
view boundary using JSON's allow_nan=False instead of leaking into a later SDK
serialization failure.

Pagination regressions cover hex BLOBs and escaped control strings under a small
budget, follow every returned row offset, and verify complete recovery. Added an
infinity query regression plus DELETE/UPDATE/PRAGMA/ATTACH read-only rejection tests.
This bounds tables/columns/rows JSON, not all outer metadata or pretty-print
indentation: read_tool's existing artifact/output cap governs the full rendered
view. sqlite3's query-only mode, authorizer and deadline remain in effect.

Database-file source hashes do not represent a logical WAL snapshot; remaining
review must address that observation semantics along with notebook global output
limits and generic read control flow. Full audit remains in progress.

Validation: 95 format/files/artifact/coverage tests passed, then all 15 format tests
passed after adding read-only rejection coverage. Seven read-loopback/core-builtins
tests passed. Ruff/source type checks passed; CodeGraph synchronized. Complete
capability audit and final full-suite gate remain outstanding.

## Notebook global output budget: paginate instead of rejecting a valid view

Removed the per-output counter/overflow exception. Each cell now selects at most
the remaining global output budget, and the selected slice updates that budget
once. Later cells still expose their sources, their output continuation and the
existing partial flags, even if the budget leaves them zero output entries.
This removes a valid-notebook failure at the 21st output without widening limits.

Regressions cover two cells where the first consumes either 15 or all 20 output
slots. Both return a bounded partial page; following the second cell's continuation
recovers all its outputs and ends in a complete view. Existing per-cell limits,
text/image budgets and strict content checks remain unchanged. Rendering a page
does not imply omitted outputs were validated.

Validation: 101 format/files/artifact/coverage tests and 7 read-loopback/core-builtins
tests passed. Ruff/source type checks passed; CodeGraph synchronized. Updated the
inventory to reflect completed source sections and explicit remaining work rather
than stale pre-refactor statuses. Full capability audit/full-suite gate remain open.

## Provider gateway and naming cleanup

Reviewed naming source/callers and removed canonical_model_reference, which had
no consumer. Provider ids still normalize while model-id casing is preserved;
unknown-provider diagnostics and custom reservations retain active callers.
Reviewed OpenRouter adapter: it is a small direct native adapter with vendor-owned
host defaults and no duplicate runtime branch.

OpenCode Go's provider_config always returns an endpoint, so removed nullable
annotation, impossible endpoint=None constructions, credential/timeout ternaries
and empty-model-map fallback. Removed dated manual account/debug transcripts and
model lists from Go/Zen source rather than treating stale comments as protocol
contracts. Fixed Zen Google route failing to forward timeout_seconds; added a
regression without instantiating an SDK client. Gateway route hosts/configured URL
precedence and Go model-specific tool feedback still need protocol review.

Trace is actively used by OpenAI/Anthropic; it is not dead code. Review found its
recursive field clipping is not a total-record budget and trace-write failures can
surface through successful requests. Its semantics/cleanup review remains pending.

Validation: 118 provider auth/catalog/dispatch/reasoning/stream/lifecycle/error tests
passed (32.70s), including the timeout regression. Ruff/source type checks passed;
CodeGraph synchronized. Complete capability audit and full-suite gate remain open.

## Provider trace: standard encoding, per-record budget and diagnostic isolation

Replaced recursive per-field string/list/dict clipping with JSONEncoder.iterencode
and one 32,000-byte preview budget for the whole record. Small records retain their
structured fields; oversized records retain trace identity plus an explicit UTF-8
preview/truncated envelope. Encoding the preview as a JSON string adds escaping,
so the output line is bounded by twice that preview budget plus envelope overhead.
This bounds each line, not accumulated file size or peak encoding of one huge value.

Trace remains opt-in. File/serialization failures warn with exception class only,
without printing payload data or turning a completed provider request into failure.
Private 0600 creation/restriction and O_NOFOLLOW protect the final file component;
parent directories are still user-selected trusted paths. The trace intentionally
contains conversation content when enabled; it is not a credential-redaction layer.
No rotation/configuration framework added.

Tests cover structured small records, large Unicode/escaped records, existing-file
permissions, directory/serialization/symlink failures and the disabled no-I/O path.
A real Anthropic SDK loopback request succeeds with normal stop/output even when
trace logging targets a directory. Existing OpenAI/Anthropic callers remain intact.

Validation: final trace/auth/dispatch/reasoning/stream/lifecycle/error suite passed
all 112 tests (32.49s). Ruff, formatting and source type checks passed; CodeGraph
synchronized. Full capability audit and final full-suite gate remain outstanding.

## Provider registration ingress: reject malformed and colliding entries

The registration adapter previously coerced provider names with str(), treated
malformed provider blocks as empty mappings, and silently overwrote case-folded
name collisions before the native parser could see them. It now rejects non-object
sections/entries, non-string/empty names and canonical duplicate ids at this ingress.
Missing providers still means no configured entries. Added six malformed-shape and
two built-in/custom collision regressions. Panels consume configuration only after
this registration validation succeeds.

Typed _entry_for/_materialize with the existing ProviderConfigEntry union, removed
the duplicate broad runtime type guard, and inlined the unused _endpoint_of forwarding
function. Per-provider shape narrowing remains necessary for adapter construction.
No fallback config or compatibility path added.

Reviewed the shared provider plugin factory: registration precedes facts creation,
views/commands are read-only, and ExitStack closes every registered provider. Panel
source/callers reviewed; nested-auth credential presence and dispatch configuration
normalization still require follow-up before marking those modules complete.

Validation: 160 provider trace/auth/catalog/dispatch/panels/reasoning/stream/lifecycle/
usage/error tests passed (32.51s). Ruff/source type checks passed; CodeGraph synced.
Full capability audit and final full-suite gate remain outstanding.

## Provider auth normalization and information panels

Core injection includes api_key=None even when a provider uses explicit nested
auth. The adapter previously left that flat key alongside auth and its strict
native shape rejected otherwise valid Google/Copilot configuration. Nested-auth
adapters now remove the empty flat placeholder; a nonempty flat key plus auth
raises a clear conflict instead of silently selecting one. Removed redundant
nested-provider and selection-key sets; credential field mapping is authoritative.
Four regressions cover both nested adapters and explicit conflicts. Generic core
environment resolution remains vendor-agnostic: a resolved nonempty flat key also
counts as a conflicting supplied credential and must not be silently discarded.

Removed the panels' credential boolean and its display copy: it only checked flat
api_key and incorrectly reported other auth methods as absent. Panel construction
now consumes configured names only, never credential values or inferred readiness.
No second auth-status mechanism or extra registry was introduced. Existing secret
exclusion, sorted catalog/configuration, transcript paging and read-only command/
view tests retain coverage. Reviewed every panel function and factory caller;
last dispatch comes from typed committed transcript records, not current config.

Validation: 164 provider trace/auth/catalog/dispatch/panels/reasoning/stream/lifecycle/
usage/error tests passed (32.75s). Ruff/source type checks passed; CodeGraph synced.
Full capability audit and final full-suite gate remain outstanding.

## Provider config: declarative collection/literal validation

Replaced custom string-list/map parsers with strict Pydantic collection and nonempty
string constraints. Tiny before validators retain explicit-null normalization;
Anthropic header lists convert once to the domain tuple. Auth method/scheme fields
now use the existing Literal types directly, deleting three switch validators and
three duplicate allowed-value tables. Cross-field credential rules still apply in
the typed converters, including environment-sensitive method checks.

The generic payload-model validator had only one remaining caller; inlined it at
the one public config ingress. Removed old quoted runtime-config-field formatting
and obsolete error helpers after repository-wide source/test consumer checks.
Pydantic locations now drive field/index paths directly, retaining unsupported-field
messages and credential-safe chained errors. No backward-compatibility formatter.

Eleven regressions cover malformed collection values, empty model aliases/targets,
non-string map entries, invalid auth literals, null normalization, preserved alias
casing and valid header/domain tuple conversion. Together these remove 161 net
implementation lines. Remaining config review concerns table/field metadata wiring
and domain/env converters; this entry does not claim full config source completion.

Validation: 175 provider trace/auth/catalog/dispatch/panels/reasoning/stream/lifecycle/
usage/error tests passed (32.70s). Ruff/source type checks passed; CodeGraph synced.
Complete capability audit and final full-suite gate remain outstanding.

## Reasoning effort: one ordered vocabulary, no rank-map scaffolding

Reviewed all reasoning_effort functions and their OpenAI/Anthropic/Google/generator
callers. Canonical ladder now holds the literal vocabulary directly; removed seven
constant aliases that only expanded Google request mapping. Google continues mapping
minimal/low/medium/high to SDK enums and xhigh/max to HIGH. Moved type-only rule
imports behind TYPE_CHECKING; the effort vocabulary no longer imports/loads thinking
rules just to provide strings. Corrected the old misplaced/dependency-free docstring.

Clamping now filters the canonical ladder and takes its final candidate, falling
back to the first supported canonical level. Removed duplicate per-call rank maps,
max/min selection and filtered-list scaffolding. Unknown/empty capability ladders
and explicit off retain their previous behavior. Eight regressions cover unsorted
levels, down/up bounds, off, unknown/empty/vendor-only metadata and mixed levels.
The existing provider loopback effort cases continue verifying actual wire payloads.

The remaining Kimi-name output-cap convention lives in thinking_rules/openai_native,
not this vocabulary/helper module; its protocol justification still needs review.

Validation: 148 generator/data/auth/reasoning/stream/lifecycle/error/protocol tests
passed (28.80s). Ruff/source type checks passed; CodeGraph synchronized. Complete
capability audit and final full-suite gate remain outstanding.

## Google native adapter: actual SDK timeout and error transport

Source review found timeout_seconds was never passed to Google SDK options. Client
HttpOptions now carries the configured timeout converted from seconds to positive
integer milliseconds, with existing configured endpoint/version handling preserved.
The installed google-genai SDK imports httpx, while our error classifier checked
httpx2.TransportError; switched to the SDK's actual transport exception family.
An actual delayed loopback request now stops at its configured budget, remains a
single request and reports a retryable network failure.

Removed the identity tool-name maps and no-op visible-argument wrapper. Tool names
stay verbatim and arguments use the existing SDK JSON conversion directly. No
provider-specific name remapping behavior was removed.

Review also found supplied oauth access tokens are currently sent as API keys.
Official Gemini OAuth docs specify Authorization: Bearer, not x-goog-api-key:
https://ai.google.dev/gemini-api/docs/oauth . The installed SDK credential path uses
Vertex AI mode and needs separate endpoint semantics. User preference requested
for real OAuth support versus deleting the incomplete mode; no dependent auth
change made while that choice is pending. Stream cleanup/cancellation, attachment
metadata and remaining request/response source review are also outstanding.

Validation: 163 generator/data/auth/reasoning/stream/lifecycle/usage/error/protocol
tests passed (29.05s). Ruff/source type checks passed; CodeGraph synchronized.
Complete capability audit and final full-suite gate remain outstanding.

## Google streaming: explicit stream ownership and cancellation boundaries

Wrapped the SDK generator in contextlib.closing. It now closes before terminal
success or cancellation events, and on malformed completion, consumer close and
exceptions. Borrowed SDK clients remain borrowed; only their invocation stream is
closed here. Added cancellation checks before client access and at chunk boundaries,
matching the other adapters' error/cancelled-done event shape. No worker/pump framework
added. A blocked socket read still relies on the configured SDK HTTP timeout; prompt
mid-read cancellation remains pending rather than being claimed solved.

Five retained-stream regressions verify close counts on success, consumer exit,
missing terminal finish_reason, pre-cancel without any pull/client-stream access,
and mid-stream cancellation with cleanup before the first cancellation event.
Two real Google SDK SSE loopback cases verify normal text/usage/stop and cancellation
without leaking the next buffered delta. OAuth choice and attachment publication/
validation remain outstanding from the preceding Google source review.

Validation: 118 Google/stream/auth/lifecycle/error/thinking tests passed (19.15s).
Ruff/source type checks passed; CodeGraph synchronized. Complete capability audit
and final full-suite gate remain outstanding.

## Shared stream wait: poll cancellation without shrinking network budgets

The existing one-slot stream pump only observed pending cancellation when the full
queue/network timeout expired. Added monotonic deadlines with 50ms queue waits
when an abort callback is supplied. Early empty polls continue until the original
first-event/idle deadline; each delivered item resets the next budget after caller
processing, preserving prior timeout semantics. Streams without abort callbacks
still wait directly to the deadline. The function now advertises its actual
Generator return type so standard contextlib.closing can own it.

Google now reuses this existing pump and its configured timeout (300s default),
with explicit contextlib cleanup of the wrapper. Pending-abort exceptions map to
Google's existing error/cancelled-done event shape. Removed the no-prefetch assertion
from the earlier retained-stream test: bounded one-slot prefetch is permitted;
observed output and close ownership remain asserted.

Two cooperative stalled-source tests cover first-token and post-delta waits, proving
cancellation returns before a 5s deadline and wakes/closes the source once. A real
Google SSE test cancels during a delayed chunk and returns within 1s despite a 3s
SDK read timeout. This proves caller responsiveness, not forced interruption of
SDK generator I/O: concurrent generator.close may be rejected while executing;
the daemon reader can remain until its SDK/socket read finishes or times out.
No additional thread, watcher, timer or cleanup framework was added in production.

Owned Google clients now always receive a finite SDK HTTP timeout, using the same
_timeout() value as the pump (300s default). This avoids a finite caller deadline
paired with an unbounded default SDK read. Three constructor regressions verify
default, explicit seconds-to-milliseconds and sub-millisecond rounding. Borrowed
clients retain their own configured transport/read bounds. Cancellation is prompt
for the caller; SDK I/O teardown can still lag until its bounded read returns.

Validation: final Google/shared-pump/stream/auth/lifecycle/error/thinking suite passed
all 127 tests (20.00s), including real delayed SSE and timeout requests. Ruff/source
type checks passed; CodeGraph synchronized. Full capability audit/full-suite gate
remain outstanding.

## Remove unconnected attachment metadata and duplicate result sanitization

Repository-wide producer audit found no core/plugin publisher for the adapters'
metadata.attachments convention. ProviderMessage owns/freeze-normalizes JSON:
attachments become tuples and data becomes MappingProxyType. Verified those actual
runtime types; all three old adapters checked list, and Anthropic's secondary data
path checked dict, so those branches never accepted ordinary SDK messages.

Removed Google/Anthropic attachment block builders, OpenAI's synthetic-feedback
attachment metadata builder, and the orphan _result_sanitize module. Removed
Anthropic's no-op argument wrapper and secondary data/status interpretation. Native
tool results now use the SDK content/is_error fields; core engine already calls
model_result_text before creating those messages. Provider capabilities metadata
still describes vendor facts; this cleanup does not add working multimodal model
input. A future attachment feature needs a declared/published SDK contract rather
than dormant metadata conventions.

Two real Anthropic SDK loopback cases verify tool-use arguments, result body/id and
success/error flags. Uncontracted metadata.status does not override is_error. No
compatibility branch or second output-budget implementation retained.

Validation: 154 auth/dispatch/reasoning/stream/Google/lifecycle/error/protocol tests
passed (36.17s); both final Anthropic flag-authority cases passed separately. Seven
core-builtins/read-loopback tests passed. Ruff/source type checks passed; CodeGraph
synchronized. Full capability audit and final full-suite gate remain outstanding.

## OpenAI gateway feedback: remove mode scaffolding and preserve ordered history

No generated catalog row or runtime producer supplies tool_feedback_mode. Removed
that metadata field/parser/type vocabulary and request-metadata selection branch.
The actual Go gateway exception is represented by one frozen model-name set on its
adapter, with model alias handling preserved. Removed OpenAI's no-op argument wrapper
and the source=replayed_conversation classifier, which core never publishes.

Synthetic feedback now converts each tool result at its original conversation
position, keeps assistant text, and carries only explicit SDK result/call fields.
Removed invented current-run/prior-run separation, end-of-request result relocation,
and metadata-derived error/bounds/reference fields with no core publisher. Argument
matching is chronological and consumed per result, so repeated call ids in later
batches cannot overwrite earlier result arguments. Unknown arguments remain None.

Four actual Go-adapter/SDK loopback cases cover model aliases, repeated call ids,
multiple request/result batches, retained assistant content, argument association
and error status. A standard endpoint case proves opaque request metadata does not
change its tool protocol. Existing ordinary tool-role and reasoning replay tests
remain applicable. Net implementation reduction: 100 lines. This preserves the
existing Go exception; it does not newly verify a funded live gateway account.

Validation: 212 provider/generator/stream tests passed, followed by all 14 final
registered-dispatch cases after chronological call-id handling. Seven core-builtins/
read-loopback tests passed. Ruff/source type checks passed; CodeGraph synchronized.
Complete capability audit and final full-suite gate remain outstanding.

## Validation blocker: core coordination worker/resource shutdown order

Reproduced the previously recorded SDK facts warning as a failing test with
PytestUnhandledThreadExceptionWarning promoted to error. CoreActivationDriver
called store.sessions after the store was closed. Core.close closed its resource
stack before joining driver/ticker, and CoreAdmitDriver was not tracked at all.

Moved coordination worker ownership into the existing ExitStack, before runtime/
store callbacks unwind. One background worker list covers activation, ticker and
admit passes; a separate started flag preserves one-time background startup.
Thread start/registration happens under the existing session-lock guard, preventing
shutdown from joining an unstarted thread or missing an admitted coordinator.
Completed admit workers are pruned when a new pass starts. Joining is complete
before resource release, including failure during final constructor revision
recording. No background framework or exception-suppression workaround added.

Four synchronized regressions verify store readability until each coordinator
finishes and safe failed-start cleanup/reopening of the workspace lease. The SDK
facts reproduction plus upgrade drain/admit tests passed (13 tests), with thread
warnings still treated as failures. Shutdown waits for admitted coordination work;
noncooperative trusted code can delay it, as with the existing session-drain rule.
The separate child-session threads remain governed by their session lifecycle;
this change does not claim a full audit of unrelated core concurrency.

Inventory reconciliation found every current capabilities file represented in the
ledger, with 34 entries still partial/pending. Full-suite validation of this repair
is an audit checkpoint; remaining source/data reviews retain their scope.

Full Python checkpoint: 1660 passed, 1 skipped in 367.78s with
-W error::pytest.PytestUnhandledThreadExceptionWarning. No warnings were reported;
the original store-closed thread failure is resolved. Ruff/format/source type checks
passed and CodeGraph synchronized. This supersedes the earlier warning-bearing
full-suite checkpoint. The full capabilities objective remains open with the source/
data entries above still partial/pending; frontend/full project gate is not claimed.

## Provider config review completion: unify shapes and remove dead defaults

Completed remaining source/consumer review of the config field/table wiring,
frozen domain construction, environment resolution, custom name validation and
shape converters. Endpoint/named_endpoint used identical parsers and defaults;
collapsed them into one shape. Removed the base-URL helper and fallback already
covered by the authoritative OpenAI-wire default table. Unknown defaults remain
None rather than becoming another vendor's URL.

Provider-block absence is handled once at dispatch, leaving six shape converters
nonnullable and removing their repeated None branches plus an impossible custom
skip. Google credential validation now enforces the selected required field and
forbids the other two in one place. Three regressions check every method's missing,
valid and conflicting credential combinations. This preserves parser semantics;
the separate Google OAuth delivery issue remains pending in the adapter.

Removed endpoint cache_retention, which had no configuration ingress/producer.
OpenAI still explicitly refuses unsupported retention on the SDK request itself,
with a new request-boundary regression. Anthropic's active configuration/request
retention behavior remains intact. No compatibility field or fallback retained.

Validation: 218 provider/generator/stream tests passed on this worktree before the
continuation; reviewed the unchanged diff again. Ruff/source checks are revalidated
with the commit gate. Full capabilities objective remains open.

## Background output: preserve byte-page boundaries and final readiness logs

Log scans now collect bounded raw byte chunks and decode once, replacing repeated
page-text concatenation. UTF-8 codepoints and CRLF split across SDK pages no longer
become replacement characters or extra lines. Readiness polling keeps its bounded
tail as bytes and decodes the combined window, so split characters can be recovered
when the next page arrives without introducing an incremental-decoder abstraction.

Readiness aggregates liveness across both reads and waits for both streams' EOF
before concluding an exited process never became ready. It no longer discards
unread final pages or misses stdout written between a live stdout read and an
exited stderr read. Deadline and literal per-line matching stay unchanged.
Replaced the handwritten nonempty stdin validator with Field(min_length=1) and
removed obsolete runtime/P4 migration prose from the module header.

Four regressions cover tiny UTF-8/CRLF byte pages, multi-page ready text for both
running and exited processes, and exit between stream reads. All 102 background/
shell/core-process/lease tests passed (34.43s); Ruff/source type checks passed.
The existing 8MiB line-scan ceiling and byte-cursor product choice remain unresolved;
this does not claim a complete background_process source audit.


## Typed background process identities

Kept validated `_ProcessRecord` objects throughout background process operations,
serializing only when writing workspace state. Removed the dictionary round trip,
two per-field type/default helpers and impossible pid-zero fallbacks. A live host
handle without a persisted mirror still reports its real identity with unknown
command/description and start time. Exit updates preserve an already known exit
code when the host has no new answer; omitted optional exit fields remain omitted.

Validation: 102 background-tool, shell, host-process and process-lease tests passed
(34.84s), plus the added host-only identity regression (1 passed). Ruff, formatting,
source type checking and CodeGraph synchronization passed. Log scan ceilings and
cursor behavior remain pending; this is not a claim of full module completion.


## Host-owned background log paths

Removed the background tool's workspace log directory and explicit per-stream
filenames. The existing SDK `spawn_process` contract supplies host-owned default
paths and returns them on the handle; state and tool results already use those
actual paths. This removes filesystem writes before the host admits the spawn
and avoids creating `.voidcode/background-processes` in user workspaces.

Adjusted existing tool assertions and the fake service's default-path behavior;
no new test cases retained, following the user's instruction to stop adding tests.
Validation: 103 existing background, shell and host-process tests passed (36.59s).
Ruff, formatting, source type checking and CodeGraph synchronization passed.


## Shell output artifacts use the SDK contract

Removed shell's ad hoc artifact dictionary. Results retain the existing immutable
SDK `ArtifactReference` until serialization, then use its `sha256`, `bytes` and
`media_type` payload. Output bounds keep the same content-addressed URI with the
same SDK metadata. Removed duplicate identity/media aliases, fixed status/source
fields and the stream kind already expressed by stdout/stderr field names.
There are no repository runtime/UI consumers of those removed dictionary keys.
This also removes downstream dictionary shape checks on internally produced data
and the test-only duplicate artifact dataclass. Existing assertions now use the
actual SDK contract; no tests were added.

Validation: 40 existing shell, host-process lease and SDK codec/contract tests
passed (17.59s). Ruff, formatting, source type checking and CodeGraph sync passed.


## Shell result fields and capture wording

Removed shell-only `output_char_count` (no business consumer and measured before
some final output transformations), `interrupted` (duplicated timeout/cancellation)
and `stop_confirmed` (always true when present, despite no confirmation field in
`ProcessResult`). Retained the actual timeout/cancel flags, reason, exit code,
stream previews, capture-limit flag and SDK output bounds. This does not alter the
host's process shutdown implementation or claim its bounded kill wait proves exit.
Changed artifact guidance to "Captured process output" because a capture-limit
failure cannot honestly advertise the artifact as full process output. Updated
existing assertions only; no tests added.

Validation: 40 existing shell, process lease and SDK codec/contract tests passed
(17.32s); Ruff, formatting, source type checking and CodeGraph sync passed.


## Grep context representation and ingress bound

Context lines now retain immutable `(line_number, text)` pairs from the reader
through matching and preview rendering. Only `as_payload` builds JSON objects.
Removed generic JSON freezing/thawing and rendering-time type checks for internally
produced lines. Replaced a nonlocal line counter with `enumerate`.

Bound `context` to 100 in both ingress validation and the published tool schema;
previously an arbitrary count could pre-read the whole file before returning its
first match (or overflow `islice`). This does not bound a single line or total
result bytes; those and cooperative cancellation remain audit work.

Validation: 100 existing file/search tests passed (0.71s); no tests added or changed.
Ruff, formatting, source type checking and CodeGraph sync passed.


## Cooperative search cancellation

Glob and grep now check the invocation abort signal before traversal and between
directories/files; grep also checks before opening a file, while reading lines and
before matching buffered context. A cancelled scan returns a ToolFailure with
partial output bounds and ConfirmedStop(cancellation_signalled=True), rather than
claiming an empty or complete successful search. It discards accumulated matches
on cancellation instead of presenting them as an inventory.

Used the existing invocation signal directly, without a cancellation framework or
new test cases. These are cooperative boundaries: a single directory enumeration,
file read or Python regular-expression evaluation is not preemptible; output byte
bounds and long-line costs remain pending.

Validation: 100 existing search/file tests passed (0.80s), plus a manual invocation
of both tools with an already-cancelled signal confirmed error/partial/stopped
results. Ruff, formatting, source type checking and CodeGraph sync passed.


## Search result and line bounds

Reused SDK `TOOL_OUTPUT_BYTES` as the collection budget for serialized result lists,
separate from the core's final model projection. Grep measures complete match
records, including UTF-8 text, columns and repeated context, before retaining them.
Glob conservatively charges absolute/resolved path spellings, which also bound
its shorter workspace-relative results. Both stop on either count or byte budget
and advise narrowing the search instead of attributing all truncation to count.
The budget covers the matches list, not the envelope or repeated human preview.

Grep reads at most 64 Ki characters plus one per physical line. Oversized lines
fail visibly with `line_too_long` instead of searching a prefix, fabricating line
numbers, or silently omitting matches. Together with the existing context-count
bound this bounds buffered input and occurrence lists. This is not a regex time
limit: an individual regex evaluation or OS read remains nonpreemptible.

Source review covered traversal ordering, explicit/external paths, ignore/filter
behavior, UTF-8/binary handling, lookahead, context rendering, count/byte limits,
errors and cancellation; glob/grep are now marked reviewed with these limits.
Validation: existing 100 file/search tests passed; manual UTF-8 and long-path
probes retained match lists of 49,575 and 47,237 bytes with truncation, and a long
line returned `line_too_long`. No tests added. Ruff, formatting, source type check
and CodeGraph sync passed.


## Workspace edit coordinates and diagnostic flow

Replaced `splitlines()` with CR/LF/CRLF-only splitting, preserving Unicode line
separators, NEL, vertical tabs and form feeds as ordinary LSP text. Empty input and
trailing line endings naturally retain the final editable line, removing manual
empty/trailing-line repairs. Lower-bound checks already enforced by ingress were
removed. Unique resolved document paths now sort once without repeated sets.
The protocol's end-of-line definition is documented in the official specification:
https://github.com/Microsoft/language-server-protocol/blob/gh-pages/_specifications/specification-3-14.md#text-documents

Removed the result body's unused diagnostics collection and its serialization.
Document-state queries return their prebuilt ToolFailure directly instead of
throwing/catching a custom carrier exception. Missing providers and missing
resources still permit hash-only guards; malformed answers return the same SDK
diagnostic; handler exceptions still propagate visibly.

Validation: 81 existing file/LSP/optional-query tests passed (25.75s) after the
coordinate fix; manual edits with four Unicode/control separators preserved the
first LSP line and edited the next CRLF-delimited line correctly. No tests added.
The final diagnostic-flow change passed 74 existing file/optional-query tests
(5.52s). Ruff, formatting, source type checking and CodeGraph sync passed.
Cancellation and structured partial-write results remain pending.


## Workspace edit partial commits and cancellation

Workspace edits now prepare result metadata before committing, then report only
the successfully committed prefix when a later replacement fails or cancellation
arrives between files. Failures include committed paths/hash/diff records, partial
output bounds, settled execution and explicit remaining-path guidance. Removed
the parallel mutable uncommitted-path list. Preflight checks cancellation between
edits; each file still uses the existing checked atomic replacement. There is no
multi-file transaction, rollback or replay guarantee.

Extended the existing `changes` slot to ToolFailure instead of inventing separate
partial-change metadata. Codec roundtrips and core invocation facts now preserve
committed resource changes regardless of outcome; read coverage remains
success-only. Updated the public fact contract. Historical events are unchanged.

Validation: 138 existing file/SDK/facts/hooks/optional-query checks passed (11.50s).
Manual two-file write-failure and cancellation probes both left first=new,
second=old, returned only the first file and preserved its change through codec
roundtrip. Existing failure assertions were adjusted; no tests added. Ruff,
formatting, source type checking and CodeGraph sync passed.


## Edit ingress and diagnostic simplification

Replaced the handwritten edit argument schema, mode-count switch and manual
replacement parsing with strict Pydantic single/batch/patch models and one union
adapter. The same models generate the published object schema, forbid unknown
fields (including batch items), preserve strict booleans and validate once before
filesystem reads. Frozen SDK arguments are converted once at ingress. Missing
hashes use standard validation; stale content keeps its typed diagnostic.

Removed the unused result diagnostics collection and the ambiguous-match location
hint subsystem: it allocated repeated copies of the same old-string reference,
then scanned every occurrence to return previews already supplied by the caller.
Ambiguity still reports the observed count and asks for replaceAll or a longer
unique string. Removed redundant no-op/input-echo diagnostic fields.

Cancellation checks run between prepared replacements and immediately before the
single checked commit. Final hash/diff generation now precedes writing so result
preparation cannot fail after a successful commit. Single-file batches remain
all-preflight/one-write; multi-file marker/unified patch review remains separate.
No tests added or modified. Diff formatting/output bounds still need review.

Validation: 84 existing file/core-builtins/core-vertical/optional-query tests
passed (115.56s), including restart, governance and argument rejection paths.
Ruff, formatting, source type checking and CodeGraph synchronization passed.
Implementation net change: 96 lines removed.


## Typed patch changes and marker partial results

PatchChange now flows through parsing, hash/mode checks, deduplication, summaries
and SDK result projection. Removed ad hoc change dictionaries, internal dictionary
revalidation, an unused diagnostics collection and custom deduplication; immutable
dataclass keys use `dict.fromkeys` to preserve first-seen order. Marker and unified
paths share a small result constructor.

Marker commits record each completed operation immediately. A later OSError or
cancellation returns the actual committed prefix via body and SDK changes with
partial output bounds. A move whose destination was created but source unlink
failed reports a create, not a completed move. Cancellation is checked between
marker hunks/operations and before unified git apply; running git subprocesses
remain a separate ownership/cancellation audit item. No rollback mechanism added.

Validation: 86 existing file/codec/fact tests passed (0.74s). Manual second-write
failure and cancellation probes reported only the first update; a source-unlink
failure reported only destination creation with source retained. Adjusted the
existing concurrent-write case for returned ToolFailure; no tests added. Ruff,
formatting, source type checking and CodeGraph sync passed. Net implementation
reduction: 44 lines. Parser and unified post-apply verification remain pending.


## Remove unified patch post-apply policy and error reconstruction

Deleted four mode-only helpers (detection, expected-mode parsing, pre-apply mode
snapshots and post-apply checks). Normal parsed changes already include these
files, and git apply owns mode application. The extra check could reject a
successfully applied patch after mutation and discard its resource changes.
No special mode-only acceptance path is needed when parsed changes are empty.

Removed git-error line-number extraction, patch-context echo and invalid-patch
example formatting. Git's original error is now the diagnostic message; existing
brief guidance and affected paths remain. Removed the duplicated git-error detail
field. This avoids inferring meaning from vendor error prose or echoing the
caller's patch back as a new observation.

Validation: 86 existing file/codec/fact tests passed (0.84s), no tests changed or
added. Manual real-git and actual execute_patch probes with core.filemode=true
and false both changed 0644 to 0755 and reported the update. Ruff, formatting,
source type checking and CodeGraph sync passed. Implementation net reduction:
156 lines. Parser and unified subprocess ownership/cancellation remain pending.


## Patch grammar and native unified input

Removed the custom unified-patch normalizer and its path re-quoting/mode-header
insertion helpers. Git now receives the caller's unified diff unchanged after
optional heredoc unwrapping; metadata parsing uses the same input without a
second normalization pass. Native mode-only diffs do not require artificial
---/+++ headers. Heredoc extraction preserves the body's final newline.

Fixed the unreachable marker `*** End of File` branch: chunk scanning now sees
the sentinel before treating other *** lines as operation boundaries. Invalid
chunk headers and unprefixed update lines now fail rather than silently vanish.
Marker framing no longer rescans for first/last nonblank lines; CRLF marker
transport is normalized before chunk parsing.

Path parsing preserves filename whitespace and decoded quote characters rather
than stripping/dropping them. This keeps expected-hash lookups aligned with git's
actual target, including trailing-space filenames.

Validation: 86 existing file/codec/fact tests passed (1.37s), no tests changed or
added. Manual probes verified repeated EOF matching over CRLF patch input,
rejection of invalid update lines without writes, native space-containing mode
paths, heredoc unified input, and trailing-space filename stale-hash rejection
followed by a valid update. Ruff, formatting, source type checking and CodeGraph
sync passed. Net implementation reduction: 87 lines. Remaining native path-parser
reconciliation and subprocess ownership are not yet marked complete.


## Marker-only patch scope; remove Git dependency from file editing

Following the user's request to reconsider complexity and the Git requirement,
removed unified-diff input entirely. `edit` now advertises only marker patches;
`execute_patch` has one parser/planner/commit path. Removed all Git execution,
unified path/escape decoding, secondary metadata parsing, format detection and
heredoc wrapping. Removed the unidiff dependency and its isolated lock package.
Unified diff output previews elsewhere remain supported. Git-based review tools
are separate product features; this change removes Git from basic file editing.

Marker add/update/delete/move operations retain exact context/hash checks,
preflight staging, per-file atomic replacements, cancellation and actual-prefix
failure results. Prepared chunks already follow document order, so reverse that
sequence directly rather than sorting whole replacement tuples (which could
reorder insertions at the same offset by text). The module shrank from 820 to
503 lines. Deleted obsolete unified-input test cases; no tests added.

Validation: 88 existing file/codec/fact/core-builtins/optional-query tests passed
(7.49s). A manual marker batch completed add/update/move/delete with PATH empty;
unified input was rejected explicitly. Ruff, formatting, source type checking,
CodeGraph sync and `uv lock --check --offline` passed (96 packages). Lock removal
was pruned from the existing resolved graph after offline regeneration could not
access unrelated cached provider metadata; the resulting lock passed uv's check.
The retained marker implementation is now marked reviewed. This does not complete
the broader capabilities audit.


## Standard edit diff and one batch result

Removed the custom indentation trimmer; diffs now retain actual indentation.
Used stdlib StringIO universal-line iteration without newline translation so
Unicode separators remain content. Standard difflib output marks a missing final
newline explicitly. Addition/deletion counts skip only the two actual file header
records, counting changed content that starts with ++ or -- correctly.

Removed recursive per-step batch diff results and the redundant applied-count
field. A batch preflights entirely in memory and writes once; its final diff and
hash describe the committed file. `match_count` now means actual replacements in
both single and batch modes, rather than counting batch entries. Existing batch
assertions were adjusted; no tests added.

Validation: 82 existing file/codec/fact checks passed (0.85s). Manual indentation,
header-like content, final-newline-only change and Unicode-separator probes
returned correct counts and faithful text. Ruff, formatting, source type checking
and CodeGraph sync passed before an unrelated concurrent SDK commands edit was
observed. That change is not part of this batch. Implementation net reduction:
65 lines. Final result output-bound reconciliation remains pending.


## Gateway endpoint authority and route simplification

Zen/Go Anthropic adapters no longer override an explicit provider base_url with a
pinned public gateway URL. The native Anthropic transport already strips a final
/v1 before its SDK appends /v1/messages, so provider defaults and user endpoints
can follow the same path. Google/chat routes retain the resolved endpoint too.
Removed route-specific URLs, endpoint replacement branches, the now-redundant
ModelRoute wrapper, Zen's copied route map and stale comments. Routing now maps
upstream API identifiers directly to the existing typed wire vocabulary.
Unknown protocols still fail visibly before client creation; aliases, per-request
session headers, gateway keys, timeouts and Go feedback policy are retained.

Validation: 104 existing lifecycle/errors/dispatch/data tests passed (11.86s).
Manual Zen/Go Anthropic construction preserved a custom host/path and let native
normalization remove /v1. No tests added; existing private route arguments were
updated. Full source type checking now passes after concurrent SDK changes;
Ruff and CodeGraph sync passed. External vendor protocol/header assumptions and
Go's model-specific feedback convention still need reconciliation.


## SQLite observations use a transaction, not a physical-file hash

Schema discovery and the selected query now run in one explicit read transaction,
closed with the existing connection cleanup. Removed source_sha256 and the sole
whole-file hashing helper: a SQLite observation can include WAL state that is not
in the main database bytes, and hashing the path after the query cannot establish
the observation's version. The read projection already accepts an absent source
hash as unknown. This also removes an unbounded whole-database scan after an
otherwise bounded query. Empty table names remain distinct from no selected table.

Validation: 94 existing format/file/artifact tests passed (0.99s); no tests added
or modified. Manual WAL probes showed unchanged main-file bytes while rows changed,
and no misleading source hash in either view. A writer commit injected between
schema discovery and row selection remained invisible to the read snapshot, while
the writer saw its new row. An explicitly selected empty-name table returned its
row. Ruff, formatting, targeted source type checking and CodeGraph sync passed.
Other concurrent SDK/product changes were preserved outside this batch.


## Recover clipped PDF text through the same artifact contract

Both file and artifact PDF views now spill the full extracted text whenever the
preview clips a line, including texts between the 32 KiB preview budget and the
50 KiB general output budget. Previously those texts lost their full-text field
without receiving a text_reference, so a clipped tail had no extracted-text entry
point. Existing line cursors and partial/truncated flags remain unchanged; the
full text artifact provides access to clipped line content.

PDF source and extracted text now share the source snapshot's temporary directory,
removing a nested directory lifetime. The extractor's timeout/cancellation/
kill-and-wait cleanup is retained. Removed redundant local reference variables in
the format-view renderer. No new dependencies or test cases.

Validation: 94 existing format/file/artifact checks passed (0.90s). Manual file
and artifact entry probes with a scripted extractor returned complete 42,000-byte
UTF-8 text artifacts while reporting preview clipping. Ruff, formatting, targeted
source type checking and CodeGraph sync passed. Notebook image/pagination and the
remaining read-tool source review are still pending.


## Notebook output completeness and local text budget

Moved notebook text-budget accounting into a local closure, removing mutable
single-element budget/flag lists and their repeated plumbing. String-list joining
now only validates/joins: the source was already bounded and decoded from one
JSON byte snapshot. Removed redundant key-to-string coercion after JSON parsing.

Notebook output records now retain an images list, rather than overwriting one
image slot when the MIME bundle contains several supported image formats. The
existing global spill pass updates every retained record to its artifact reference.
Unsupported MIME output now also sets the incomplete/truncated flag; its complete
original bytes remain available through the existing source_reference mechanism.
Renamed the private image check to describe its actual signature-only scope;
this is format identification, not a full pixel/container decoder.

The retained format-view source review covers read-only SQLite snapshot/budgets,
notebook source/output pagination and raw recovery, MIME records/signatures,
PDF immutable source/temp ownership, timeout/cancellation and clipped-text recovery.
read_formats.py is marked reviewed; the larger read_tool.py audit remains open.
Validation: 94 existing format/file/artifact checks passed (0.92s), no tests changed
or added. A manual read with PNG/JPEG signature fixtures and an unknown MIME kept
two independent image references, reported partial output and preserved the exact
raw notebook artifact. Ruff, formatting, targeted type checking and CodeGraph sync
passed. Net implementation reduction: 7 lines; no new dependencies.


## Read ingress uses strict field constraints

Replaced eight custom numeric validators with Pydantic Field bounds and one strict,
extra-forbidding configuration. Removed mixed StrictInt/coercing-int behavior.
Published limit/output-limit maxima now also apply at direct capability ingress.
The whitespace-aware path validator remains explicit.

ReadTool now validates the complete argument mapping instead of rebuilding a
whitelist dictionary that silently discarded unknown fields. This makes unknown
field rejection effective and removes the duplicate field inventory at invocation.
Optional None values retain their existing default-selection semantics.

Validation: 94 existing format/file/artifact checks passed (0.98s), no tests changed
or added. Manual calls with boolean offset, string limit, unknown field and
limit=2001 all failed validation before resolving a nonexistent file. Ruff,
formatting, full source type checking and CodeGraph sync passed. Implementation
net reduction: 66 lines. Remaining read projection/metadata review stays open.


## Typed directory observations and bounded selection

Directory trees use a frozen internal dataclass instead of generic dictionaries,
assertions and int/float fallback accessors. Scandir observes each entry's metadata
once; standard-library heap selection retains only the requested most-recent
candidates instead of sorting/materializing the whole directory. The listing
observes symbolic links themselves and does not traverse their targets, retaining
dangling links without treating them as missing target files.

Filesystem enumeration/stat errors now propagate visibly rather than becoming
empty directories or zero timestamps. Every nested omission contributes to the
partial flag; global-budget omissions count actual undisplayed entries, and
uninspected directories stay unexpanded instead of appearing empty. Guidance no
longer attributes every incomplete listing to a fixed entry count.

Validation: 94 existing format/file/artifact checks passed (0.91s), no tests added
or changed. Manual nested overflow, exhausted budget, dangling-link and injected
permission-error probes matched the stated behavior. Ruff, formatting, targeted
source type checking and CodeGraph sync passed. A concurrent core/processes.py
rewrite temporarily failed global type checking outside this change; that source
was preserved separately. Directory cancellation and remaining read projections
are still pending audit.


## Archive member identity and bounded reads

ZIP member selection now uses the chosen ZipInfo object instead of validating one
record and resolving its filename again (which selected the last duplicate).
ZIP/TAR both use last matching record semantics, aligned with listing metadata.
Their shared byte reader reads at most MAX_ARCHIVE_MEMBER_BYTES+1 and checks the
actual result, closing the member stream on every path. TAR non-regular members
fail explicitly rather than falling through to an older duplicate or following
an archive link. No extraction to workspace paths is performed.

Member normalization preserves filename whitespace. Archive-suffix detection
finds the first suffix actually followed by colon/end, so an earlier `.zip` inside
a directory name no longer hides a later real archive suffix.

Validation: 94 existing format/file/artifact checks passed (0.91s), no tests added
or changed. Real ZIP/TAR manual probes confirmed last duplicate content, rejection
of an oversized duplicate, preserved trailing-space names and correct detection
under a folder.zip-cache prefix. Ruff, formatting and targeted source type checks
passed. Archive metadata enumeration limits and clipped member-line recovery
remain pending; the entire archive path is not yet marked reviewed.


## Recover clipped archive-member text with existing artifacts

Archive text previews now distinguish clipped line content from ordinary page
limits. Only when an emitted line is clipped, the existing artifact service stores
the complete bounded member text; OutputBounds carries its reference and the
model-visible summary gives the read URI. Existing line cursors still navigate
complete subsequent lines, while artifact byte reads recover the clipped tail.
No new pagination scheme or duplicate body reference fields were introduced.
ZIP/TAR share the same path; short members avoid extra artifacts. Removed a
redundant directory predicate and duplicate output-bound argument plumbing.

Validation: 94 existing format/file/artifact checks passed (1.46s), no tests changed
or added. Manual real ZIP/TAR calls clipped a multibyte long line, then recovered
the exact complete text through the returned artifact's byte view. A short-member
read added no spill. Ruff, formatting, targeted source typing and CodeGraph sync
passed. Archive metadata bounds/mode semantics remain pending.


## Native file data and hash share one observed stream

The file renderer opens one binary descriptor, sniffs/rewinds it, and either reads
a bounded attachment/binary payload or wraps it in stdlib TextIOWrapper with no
newline translation. Text hashing occurs while observing every decoded line, so
UTF-8 BOM and CR/LF/CRLF bytes remain exact and returned lines cannot be tagged
with the hash from a separate path open. This protects replacement races; it is
not an atomic filesystem snapshot against in-place concurrent writes.

Merged attachment/binary read/hash/spill/bounds setup. Actual bounded reads replace
attachment stat-then-unbounded-read, and binary reads now enforce their existing
50 MiB constant. Removed a separate sniff open and whole-file hash pass. Whole-line
page boundaries no longer incorrectly mark emitted ReadObservations as clipped.
Read ingress and text line loops now return explicit confirmed cancellation.

Validation: 97 existing format/file/artifact/coverage checks passed (1.09s). Manual
atomic path replacement preserved one descriptor's body/hash/BOM/mixed line endings;
binary overflow was rejected, whole-line byte paging remained partial but unclipped,
and a pre-cancelled request failed before resolving a nonexistent file. The existing
cancellation test was adjusted to the explicit failure result and lost its unused
SQLite setup; no tests added. Ruff, formatting, targeted type checks and CodeGraph
sync passed. Net implementation reduction: 30 lines. Native clipped-line recovery,
archive metadata and remaining projections are still pending.

## Tool documentation and transcript query ingress

Removed historical sidecar guidance wording and repeated schema formatting from
`read_tool.py`. Documentation uses the admitted, visible tool definition directly;
its typed description no longer gets a synthetic fallback.

Transcript queries retain blank values so empty and duplicate parameters cannot
silently become default cursors. Malformed query syntax, non-decimal numerals and
integers exceeding Python's conversion limit return explicit URI failures before
reading events. Removed the redundant parameter-name/target-name tuple mapping.

Validation: Ruff and targeted source type checking passed; 77 existing file and
artifact tests passed. Six manual malformed-query probes (blank, superscript,
duplicate with blank, missing equals, unknown blank key and 5000-digit integer)
were rejected before event-store access. No tests added. Remaining transcript
projection/bounds and artifact paths still require source review.

## Artifact page boundaries

Artifact line pages previously counted the unfinished line at the byte-window
boundary as complete. Continuing with the advertised line offset skipped its
unread tail. Non-final windows now retain only complete lines; when one line
exceeds a whole window, the existing byte-read path supplies a byte cursor
instead. No new pagination protocol or helper abstraction was introduced.

Byte pages use the standard incremental UTF-8 decoder to defer an incomplete
trailing character. Their cursor and consumed byte count exclude those deferred
bytes, so subsequent pages preserve valid multi-byte text instead of producing
replacement characters at every window boundary. Arbitrary invalid bytes still
use replacement decoding, as before.

Validation: Ruff, targeted source type checking and 77 existing file/artifact
tests passed. Manual probes reconstructed a 40,000-character Chinese line plus
following lines across windows without loss, confirmed the preceding complete
line advances by line cursor, and verified a four-byte window consumes one
three-byte character and resumes at byte three. No tests added. Artifact full
read cancellation/size accounting, duplicate image spills, and remaining result
metadata are still pending review.

## Reuse existing image artifacts

Removed full-content loading and duplicate spilling for image artifacts above
the inline attachment limit. The first bounded store page already provides the
immutable artifact identity, media type and total size; the attachment can use
that existing reference directly. Small images still load bounded content for
inline data URIs. The binary full-read ceiling now applies only when bytes are
actually materialized, rather than refusing an already-stored large reference.

Updated the existing large-image test to require reuse rather than a second
spill; no test cases added. Ruff and targeted source type checking passed, and
97 existing file/format/artifact/coverage tests passed. A manual store probe
refused any second read and confirmed large-image output uses one bounded page,
the original identity and no additional spill. PDF/full-artifact loop
cancellation and remaining read result semantics still need review.

## Raw archive reads and ignored selectors

Explicit `view="raw"` now bypasses archive auto-detection and unsupported-codec
rejection. ZIP/TAR and unsupported archive formats can therefore be read as
ordinary files, matching the existing raw-view contract rather than silently
listing their members or demanding a codec. Member-address paths still require
the archive view; raw mode names an actual filesystem path.

Archive views reject SQLite/notebook/PDF views and selectors instead of silently
ignoring them. Directory trees reject derived format selectors and explicit
line offset/limit parameters, which their fixed bounded tree renderer never
used. Default directory behavior and archive line pagination are unchanged.

Ruff, targeted type checking and 97 existing file/format/artifact/coverage tests
passed. Manual probes confirmed raw ZIP and unsupported 7z bytes use the binary
file branch, and five incompatible archive/directory selector combinations fail
explicitly. No tests added. Archive metadata collection bounds and directory
mid-scan cancellation remain pending.

## One authoritative read argument schema

Removed the handwritten read JSON schema. `ReadArgs.model_json_schema()` now
publishes the same field types and numeric limits used at invocation ingress;
field descriptions live alongside those constraints. Artifact page and notebook
output ceilings no longer have independently maintained numeric copies.
Corrected the transcript description to reflect that readable prompts and tool
results are included. Generated optional/null schemas now reflect the runtime's
already-accepted optional fields. No schema translation layer was added.

Net source reduction: 38 lines. Ruff, full source type checking and 97 existing
file/format/artifact/coverage tests passed. Manual validation checked the thawed
SDK wire schema against JSON Schema and compared valid arguments plus five
invalid bound/unknown-field cases with Pydantic ingress. No tests added.

## Derived-view output recovery metadata

When a serialized SQLite/notebook/PDF view exceeds the output budget, its
existing full-view artifact is now also attached to `TextOutput.bounds.reference`.
Previously only prose and the opaque body exposed this recovery reference, so
standard output consumers could not discover it. Reused one UTF-8 encoding for
the size check and preview slicing. Source snapshot and extracted-text references
remain distinct from this complete rendered-view reference.

Ruff, full source type checking and 97 existing file/format/artifact/coverage
tests passed. A real 200-cell notebook with 300-character sources produced a
bounded preview whose standard reference resolved to the complete 200-cell JSON
view. No tests added. Native clipped-line recovery and remaining read lifecycle
review are still pending.

## Exec factory and service contract

Reviewed the shared exec factory and its optional plugin adapter. The factory
creates stateless shell/background tools and registers the pure deactivation
transform; it owns no resource lifetime or independent configuration. Corrected
its module description to identify shared capability ownership. Existing
nonempty argument validators remain small and preserve field-specific messages.

Deleted the background tool's seven-name `getattr`/`callable` service preflight
and its helper. Those methods are required by the typed SDK `PluginServices`
contract; core implementations supply them, and invocation code calls the
required methods directly. This removes a second manually maintained service
interface and unnecessary per-invocation reflection. Deleted the obsolete test
that deliberately passed objects outside that interface; no tests added.

Ruff, formatting, full source type checking and 88 existing background/shell
tests passed. Net reduction: 18 production lines and 15 test lines. Log scan
cursors, readiness cancellation and remaining lifecycle semantics still need
review; this does not mark all execution capabilities complete.

## Background readiness cancellation

Readiness polling now checks the live invocation cancellation signal before each
stream read. Previously `_start` captured a boolean only before spawning, so a
later cancellation could leave the call waiting the whole 30-second timeout.
The start path now passes the invocation context instead of a stale boolean.

Cancellation ends only the readiness wait. The already-started supervised
process keeps its persisted identity and normal explicit stop control; success
still reports the actual start and current handle. `ready=None` records that
readiness was not determined, and prose distinguishes cancellation from a false
pattern timeout. No new state fields or cancellation mechanism were added.

Ruff, formatting, full source type checking and 88 existing background/shell
tests passed. A manual scripted service cancelled after the first stream read
with a 30-second requested timeout: start returned in under one second, retained
the persisted identity, made only one output read and never called process stop.
No tests added. Log cursor/scan bounds and other operation cancellation remain
pending review.

## One authoritative background-process argument schema

Deleted the hand-maintained five-operation JSON schema. The existing strict
discriminated Pydantic union now generates the published schema, with the root
object wrapper already used by edit. Operation/field descriptions moved onto
the models, including the inherited process identity field. Numeric bounds,
defaults and empty-input constraints therefore have a single definition.
Optional null fields now accurately match invocation ingress. Renamed one
existing test to describe the current operation contract rather than legacy
compatibility; no test cases added.

Production source is 80 lines shorter. Ruff, formatting, full source type
checking and 88 existing background/shell tests passed, including generated
schema admission and operation validation. Manual probes checked all five
valid operations and four invalid limit/empty/unknown-field combinations
against both the wire JSON schema and runtime union. Background lifecycle and
log scan cursor review remain incomplete.

## Background result output bounds

Process lists now disclose how many rows the existing 64-row limit omitted and
mark standard output bounds partial/truncated accordingly. Previously the list
reported a successful 64-row result with no indication that more processes
existed. No body fields or pagination mechanism were added.

Log results now project their existing truncation status onto standard
`TextOutput.bounds`, matching the body rather than implicitly claiming complete
output. Existing semantics count skipped leading lines as a partial view.

Ruff, formatting, full source type checking and 88 existing background/shell
tests passed. Manual probes verified 70 processes yield 64 rows plus an explicit
six-row omission and partial bounds; one-line and whole-log reads had matching
body/output truncation states. No tests added. The 8 MiB log scan ceiling still
needs cursor/recovery review; output metadata consistency alone does not solve
that separate limitation.

## Shell ingress schema and service dispatch

Shell arguments now generate the published schema directly from `ShellExecArgs`,
with descriptions beside their validation constraints. Removed the separate
JSON field/type/timeout table. Generated defaults/null acceptance describe the
existing runtime behavior.

The shell calls the mandatory typed `PluginServices.run_process` method directly
instead of rediscovering it with `getattr` and checking callability on every
invocation. Deleted the obsolete test that passes `None` outside the SDK service
contract. No tests added. Core-owned process execution, timeouts, capture limits,
artifact handling and command interception are unchanged.

Ruff, formatting, full source type checking and 87 existing background/shell
tests passed. Manual schema probes agreed with ingress for valid default/max
timeouts and nullable descriptions, and rejected zero/over-limit/bool timeouts
and unknown fields. Shell interception policy and progress boundary review remain
pending; this is not a completed shell source audit.

## Anthropic request assembly and tool/cache gating

Deleted the private `_wire` materialization: its only caller used the tools list
and discarded both canonical request serialization and prefix/tool hashes. It
also converted the full history a second time. Request assembly now constructs
tools directly after catalog capability gating and converts history once. No
public SDK wire contracts were removed. Production source is 26 lines shorter.

Fixed cache retention reintroducing tools on models whose catalog explicitly
says `supports_tools=False`. Cache control now modifies only the tools already
selected for the payload, or uses the system block when tools are absent.

Ruff, formatting and full source type checking passed. The initial test run
could not bind loopback sockets in the sandbox; rerunning the same six existing
provider test modules with local socket permission passed all 136 tests in
22.14 seconds. Manual request probes confirmed one history assembly and correct
tool/cache gating for both supported and unsupported tool metadata. No tests
added; no live vendor credentials or external service requests used. Anthropic
stream/replay/usage and transport details remain pending full reconciliation.

## OpenAI unused wire fingerprints and SDK types

OpenAI request construction no longer serializes system-prefix/tool snapshots
or computes SHA-256 fingerprints with no consumer. Its private materialization
wrapper is replaced by a tools-only projection; messages go directly into the
payload. Tool projection is skipped when catalog metadata disables tools.
Existing tool name/schema transformations and history conversion are retained.

After removing the last consumer, deleted `ProviderWireMaterialization` and
`WirePrefixDescriptor` from the SDK. Repository-wide source/test searches found
no remaining references. These structures were internal adapter intermediates,
not fields in persisted turn requests or provider protocol methods. No
compatibility aliases or replacement abstractions were introduced. Combined
production source reduction: 41 lines.

Ruff, formatting and full source type checking passed. Six existing provider
modules plus SDK dependency-direction checks passed 140 tests in 22.85 seconds
with local loopback socket permission. No tests added and no live vendor calls
used. Remaining OpenAI schema/replay/transport semantics still need review.

## Preserve tool schemas and respect Google model tool capability

Removed OpenAI's legacy flat-property-map conversion. A tool already declares
a JSON schema; the adapter now preserves it and only supplies a missing root
object type, as Anthropic does. Previously valid root `oneOf`/`anyOf` schemas
without explicit root properties were rewritten as property names, destroying
their constraints. No compatibility fallback remains.

Google now consults catalog tool capability before constructing function
declarations. Explicit `supports_tools=False` sends no tools, while unknown
capability retains the existing permissive behavior. This matches OpenAI and
Anthropic and avoids sending unsupported declarations.

Ruff, formatting, full source type checking and 136 existing provider tests
passed (22.10 seconds, local loopback permitted). Manual probes preserved a
root `oneOf` schema and its valid/invalid instances, and checked Google true,
false and unknown capability behavior with identical declaration schemas. No
tests added. Full adapter/replay/usage and Google OAuth review remain pending.

## Anthropic streamed argument accumulation

Removed the private accumulator's unused `started` flag. Argument fragments now
append to a per-call list instead of rebuilding an immutable tuple and data
object for every delta; final parsing still joins once. This avoids quadratic
fragment-reference copying for long streamed arguments without adding a buffer
abstraction. Fragment ordinals remain zero-based and contiguous.

Tool name reverse mappings now compute once per stream instead of rescanning
the full request for every tool start and again at stream completion. The
accumulator is private transient state; persisted SDK events remain unchanged.

Ruff, formatting, full source type checking and 115 existing streaming, usage,
lifecycle, error and thinking tests passed in 10.42 seconds (local loopback
permitted). A scripted 3012-fragment response preserved exact final arguments,
call identity, all fragment ordinals and the terminal tool-call reason. No tests
added and no live vendor calls used. Remaining malformed-stream, thinking replay
and transport reconciliation are still pending.

## OpenAI streamed argument accumulation

OpenAI's private transient accumulator now appends fragments to a list instead
of recreating a tuple and dataclass on every delta. The used `started` and
`explicit_streaming` fields remain: they govern partial-call versus completed
call event emission. The reverse tool name mapping is built once per stream
instead of once per tool delta. Final validation, event ordering, conflict
checks, legacy complete-call output and fragment ordinals are unchanged.

Ruff, formatting and targeted provider type checking passed. A full source type
check encountered 17 missing EventType diagnostics in concurrent unstaged core
event changes; those files are outside this batch. Existing provider streaming,
usage, lifecycle, error and thinking tests passed 115 tests in 10.62 seconds
with local loopback permitted. A 3012-fragment scripted response produced one
start event, preserved original fragment ordinals/call identity and reconstructed
the exact input object. No tests added or live vendor calls used.

## Anthropic frozen replay metadata and message merging

Thinking replay now thaws SDK-owned message metadata through the shared JSON
projection before inspecting its block list. SDK metadata freezes lists into
tuples; the old list-only check silently discarded supplied signed blocks and
could also leave nested mapping proxies unsuitable for wire JSON. The existing
opaque signature/content values are preserved without constructing reasoning.

Removed the separate assistant merge branch: thinking and ordinary blocks now
use the same role-merging path, in that order. Removed the already-handled
assistant-role check from the remaining user branch. Source is 16 lines shorter.

Ruff, formatting and targeted provider type checking passed. Existing provider
streaming, usage, lifecycle, error and thinking modules passed 115 tests in
11.35 seconds with local loopback permitted. A manual frozen-metadata probe
confirmed signed thinking precedes tool use, tool feedback/user text merge in
order, wire JSON serializes, and source metadata remains unchanged. No tests
added. Important remaining gap: the adapter's response/stream paths do not yet
record signed thinking blocks themselves; this fixes supplied metadata replay,
not complete signed-thinking round trips or redacted-block handling.

## Anthropic signed-thinking response round trips

Completed the previously identified response metadata gap. Non-stream responses
preserve returned thinking/redacted-thinking blocks in existing `thinking_blocks`
metadata. Streams collect thinking text by block index and retain opaque
signature deltas and redacted block data; their terminal metadata uses the same
shape. Replay now accepts both block types. No new SDK fields or storage path
were introduced. The core already persists provider metadata through
`encode_provider_result` and restores it into assistant message metadata.

Protocol evidence: [Anthropic thinking documentation](https://platform.claude.com/docs/en/build-with-claude/thinking)
requires opaque thinking signatures and redacted blocks to be returned unchanged
for tool continuations; signature deltas arrive before content block stop.
Installed SDK ThinkingBlock, SignatureDelta and RedactedThinkingBlock types
confirm the fields. Text fragments append to lists and join once; signature
fragments concatenate in arrival order without interpretation.

Ruff, formatting and targeted provider type checking passed; 115 existing
provider tests passed in 11.11 seconds with local loopback permission. Manual
scripted API/stream responses yielded identical signed/redacted metadata and
next-request blocks, including split signatures, initial thinking text, tool
use and SDK-frozen metadata. A provider-result codec round trip also retained
the blocks. Signatures were not emitted as visible text. No tests added and no
live vendor calls used. Malformed stream index/duplicate block policy and
remaining adapter transport/usage reconciliation are still pending review.

## Anthropic stream index ingress and non-placeholder tool state

Content start/delta events now require an explicit non-negative integer index;
missing, string, boolean and negative values no longer silently address block
zero. Repeated tracked tool/thinking start indices fail rather than replacing
previous blocks. Tool input deltas must reference an existing started call,
instead of manufacturing an incomplete accumulator and rejecting it later.

With that invariant, accumulator identity/name/initial input are required typed
fields. Removed nullable placeholder defaults, final missing-name/input checks,
conditional delta identity emission and repeated final ID normalization. Net
production reduction: 11 lines. This validates the tool/thinking states actually
retained; it does not introduce a complete content-event state machine.

Ruff, formatting and targeted provider type checking passed. Five existing
provider modules passed 115 tests in 11.44 seconds with local loopback
permission. Manual streams rejected four malformed index forms, an orphan input
delta and a duplicate tool start without emitting completed calls or success;
a valid explicit empty input still completed correctly. No tests added. General
stream terminal ordering, transport ownership and usage reconciliation remain
pending source review.

## Anthropic SDK stream close ownership

Removed the generator wrapper around SDK streams. The shared timeout pump now
receives the actual SDK iterator and can invoke its `close()` directly even
while a worker is blocked reading. Previously closing an executing wrapper
generator could fail without closing the SDK response, leaving a cancelled
request's connection until its transport timeout. SDK stream API exceptions now
map through the existing provider error boundary using the same error payload
extractor; initial request errors retain their existing transport mapping.

Installed Anthropic Stream implementation confirms it is an iterator with a
response-closing `close()` method. No new stream wrapper/resource abstraction
was added. Ruff, formatting and targeted type checking passed; 115 existing
provider tests passed in 11.44 seconds with local loopback permission. A manual
blocking SDK stand-in returned by transport identity closed on mid-read
cancellation in under one second. A real SDK RateLimitError preserved status
429, retry-after headers and rate_limit classification. No tests added or live
vendor calls used. OpenAI has a similar wrapper plus separate SSE error
normalization that still needs reconciliation.

## OpenAI SDK stream close and error boundaries

Removed OpenAI's SDK stream generator wrapper so the timeout/cancellation pump
closes the actual SDK iterator while its worker is blocked. The existing provider
error boundary now handles SDK API exceptions with status/header extraction,
and JSON/Pydantic decoder failures through the existing bounded stream-error
payload. Mapping decoded non-object events remains at the chunk boundary.

This also avoids the previous wrapper's catch-all classification of arbitrary
stream exceptions, including raw network failures, as invalid JSON events.
Known network exceptions retain network failure classification; unknown errors
use the ordinary bounded provider failure. No new stream class or ownership
mechanism was introduced.

Ruff, formatting and targeted provider type checking passed. Six existing
provider modules passed 136 tests in 22.59 seconds with local loopback permitted.
Manual probes confirmed SDK stream identity, direct close on blocked-read
cancellation in under one second, HTTP 429 classification, and parser-error
redaction of private document/message strings. No tests added or live vendor
calls used. Broader native adapter source reconciliation remains pending.

## Model catalog typed ingress without field-by-field conversion

ProviderModelMetadata now uses the existing Pydantic validated-dataclass path,
as ContextPriceTier already did. Reusable constrained scalar annotations define
positive integer limits, strict optional booleans/nonempty strings and finite
nonnegative prices. String collections accept JSON arrays and retain immutable
tuples internally. Removed six permissive conversion helpers, the duplicate
allowed-field table and the 29-field manual construction map. Model-specific
loader errors identify provider/model; absent optional facts still remain None.

Wrong types, negative/nonfinite prices, unknown fields and malformed list items
now fail rather than quietly becoming unknown or being filtered out. This is
packaged data ingress, not a runtime compatibility path. No new dependency. Net
production source reduction: 68 lines. The dev-time generator already validates
its output through this runtime loader before writing a snapshot.

Ruff, formatting and targeted type checking passed. Existing data/generator/
thinking/usage tests passed 42 tests, and auth/catalog/dispatch/panels/streaming/
reasoning/lifecycle/error consumer tests passed 157 tests (38.74 seconds, local
loopback permitted). Compared every packaged row against the committed loader:
all 1170 model payloads were identical. Six malformed row probes failed visibly;
False, zero price and missing/unknown values stayed distinct. No tests added.
The broader data provenance/consumer audit and tokenizer disposition remain
pending; this establishes loader behavior, not full capability completion.

## Catalog identity and packaged snapshot contract

Retained documented case-insensitive catalog matching while making empty or
colliding normalized provider/model keys explicit ingress errors. Previously
`Model` and `model` rows could silently replace each other. Uses the existing
result maps for collision detection; no additional registry or alias mechanism.

Completed the packaged JSON data contract review alongside the generator,
workflow and consumers: 35 providers/1170 model rows validate; limits/capability
flags feed native requests, `api` feeds routed-wire selection, effort fields
feed thinking controls, prices/context tiers feed report-time pricing, and
model identifiers feed catalog panels. Generator output is validated by the
same runtime loader before writing; the daily GitHub workflow generates and
tests a snapshot PR, while runtime reads only the bundled resource. No local
per-model override table is reintroduced. This review does not assert freshness
or exact equality with a live upstream API response.

Ruff, formatting and targeted type checking passed; 42 existing data/generator/
thinking/usage tests passed in 2.82 seconds. Manual probes verified every
normalized lookup returns its original metadata object and four empty/collision
shapes fail visibly. No tests added. Original upstream casing is still folded
by the generator under the existing documented catalog contract; vendor wire
model names remain preserved by request/config paths.

## Share exact edit diff rendering

Workspace edit now reuses `edit_tool.summarize_diff` rather than independently
calling difflib with `str.splitlines`. This removes a second diff-rendering path
and preserves Unicode line separators inside file lines. Missing final newlines
receive the same explicit marker already used by ordinary edit. Both diff
headers use the displayed workspace path, consistently with the shared helper,
rather than the old workspace-only synthetic a/b prefixes. No new helper module
or dependency was introduced.

Model output for both successful edit paths is a short summary; complete diffs
remain in structured bodies for clients/history, so they do not require a new
model-output clipping/spill layer. Workspace partial failures already report
only committed prefix changes and partial standard output bounds.

Ruff, formatting and targeted type checking passed; 97 existing file/format/
artifact/coverage tests passed in 1.75 seconds. A real workspace edit over
`a<U+2028>b` without final newline produced one original and replacement line,
with missing-newline markers on both, and the intended file bytes. No tests
added. Workspace version-field/ingress reconciliation and remaining native read
recovery/lifecycle review still need completion.

## Workspace-edit single ingress validation

Removed the manual raw-edits loop that checked only whether expectedHash was a
string before the Pydantic model ran. The generated schema and `_TextEdit`
already require this string; missing and wrong-type hashes now use ordinary
strict model validation alongside all other malformed fields. This removes a
second validation path and its special missing-hash diagnostics. Stale content
hash/version checks after validated ingress remain classified diagnostics.

The existing missing-hash frozen-arguments test now asserts model rejection; no
test cases added. Ruff, formatting and targeted type checking passed, and 97
existing file/format/artifact/coverage tests passed in 2.02 seconds. Production
source is 22 lines shorter. Workspace version-field reconciliation remains
pending; no compatibility behavior was added.

## Workspace version field and edit audit reconciliation

Removed the duplicate expectedVersion alias and its precedence rule; the
existing LSP `version` field is the sole optional version constraint. Updated
existing unit and integration callers. Source changes were incorporated by
concurrent commit `15ebd391`; this audit record does not duplicate that commit.
Historical changelog entries describe their original release and remain intact.

Revalidated the current worktree: full source type checking passed; 97 existing
file/format/artifact/coverage tests passed in 2.04 seconds. Two existing LSP and
optional workspace-query integration modules passed all 10 tests in 22.88
seconds after granting local loopback access (the first sandboxed run failed
only at socket creation). A manual ingress/schema probe confirmed only version
is published, expectedVersion is rejected as an unknown field and version is
accepted. No tests added.

Completed the remaining ordinary edit/workspace-edit source reconciliation: all
shape validation precedes filesystem edits; hash/document constraints remain
explicit; coordinate encoding and overlap checks precede checked commits;
partial workspace failures project only committed prefixes. Successful model
output is a short summary, while complete diffs stay in structured client/ledger
bodies. There is no hidden truncated diff preview requiring a separate recovery
protocol. Both module inventory rows are now reviewed. Native reads, exec and
provider adapters still have their separately recorded pending scope; this is
not full capability or project-gate completion.

## Directory scan cancellation

Directory traversal now checks the invocation's existing abort signal while
consuming scandir entries, and before entering recursive scans. `takewhile`
stops iteration without a custom exception or cancellation state machine; the
existing scandir context manager closes the iterator. After materialization,
the renderer returns the same typed confirmed cancellation as native file
reads rather than a successful partial directory tree. Bounded rendering of
already-selected entries needs no additional asynchronous mechanism.

Ruff, formatting and targeted source type checking passed; 97 existing
file/format/artifact/coverage tests passed in 1.01 seconds. A real 50-entry
directory with cancellation injected on the third scandir entry stopped at
three entries, closed the iterator and returned ToolFailure with confirmed
cancellation. No tests added. Blocking individual OS calls are still limited
by OS completion; this adds cooperative cancellation between observations.
Native clipped-line recovery and remaining artifact/archive semantics still
need review.

## Native clipped-line recovery

When a selected native line exceeds the existing character preview limit, read
now snapshots the original bytes into the existing artifact store and exposes
that reference in standard output bounds plus byte-continuation guidance. The
line-based result body and coverage remain unchanged: clipped lines do not
claim whole-file visibility. Ordinary short-line reads do not snapshot or spill.

The recovery snapshot reuses the same open descriptor, is bounded by the
existing 50 MiB binary/artifact materialization ceiling, and must match the hash
of the observed text stream before it is stored. Oversized recovery requests
fail explicitly; source changes during snapshot preparation fail rather than
linking different content to the preview hash. No new cursor/schema fields or
artifact protocol were introduced. This adds a bounded second read only to
clipped native pages.

Ruff, formatting and targeted type checking passed; 97 existing file/format/
artifact/coverage tests passed in 1.08 seconds. Manual probes recovered 30,000
Chinese characters and original CRLF bytes through artifact byte pages, checked
no-spill short reads and the recovery size ceiling, and rejected an in-place
source change before any mismatched spill. No tests added. Remaining read
artifact/archive projection and lifecycle details are still pending.

## Archive member metadata count bounds

Archives now admit at most 10,000 members before normalizing metadata, building
name sets or rendering children. ZIP checks its SDK-loaded infolist before
allocating a second metadata collection; directory identity uses ZipInfo.is_dir.
TAR iterates only the limit plus one via stdlib islice instead of unconditionally
calling getmembers before checking any application limit. Over-limit archives
fail explicitly rather than returning an incomplete membership map.

This bounds application-level metadata collections and TAR header count, not
the standard ZIP parser's initial central-directory load. Nor does a header
count bound total TAR decompression work when skipping large preceding members;
that remaining resource/feature-scope decision is still open. No custom archive
parser or dependency was introduced.

Ruff, formatting and targeted type checking passed; 97 existing file/format/
artifact/coverage tests passed in 1.19 seconds. Manual real ZIP/TAR probes
confirmed over-limit refusal and ordinary selected-member reads; a scripted
100-member TAR iterator stopped after exactly limit plus one observations.
No tests added. Archive listing continuation, collisions and remaining
projection/cancellation semantics still need review.

## Archive directory continuation with existing offsets

Archive listings now honor the existing 1-based offset and limit arguments,
with the previous 200-entry ceiling retained per page. The standard next_offset
field and output guidance identify the next entry; invalid offsets fail visibly.
Removed the misleading advice to list a subdirectory when a flat archive has
more than 200 files. Read argument descriptions now include member lines and
archive entry pagination. No new cursor type or separate listing mechanism.

Ruff, formatting and targeted type checking passed; 97 existing file/format/
artifact/coverage tests passed in 0.99 seconds. Manual real ZIP/TAR archives
with 450 flat entries were reconstructed exactly across three listing pages,
with no gaps/duplicates; seven-entry limits and out-of-range offsets were also
checked. No tests added. Count limits alone do not establish byte-bounded output
for unusually long member names, and compressed TAR scan work/cancellation and
file/directory name collisions still need reconciliation.

## Archive file/directory identity conflicts

Archive children now reject a path that is simultaneously a file and an
explicit/implicit directory, independent of member ordering. Direct reads of
such a path also fail rather than preferring descendants and hiding the file.
The same child map detects conflicts; no new identity registry or heuristic
precedence was added. Repeated file entries still retain the existing last
member identity/size semantics.

Ruff, formatting and targeted type checking passed; 97 existing file/format/
artifact/coverage tests passed in 1.04 seconds. Real ZIP/TAR probes rejected
conflicts in both root listings and selected-member reads. Additional manual
probes covered reversed member ordering, explicit directory conflicts and
last-size behavior for repeated files. No tests added. Long-name output bounds
and compressed archive scan resource/cancellation limits remain pending.

## Byte-bounded archive directory pages

Archive entry pages now stop at both the existing count limit and read data
byte budget. The continuation field advances by the number actually rendered;
full member names are retained instead of clipping an identity callers need
for selected reads. Header and a worst-case continuation line are included in
the data budget, without a new configurable limit or pagination mechanism.

A directory label or single member name too large to fit fails explicitly; it
cannot produce an empty truncated page advertising the same cursor forever.
Ordinary short-name listings retain the same pagination. This bounds the raw
listing data, not all surrounding envelope/summary metadata.

Ruff, formatting and targeted type checking passed; 97 existing file/format/
artifact/coverage tests passed in 1.14 seconds. A real ZIP with 200 long
multibyte names reconstructed all names exactly across eight byte-bounded
pages, with strictly increasing offsets. A single 60,000-byte name was rejected
without a stuck cursor. No tests added. Compressed archive scan resource and
cancellation limits remain pending review.

## Artifact raw and byte-read precedence

Artifact invocation now forwards its validated raw selector into rendering.
Explicit raw reads skip both claimed and magic-detected PDF extraction, instead
of silently running Poppler despite the request. Explicit byte_offset now
selects ordinary byte paging before media-type-based PDF/image handling. An
explicit PDF view still rejects incompatible byte arguments at ingress.

No new reading branch or parameter was added: this enables existing raw/byte
paths that automatic media classification previously shadowed. Raw binary
text pages retain existing UTF-8 replacement decoding; they do not claim a
lossless binary-to-text encoding. Default PDF extraction and image attachments
are unchanged.

Ruff, formatting and targeted type checking passed; 97 existing file/format/
artifact/coverage tests passed in 0.88 seconds. Manual claimed/unclaimed PDF
reads with extraction patched to fail confirmed raw and byte reads never invoke
it; image byte reads returned the requested byte cursor rather than attachment
output. No tests added. Artifact full-read cancellation and remaining read
projection semantics still need review.

## One artifact dispatch path with cancellation between pages

Merged the duplicated raw/PDF invocation branches into one renderer call. A
single ingress block rejects unsupported format selectors, page without an
explicit PDF view, and byte arguments with an explicit PDF view. Removed the
private renderer's redundant byte_limit/byte_offset combination validation;
that constraint is already validated by invocation ingress.

The shared artifact page fetch now observes the existing abort signal before
each store read. Stdlib CancelledError carries that local stop through the
renderer; the one dispatch boundary returns the same typed confirmed read
cancellation as native files/directories. This covers full-PDF materialization
and line-offset scanning without adding per-loop cancellation implementations
or a new exception class. Cached first pages remain reused.

Ruff, formatting and targeted type checking passed; 97 existing file/format/
artifact/coverage tests passed in 1.10 seconds. A scripted multi-page PDF store
cancelled after page one: no second store read or PDF extraction occurred, and
the result was ToolFailure with confirmed cancellation. No tests added.
Remaining archive decompression and artifact/transcript projection scope is
still recorded separately; the complete capability audit is not yet achieved.

## Google function-call identity round trip

Google now retains an upstream function_call.id through the existing shared
normalizer rather than always replacing it with a name/ordinal. History replay
sends that ID on function_call, and consecutive tool results return the matching
ID on function_response. The previous deterministic fallback is retained only
when the provider omits an ID. No new identity field or mapping mechanism.

[Google function-calling documentation](https://ai.google.dev/gemini-api/docs/generate-content/function-calling.md)
requires returning the exact function-call ID in its response; this matters for
parallel calls sharing a tool name. Installed SDK FunctionCall/FunctionResponse
types accept the preserved fields.

Ruff, formatting and targeted type checking passed; 118 existing provider
streaming/usage/lifecycle/error/thinking tests passed in 11.36 seconds with
local loopback permission. Manual real SDK-part probes preserved two same-name
calls with distinct server IDs through SDK-valid replay and tool-result parts,
and checked the missing-ID fallback. No tests added or live vendor calls used.
Google thought-signature/part replay remains a separate known gap, alongside
the previously recorded OAuth and remaining wire audit.

## Google signed Part persistence and replay

Google responses with thought signatures now preserve their complete ordered
Part list in existing opaque provider metadata (`google_parts`). Non-stream
and stream completion use the same helper; streaming retains original chunk
part boundaries, including empty text parts carrying only a signature. Without
a signature, no replay metadata is added.

SDK Part.model_dump(mode="json") supplies the existing base64 binary encoding;
Part.model_validate restores typed parts after SDK JSON metadata thawing. Signed
assistant messages replay those parts directly rather than reconstructing and
merging text/functions, keeping signatures attached to the original Part. No
custom codec, tool-name signature map, SDK field or storage mechanism was added.

[Google thought-signature documentation](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)
requires returning signatures within their original parts, including during
function calling. Installed SDK Part configuration confirms base64 JSON byte
serialization and validation.

Ruff, formatting and targeted type checking passed; 118 existing provider
streaming/usage/lifecycle/error/thinking tests passed in 11.42 seconds with local
loopback permission. Manual real SDK response/stream probes preserved binary
signatures on two same-name function calls and an empty signature-only part,
then round-tripped provider-result JSON persistence and frozen request metadata
back to identical SDK parts. Unsigned metadata stays absent and signatures
were not emitted in visible text deltas. No tests added or live vendor calls
used. OAuth and remaining Google wire/usage reconciliation remain pending.

## Google OAuth uses SDK credentials rather than an API key

Fixed the auth=oauth branch that passed access_token as api_key. It now creates
existing google-auth OAuth Credentials and lets Google GenAI send the Bearer
header. Explicit credentials select the already-supported Vertex surface; an
OAuth configuration must name its project, with optional region left to SDK
defaults. Missing project fails typed before SDK construction. Credentials do
not fall back to GOOGLE_API_KEY; API-key and service-account paths remain
separate. No new auth mode, fields, login/refresh manager or dependency.

[Google SDK client documentation](https://googleapis.github.io/python-genai/genai.html)
assigns its credentials argument to Vertex AI, while the
[OAuth quickstart](https://ai.google.dev/gemini-api/docs/oauth) documents Bearer
access-token delivery. The configuration guide now states this implementation's
Vertex/project requirement and supplied-token scope.

Updated the existing auth wire test to check actual OAuth semantics rather
than the old API-key header bug. Ruff, formatting and targeted type checking
passed; 124 existing auth/catalog/lifecycle/error/usage/thinking tests passed
in 14.36 seconds with local loopback permission. The real SDK sent Bearer
authorization without x-goog-api-key and used the configured project/location
path. Manual probes confirmed an ambient API key does not override explicit
OAuth and missing-project errors do not expose token material. No tests added
or live vendor calls used. Remaining adapter source/usage reconciliation and
other capability inventory items still prevent full goal completion.

## Google latest observed stream usage

Streaming now retains the latest valid ProviderTokenUsage instead of keeping
the final SDK response solely to inspect its usage. A trailing event without
usage no longer erases statistics already reported by the provider. Explicit
zero counts replace earlier totals, and a stream without any valid usage
remains unknown. Counts are neither accumulated nor filled from guessed zeros.
The raw last-response state was replaced by the typed usage state it served.

Ruff, formatting and targeted type checking passed; 118 existing provider
streaming/usage/lifecycle/error/thinking tests passed in 11.63 seconds with local
loopback permission. Manual real SDK response streams checked an empty final
usage event, a later explicit all-zero report, and no usage at all. No tests
added or live vendor calls used. Reasoning-output accounting and remaining
native adapter source reconciliation are still pending.

## Google generated output includes thinking

Google's candidates_token_count excludes thoughts_token_count. The adapter now
reports their sum as the existing normalized output_tokens total only when
both counts are valid; missing/invalid components leave the total unknown.
Zero remains an observed zero. No inferred total from prompt subtraction or
new reasoning-count field was introduced. SDK/public fact descriptions now
state that generated output includes reasoning; historical recorded facts are
not repriced or rewritten.

[Google Generate Content thinking documentation](https://ai.google.dev/gemini-api/docs/generate-content/thinking)
separately defines candidate/thinking counts and prices their combined output.
Installed SDK usage metadata confirms those distinct fields.

Updated the existing real-SDK streaming assertion: its fixture reports only
candidate tokens, so the inclusive total is unknown. Ruff, formatting and
targeted type checking passed; 131 existing provider/data tests passed in
11.45 seconds with local loopback permission. Manual probes covered nonzero
sums, zero components, absent components and invalid boolean/negative thinking
counts. No tests added or live vendor calls used. Remaining adapter and
capability inventory scope still prevents full goal completion.

## Google non-stream text projection shares the Part loop

Removed the SDK response.text convenience read from propose_turn. Visible text
is now collected alongside reasoning and tool calls in the existing Part loop.
This avoids a second text scan and the SDK's warning on normal function-call
parts, while retaining original signed Part metadata separately. Thought text
stays in reasoning and does not enter visible output.

Ruff, formatting and targeted type checking passed; 118 existing provider
streaming/usage/lifecycle/error/thinking tests passed in 11.30 seconds with local
loopback permission. A real SDK response subclass whose text accessor raises
confirmed extraction of visible text, reasoning, call identity and signed
metadata without invoking that accessor. No tests added or live vendor calls
used. Signed replay for provider-omitted function IDs and remaining adapter
reconciliation still need review; Google is not marked fully reviewed yet.

## Google signed replay distinguishes runtime and wire IDs

Signed Google parts must stay unchanged, including an omitted function ID.
History conversion now maps the current assistant's runtime call identities to
the original signed function IDs, including None. Tool feedback therefore
omits an ID when its original signed call omitted it, rather than sending an
invented runtime fallback unmatched by the preserved Part. Explicit upstream
IDs still round-trip. The map is local and cleared for each assistant, so later
unsigned calls with reused fallback IDs cannot inherit earlier mappings.

The original/runtime call lists are zipped strictly: inconsistent own metadata
fails rather than silently truncating. Consecutive tool feedback now uses one
loop for the whole batch instead of separate first/following message code. No
persisted field, signature mutation or cross-plugin contract was added.

Ruff, formatting and targeted type checking passed; 137 existing provider/auth
tests passed in 21.75 seconds with local loopback permission. Real SDK-part
probes covered missing/explicit IDs in one signed batch, unchanged binary
signature serialization, a later unsigned fallback-ID reuse and malformed call
count rejection. No tests added or live vendor calls used. Remaining native
adapter reconciliation and other capability inventory items remain pending.

## Google adapter source audit reconciliation

Completed source reconciliation across Google client construction/ownership,
service-account and OAuth/API-key authority, configured endpoint/version/header
behavior, catalog tool/output/thinking controls, history role/tool batching,
SDK response/stream projections, signed Part persistence, original/missing
function IDs, usage normalization, prompt blocking, terminal reason handling
and cancellation/error paths. Each affected area has its implementation and
verification evidence recorded above. The module inventory row is reviewed.

Known product boundaries remain explicit: supplied OAuth tokens use Vertex
with project and do not implement interactive login/refresh; network calls use
configured SDK timeouts, so cooperative caller cancellation does not promise
instant termination of an in-progress SDK/socket operation; unknown usage
components remain None; no live provider compatibility is claimed from local
fixtures. These are stated boundaries rather than hidden success fallbacks.

The last affected combined adapter/auth validation passed 137 existing tests
in 21.75 seconds, alongside targeted type/lint checks and the signed/missing-ID
manual probes. No source edits or additional tests were needed for this final
review record. A filesystem-to-inventory comparison found every current file
under capabilities listed, with only the deliberately removed sanitizer still
recorded as removed. This proves inventory coverage, not review completion of
rows still marked partial/pending or the outstanding full project gate.

## Remove identity Anthropic output-cap arithmetic

Deleted `_max_tokens_with_thinking`, its buffer/floor constants and the budget
return value threaded from `_thinking_payload`. Payload assembly already sets
max_tokens to the catalog model maximum (or the required unknown-model cap).
The deleted helper clamps any raised result to that same maximum, so it always
returns the value already present. Thinking controls still write directly into
the payload; their helper now truthfully returns None. Corrected the stale
docstring that claimed off omitted a knob despite writing disabled thinking.

Production source is 29 lines shorter. Ruff, formatting and targeted type
checking passed; 139 existing reasoning/streaming/usage/lifecycle/error/thinking
tests passed in 25.95 seconds with local loopback permission. Thirty generated
old/new payload comparisons across model caps and integer budgets were exactly
equal. No tests added or live vendor calls used. This proves the deleted cap
calculation was redundant; it does not establish validity of arbitrary numeric
thinking budgets, whose zero/minimum/output-cap protocol checks remain pending.
