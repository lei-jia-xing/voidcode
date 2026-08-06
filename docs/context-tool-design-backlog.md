# Context And Tool Design Backlog

This is the working backlog for improving prompt assembly, provider cache reuse,
and agent-facing tool contracts. It records the audit baseline; items are not
considered complete until implementation and focused tests provide evidence.

## Context Assembly And Caching

- [ ] Define a production cache-key contract (stable-prefix hash, dynamic-suffix
  hash, tool materialization generation, provider/model identity) and expose hit
  metadata from provider execution. Prompt hashes and a provider request
  `prompt_cache_identity` are now available; actual provider cache hit reporting
  remains. LiteLLM diagnostics now log the non-content hash dimensions.
- [ ] Move session/config-stable instruction sections before the dynamic boundary;
  keep date, git status, touched-file rules, README context, task state,
  continuity, tool results, and the current user request dynamic.
- [ ] Cache git/environment observations and refresh them on meaningful workspace
  changes instead of every prompt assembly.
- [ ] Deduplicate and version rule/README injections by normalized path and file
  revision so ordering or repeated tool results do not cause avoidable misses.

## Agent-Facing Tool Contracts

- [x] Preserve `ToolDefinition.path_argument_keys` whenever definitions are
  decorated with guidance.
- [ ] Make every input schema explicit about required fields, constraints, enums,
  and field-level descriptions. `read_file` now documents path, offset, limit,
  and continuation semantics; `write_file` and `multi_edit` now document their
  replacement semantics; `grep` now documents explicit literal/regex selection
  and search filters; the broader tool surface remains.
- [x] Reconcile `grep`'s regex description with its actual explicit-switch
  behavior and retry guidance.
- [ ] Converge on `content` as a short human summary and put machine-readable
  payloads in `data`.
- [ ] Plan a compatibility migration from `read_file.filePath` to `path`.
- [ ] Provide structured file lines/raw content so edit calls do not need to
  strip presentation line prefixes.

## Evidence Already Found

- `src/voidcode/tools/guidance.py` currently drops `path_argument_keys`.
- `src/voidcode/runtime/prompt_assembly.py` places the dynamic boundary before
  workflow, skills, hooks, memory, and tool policy sections.
- `tests/unit/runtime/test_prompt_stable_prefix.py` hashes a prefix for tests,
  but no provider cache reuse contract is implemented there.
- Runtime context metadata now exposes deterministic stable-prefix and
  dynamic-suffix hashes; provider cache hit reporting and tool-generation/model
  dimensions remain to be wired into the provider request layer.
