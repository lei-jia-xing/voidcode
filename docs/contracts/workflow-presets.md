# Workflow Preset Contract (REMOVED)

**This contract was removed in the mode-composition Phase 1 refactor.**

The `WorkflowMode` family (`workflow_mode` metadata, `WorkflowModeResolution`,
versioned `workflow` snapshots, `hook_preset_refs` merged per workflow mode, and
the `workflow_mode_prompt_context` prompt slot) no longer exists.

The surviving concept is the single runtime `mode` (`normal` | `plan`), a
declarative combination of orthogonal switches resolved at one aggregation
point (`runtime/mode.py::resolve_mode`):

- `plan` implies `read_only` and activates the `mode_guidance` context
  transform provider via its `transform_refs`.
- Persistence is the plain session-metadata `mode` scalar (plus derived
  `read_only`); no snapshot contract is needed because the stored value is the
  scalar itself.

See `docs/mode-composition-design.md` for the authoritative design.
