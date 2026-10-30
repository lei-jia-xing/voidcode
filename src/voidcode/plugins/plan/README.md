# plan

Plan mode: a read-only exploration overlay plus the plan instruction.

## Surface

- Command `/plan` (no arguments): enter plan mode. Sets the read-only session overlay and records the activation. Idempotent — re-entering reports `changed: false`.
- Command `/plan off` (no arguments): leave plan mode. Revokes this plugin's overlay only.
- Command `/plan status` (no arguments): reports `Mode: plan` or `Mode: default` plus `{"active"}`.
- Context contribution: while active, contributes the plan-mode instruction to the model context. All three commands run with `busy_gate=False`.

## Configuration

Takes no plugin options; the factory accepts only the injected `providers` map and rejects unknown fields. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Session-scoped state holds only `schema_version` and `active` (schema v1); entering/leaving stages `plan_mode_enabled`/`plan_mode_disabled` events.
- Deactivation parks an active mode off: the activation transaction deletes this owner's overlay row, so keeping `active` would promise a restriction that no longer exists — the user re-enters explicitly.

State ingress requires an integer schema version and a boolean active flag; missing
or unknown fields fail visibly. Status presentations offer an explicit next-mode
action, and reading status never enters or leaves plan mode.
