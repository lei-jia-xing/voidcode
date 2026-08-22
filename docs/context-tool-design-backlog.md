# Context And Tool Design Backlog

This is the working backlog for improving prompt assembly, provider cache reuse,
and agent-facing tool contracts. It records the audit baseline; items are not
considered complete until implementation and focused tests provide evidence.

## Context Assembly And Caching

- [x] Define the final provider-wire prefix descriptor. LiteLLM now exposes
  canonical bytes/hash for the stable system-message prefix plus materialized
  tools, message count, tool generation, and assembly version. It is a
  deterministic diagnostic identity, **not** evidence of an actual cache hit.
  Provider usage retains unknown fields as `null` rather than treating them as
  observed zero.
- [x] Materialize the minimal Anthropic Messages-compatible cache policy. Only
  the explicit Anthropic adapter supports `cache_retention: short|long`, adding
  an ephemeral `cache_control` breakpoint to the final tool prefix (or system
  prefix when no tools exist), with `5m`/`1h` TTL respectively. Non-compatible
  provider configurations reject retention as `unsupported_feature`; fake
  gateway coverage proves write on turn one, read on turn two, and a tool-schema
  change produces a fresh write rather than a false hit.
- [x] Keep session/config-stable instruction sections before the dynamic
  boundary; date, git status, touched-file rules, README context, task state,
  continuity, tool results, and the current user request stay dynamic.
- [ ] Cache git/environment observations and refresh them on meaningful workspace
  changes instead of every prompt assembly.
- [ ] Deduplicate and version rule/README injections by normalized path and file
  revision so ordering or repeated tool results do not cause avoidable misses.

## Agent-Facing Tool Contracts

- [x] Preserve `ToolDefinition.path_argument_keys` whenever definitions are
  decorated with guidance.
- [x] Make every input schema explicit about required fields, constraints, enums,
  and field-level descriptions. `read` now documents path, offset, limit,
  and continuation semantics; `write` and `multi_edit` now document their
  replacement semantics; `grep` now documents explicit literal/regex selection
  and search filters; the broader tool surface remains.
- [x] Reconcile `grep`'s regex description with its actual explicit-switch
  behavior and retry guidance.
- [x] Converge on `content` as a short human summary and put machine-readable
  payloads in `data`. `read` returns a short summary in `content` with
  structured `data.lines` / `data.raw_content` / `data.content_hash`; `grep`
  returns a summary in `content` with matching details in `data.matches`.
- [x] Switch `read` to the canonical `path` field without a legacy alias.
- [x] Provide structured file lines/raw content so edit calls do not need to
  strip presentation line prefixes.

## Evidence Already Found

- `src/voidcode/tools/guidance.py` preserves `path_argument_keys` and has regression coverage.
- `src/voidcode/runtime/prompt_assembly.py` places skills, workspace memory,
  and tool policy before the dynamic boundary; reactive rules, runtime state,
  tool results, and the current user request follow it.
- `src/voidcode/provider/litellm_backend.py` owns the final wire descriptor;
  prompt-assembly hashes remain assembly diagnostics and are never represented
  as an actual provider-cache hit.
- Focused provider tests cover canonical finish reasons, object/string tool
  arguments, assistant text paired with tool calls, unknown usage, and the
  explicit unsupported Anthropic cache-retention path.
