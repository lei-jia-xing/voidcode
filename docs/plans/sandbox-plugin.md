# Transparent Sandbox Exec Backend (as-built; implemented)

SUPERSEDES the earlier sandbox-as-a-plugin design below the fold of this file's
history, and supersedes `local://sandbox-implementation-contract.md` (the v0/v0.1
plugin-era contract, kept for archaeology only). The user-approved re-brief killed the
plugin: the sandbox is now a transparent runtime exec backend. Plugins make ZERO
sandbox changes. When this record and the repo disagree, the repo wins.

## What shipped

- Optional top-level `sandbox` section in `voidcode.json` (`mode`, `network`,
  `writable_roots`; strict parser, unknown keys refused). ABSENT section = unconfined
  host exec, byte-identical to today (`effects=None`, nothing staged).
- Neutral `SandboxPolicy` in `core/activation.py` following the `ExecutionPolicy`
  pattern. Product (`config.py`/`bootstrap.py`) maps config to neutral; core never sees
  `voidcode.json`. The section is recorded in the private snapshot and rebuilt on
  restart (old snapshots without it rebuild to absent).
- `core/sandbox.py`: helper argv + explicit full env (baseline allowlist, secrets never
  implicit, loader vars dropped) + protected paths (config file, admitted plugin roots,
  helper, state private/db/spool) + availability probe + report decoding.
- `native/sandbox-adapter/`: thin `voidcode-sandbox-helper` (`probe`/`exec`/`supervise`)
  linking public `mxc-sdk 1.0.1` (crates.io, MIT) — bubblewrap/Seatbelt/ProcessContainer
  via `Containment::Process`. Supervisor runs exact argv (no shell re-parse), pumps
  byte-clean stdio, snapshot-diffs writable roots, flushes the report even under
  kill/timeout. No Codex sources vendored, no CLI dependency, no version policing.
- Registry `run`/`spawn` confine automatically when a backend is present. Kept: spool-only
  logs under stable `state_dir/process-spool/` (custom log paths refused with a clear
  sandbox reason), cumulative sidecar, danger mode (unconfined supervisor, best-effort
  label).
- Transparent observations: `run_process` stages confined changes onto
  `LeaseContext.staged_observations`; `read_process_output`/`cancel_process` stage the
  cumulative sidecar for user jobs (services never stage); engine `_report` merges into
  `result.changes` for success AND failure, deduped by `(path, operation, old_path)`,
  tool entries win ties, capped at 256 per result (`MAX_MERGED_OBSERVATIONS`). No
  `ToolDefinition`/coverage/fact-shape changes; existing `canonical_path`/`resource_paths`
  flow untouched. A thrown tool keeps its observation prefix (committed world truth);
  cooperative cancel keeps existing parked-batch recovery.
- No reload gate: new spawns use the current policy; old jobs keep birth enforcement
  (argv fixed at spawn). Model-editing-config + yolo reload is out of threat model.

## Explicitly dropped from the plugin design

- No `plugins/sandbox/`, no `sdk/process_plan.py`, no `sdk/fs_ops.py`, no
  `Plugin.prepare_process`, no `prepare_process`/`fs_op`/`read_process_effects` services,
  no contributor slot/refusal/identity, no digest gate, no birth digests, no helper
  `fs-op` subcommand. SDK keeps only `sdk/observed_effects.py` (internal report shapes)
  and `ProcessResult.effects`.
- No deny-glob language, no permission-profile machinery, no per-command
  escalation/retry, no Codex parity claims. `.git` is ordinary writable workspace metadata.
- No seccomp filter on the bubblewrap path (namespace+mount isolation only) — a
  hardening delta vs Codex, stated honestly.

## Honest V1 boundary

Registry children ONLY: shell_exec, background jobs, user-shell escapes, LSP service
processes. Hooks executor, review git, and MCP stdio stay host-side with ZERO plugin
changes and are NEVER claimed confined — documented follow-up, not a cut. File tools
and ast_grep/LSP-doc reads stay host-side and self-report; engine dedup absorbs overlap
with supervisor observations. macOS/Windows backends share the helper shape but are
runtime-unverifiable on this Linux host.

## Evidence

- `tests/integration/test_sandbox_backend.py`: confined exec lands in durable fact
  changes with zero tool edits (loopback model turn), outside-root write never touches
  host, custom log refusal, danger best-effort label, absent-section parity. Loud skip
  without helper/bwrap.
- `tests/unit/core/`: 188 passed. `mise run typecheck`: clean. ruff check+format clean
  on touched files.
- Packaging: hatch hook builds the helper into `voidcode/bin/` for wheels
  (`--locked`, fails loudly without cargo unless `VOIDCODE_SKIP_HELPER_BUILD=1`);
  native sources + `Cargo.lock` in the sdist; NOTICE attributes mxc-sdk (MIT).
