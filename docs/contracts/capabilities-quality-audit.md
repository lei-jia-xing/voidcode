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
| `exec/__init__.py` | Pending full source review |
| `exec/args.py` | Reviewed source/consumers: three small nonempty field validators used by strict exec argument models |
| `exec/background_process.py` | Partial: strict persisted identity ingress, UTF-8/CRLF log pages and final readiness draining reviewed; typed state pipeline reviewed; scan ceiling/cursor and remaining lifecycle review pending |
| `exec/shell_exec.py` | Pending full source review |
| `files/__init__.py` | Reviewed: typed Tool wrapper preserves diagnostic failures; shared core/plugin factory |
| `files/_replace.py` | Reviewed: checked replacement and exclusive atomic creation; cleanup, permissions and race regressions; documented metadata/compare-window limits |
| `files/apply_patch.py` | Partial: stdin unified diffs, canonical sequential marker preflight and checked commits reviewed; parser, partial-result and cancellation review pending |
| `files/apply_workspace_edit.py` | Partial: strict generated schema, frozen-argument conversion, checked replacement and commit projections reviewed; coordinates, cancellation and partial-result review pending |
| `files/edit_tool.py` | Partial: unified exact-edit flow, checked replacement and result semantics reviewed; final ingress, diagnostic bounds and cancellation review pending |
| `files/errors.py` | Reviewed: retain only classified diagnostic exception and raising helper |
| `files/gitignore.py` | Reviewed: root-only pathspec matching; removed unused pattern snapshot/property; syntax/order/escape tests |
| `files/glob_tool.py` | Partial: strict search ingress and error-visible traversal reviewed; output bounds pending |
| `files/grep_tool.py` | Partial: strict search ingress/default path reviewed; traversal completeness pending |
| `files/read_formats.py` | Partial: notebook pagination/global output budget, SQLite encoded budget/read-only queries, PDF process cleanup/preview cap reviewed; snapshot, image and remaining pagination semantics pending |
| `files/read_tool.py` | Pending full source review |
| `files/write_tool.py` | Reviewed: hash/containment/symlink/atomic replace/cancellation; fail visibly on mode errors and always clean temporary files |
| `frontmatter.py` | Reviewed source/consumers: safe YAML, duplicate/string-key validation, framing/body/bounds; shared parser tests |
| `providers/__init__.py` | Reviewed: shared registration/facts/view wiring and ExitStack-owned provider teardown |
| `providers/dispatch.py` | Reviewed: strict registration ingress, explicit auth conflict handling, typed materialization/selection, locked lazy cache/teardown and usage wrappers |
| `providers/native/__init__.py` | Reviewed: documentation-only namespace, no side effects |
| `providers/native/_resources.py` | Reviewed: contained package paths and JSON loading; removed unused installed-data locator/text wrapper |
| `providers/native/_result_sanitize.py` | Removed: only consumer sat behind an unreachable metadata.data dict check; SDK content is already projected by core |
| `providers/native/_wire_common.py` | Reviewed: usage counts, tool IDs, request headers, bounded queue/stream timeout/error cleanup, owned-resource initialization |
| `providers/native/anthropic_native.py` | Pending full source review |
| `providers/native/claude_tokenizer/__init__.py` | Pending full source review |
| `providers/native/claude_tokenizer/constants.py` | Pending full source review |
| `providers/native/claude_tokenizer/engine.py` | Pending full source review |
| `providers/native/claude_tokenizer/normalize.py` | Pending full source review |
| `providers/native/claude_tokenizer/notation.py` | Pending full source review |
| `providers/native/config.py` | Reviewed: strict ingress, field/table wiring, typed converters, env resolution, custom names, credential exclusivity and representations; removed dead framework/default paths |
| `providers/native/errors.py` | Reviewed: classified errors, SDK text extraction and structured retry hints; removed prose scheduling and duplicate conversion functions |
| `providers/native/google_native.py` | Partial: SDK timeout/transport errors, usage, owned-client cache, stream ownership/chunk cancellation and identity tool mapping reviewed; OAuth and remaining wire semantics pending; dead attachment branch removed, caller cancellation prompt with documented bounded SDK teardown lag |
| `providers/native/model_catalog.py` | Reviewed static catalog fields/lookups and consumers; removed entire unreferenced remote discovery implementation |
| `providers/native/model_catalog_data.json` | Pending consumer/data-contract review |
| `providers/native/model_routing.py` | Reviewed: routed alias/catalog fallback, typed unsupported wires, serialized cache creation and teardown |
| `providers/native/naming.py` | Reviewed source/callers: provider normalization, preserved model casing, table-derived labels/reservations and unknown-id diagnostics; removed dead canonical-reference wrapper |
| `providers/native/openai_native.py` | Partial: lifecycle/stream/error work and ordered gateway feedback reviewed; removed metadata mode/attachment scaffolding; remaining transport/payload/replay source review pending |
| `providers/native/opencode.py` | Pending full source review |
| `providers/native/opencode_go.py` | Partial: nonnullable endpoint path and minimal feedback model set reviewed; gateway host precedence/protocol quirks pending |
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
