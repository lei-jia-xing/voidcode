# review

Review workspace Git changes with a governed, read-only child agent.

## Surface

- Command `/review` (effects `READ` + `SPAWN`, busy-gated). Optional arguments: `focus` (reviewer focus; alone it reviews as a custom target), `target` (`uncommitted` default | `base_branch` | `commit` | `custom`), `branch` (required for `base_branch`), `sha` (required for `commit`).
- Bare `/review` in TUI/Web opens a target selector and focus editor without inspecting Git or starting a child. Branch/commit requirements appear in the form labels; explicit `/review <focus>` and structured CLI/RPC arguments retain direct invocation. Focus is included for every selected target. Submission follows normal validation and governance.
- Stages a `review_started` event, joins the child (300s timeout), and returns the child's final findings or a typed error carrying `child_session_id` and `target_type`. Oversized prompts are refused with guidance to review incrementally.

## Configuration

Takes no plugin options; only the injected `providers` map is accepted and ignored. Unknown fields are rejected. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- The child inherits the parent's tool scope, is spawned with `readonly=True` (max 32 steps, label `review`), and the core forces read-only whenever the calling lease is restrictive — so review works from read-only and plan-mode sessions. There is no fixed inspection-tool list; the child uses inherited tools within its read-only stance.
- Diffs are built from Git directly (staged, unstaged, and untracked files; merge-base comparison for `base_branch`); a non-repo workspace or Git failure surfaces as a command error. Requires the `git` binary with a 30s per-command timeout.
- Stateless: keeps no plugin state beyond the staged event.
