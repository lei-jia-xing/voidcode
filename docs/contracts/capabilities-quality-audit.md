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
| `exec/args.py` | Pending full source review |
| `exec/background_process.py` | Pending full source review |
| `exec/shell_exec.py` | Pending full source review |
| `files/__init__.py` | Pending full source review |
| `files/apply_patch.py` | Partial: read guards and fuzzy repair hints removed; final mutation/ingress review pending |
| `files/apply_workspace_edit.py` | Pending full source review |
| `files/edit_tool.py` | Partial: read guards and fuzzy repair hints removed; final mutation/ingress review pending |
| `files/errors.py` | Reviewed: retain only classified diagnostic exception and raising helper |
| `files/gitignore.py` | Pending full source review |
| `files/glob_tool.py` | Partial: strict search ingress/default path reviewed; traversal completeness pending |
| `files/grep_tool.py` | Partial: strict search ingress/default path reviewed; traversal completeness pending |
| `files/read_formats.py` | Pending full source review |
| `files/read_tool.py` | Pending full source review |
| `files/write_tool.py` | Pending full source review |
| `frontmatter.py` | Pending full source review |
| `providers/__init__.py` | Pending full source review |
| `providers/dispatch.py` | Pending full source review |
| `providers/native/__init__.py` | Pending full source review |
| `providers/native/_resources.py` | Pending full source review |
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
| `providers/native/endpoint.py` | Pending full source review |
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
