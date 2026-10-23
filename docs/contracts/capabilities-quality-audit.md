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
| `providers/native/_wire_common.py` | Pending full source review |
| `providers/native/anthropic_native.py` | Pending full source review |
| `providers/native/api_routes.json` | Pending consumer/data-contract review |
| `providers/native/api_routes.py` | Pending full source review |
| `providers/native/auth.py` | Pending full source review |
| `providers/native/claude_tokenizer/__init__.py` | Pending full source review |
| `providers/native/claude_tokenizer/constants.py` | Pending full source review |
| `providers/native/claude_tokenizer/engine.py` | Pending full source review |
| `providers/native/claude_tokenizer/normalize.py` | Pending full source review |
| `providers/native/claude_tokenizer/notation.py` | Pending full source review |
| `providers/native/config.py` | Pending full source review |
| `providers/native/copilot.py` | Pending full source review |
| `providers/native/endpoint.py` | Reviewed: direct typed endpoint configuration and chat provider construction; config implementation audited separately |
| `providers/native/errors.py` | Pending full source review |
| `providers/native/google.py` | Pending full source review |
| `providers/native/google_native.py` | Pending full source review |
| `providers/native/model_catalog.py` | Pending full source review |
| `providers/native/model_catalog_data.json` | Pending consumer/data-contract review |
| `providers/native/model_match.py` | Pending full source review |
| `providers/native/model_routing.py` | Pending full source review |
| `providers/native/naming.py` | Pending full source review |
| `providers/native/openai.py` | Pending full source review |
| `providers/native/openai_native.py` | Pending full source review |
| `providers/native/opencode.py` | Pending full source review |
| `providers/native/opencode_go.py` | Pending full source review |
| `providers/native/openrouter.py` | Pending full source review |
| `providers/native/pricing_rules.json` | Pending consumer/data-contract review |
| `providers/native/pricing_rules.py` | Pending full source review |
| `providers/native/provider_config.py` | Pending full source review |
| `providers/native/provider_table.json` | Pending consumer/data-contract review |
| `providers/native/provider_table.py` | Pending full source review |
| `providers/native/reasoning_effort.py` | Pending full source review |
| `providers/native/thinking_rules.json` | Pending consumer/data-contract review |
| `providers/native/thinking_rules.py` | Pending full source review |
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
| `providers/pricing.py` | Pending full source review |

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
