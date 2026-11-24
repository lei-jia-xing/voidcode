# plan

Plan mode: an owner-scoped read-only overlay plus the plan instruction. The core
`session_overlays` row is the only mode truth; this plugin keeps no state.

## Surface

- Command `/plan` (no arguments): add this owner's `read-only` overlay. Idempotent: when
  the overlay is already active, it reports `changed: false` and stages no event.
- Command `/plan off` (no arguments): revoke this plugin's overlay only. Reports
  `changed` from the overlay row; stages `plan_mode_disabled` only when a row was cleared.
- Command `/plan status` (no arguments): reports `Plan mode: active` or
  `Plan mode: inactive` plus `{"active", "read_only"}`. `read_only` is the session's
  effective stance; when plan is inactive but another restriction applies, the output
  names that reason instead of claiming plan mode is on.
- Context contribution: while this owner's overlay is present, contributes the plan-mode
  instruction to the model context. All three commands run with `busy_gate=False`.
- `plan_mode_enabled`/`plan_mode_disabled` are bounded audit events staged with the
  command when the overlay actually changes. The overlay itself is the durable truth.

## Configuration

Takes no plugin options; the factory accepts only the injected `providers` map and rejects
unknown fields. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Effective read-only is the frozen core policy OR any owner overlay. `/plan off` cannot
  lift the frozen policy or another owner's overlay; a delegated child born read-only has
  a core-owned overlay that no plugin can clear.
- Plan denies external mutation: tools declaring `WRITE` or `EXECUTE` are hidden from the
  provider and denied if called anyway. `READ`, `SESSION`, `NETWORK`, and `SPAWN` remain
  available. In `ask` mode `SPAWN` is still approval-gated. A child spawned under plan is
  forced durably read-only.
- Network reads remain available: `web_search`/`web_fetch` run in the core process, are not
  covered by `sandbox.network`, and are not approval-gated. Disable the `web` plugin (or
  add a pre-tool gate) when that egress is unwanted.
- Hooks are trusted host-side commands, not sandboxed by plan. Mutating pre-tool gates are
  skipped under read-only and recorded best-effort as `core.hook_skipped` (the call still
  proceeds); mutating post-tool hooks are skipped with a visible note. Non-mutating hooks
  and lifecycle hooks still run.
- Work started before plan mode (background processes, jobs, child runs) keeps running;
  plan only governs calls admitted under the overlay. LSP stays denied because its tool
  declares `READ + EXECUTE` for server processes.
- The overlay is session-keyed and survives a capability reload that keeps this owner
  admitted; it also survives restart through the recorded configuration. Disabling the
  plugin deletes its overlay rows in the activation transaction. Re-enabling starts with
  plan off; the user enters explicitly.
- There is no plugin state and no deactivation state transform. Legacy `plan` state rows
  from earlier builds are ignored and cannot block status, entry, context, `/plan off`, or
  dropping the owner.
- `/core reload` is a client configuration command. Its only read-only refusal is the
  frozen core policy, not this session overlay; a reload that disables `plan` deletes the
  overlay as part of the owner switch. A reload that keeps `plan` enabled preserves it.
