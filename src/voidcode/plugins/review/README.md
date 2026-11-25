# review

Review workspace Git changes with a governed, read-only child. `/review` is its
own Git-diff flow, not the builtin `review` subagent profile: the child is
labelled `review-git` and receives only the available read-only inspection
tools.

## Surface

- Command `/review` (effects `READ` + `SPAWN` + `SESSION`, busy-gated). Optional
  arguments: `focus` (reviewer focus; alone it reviews as a custom target),
  `target` (`uncommitted` default | `base_branch` | `commit` | `custom`),
  `branch` (required for `base_branch`), `sha` (required for `commit`).
- Bare `/review` in TUI/Web opens a target selector and focus editor without
  inspecting Git or starting a child. Branch/commit requirements appear in the
  form labels; explicit `/review <focus>` and structured CLI/RPC arguments
  retain direct invocation.
- Spawns one child, stages one `review_dispatched` event, then joins the child
  for up to 300s and returns its findings or a typed error carrying
  `child_session_id` and `target_type`. Command events commit with the command
  result, so `review_dispatched` is a post-hoc dispatch record committed with
  the command result; a crash during the join loses it. It is not a live-start
  signal. It records the child id plus prompt and diff SHA-256 digests.
- A review still running at the join deadline is not cancelled. The command
  returns `status="running"` (or `status="waiting"` for an approval-parked
  child) with the actual wait time and guidance to use `subagents.monitor` or
  `voidcode show`. The child is spawned with `notify_on_completion=False`: the
  synchronous join owns the result, and a still-running review is announced
  through the returned child id and dispatch event but is not pushed to the
  parent. Its result is recovered from the session tree rather than a
  duplicate parent turn.
- Oversized prompts are refused with guidance to review incrementally.

## Configuration

Takes no plugin options; only the injected `providers` map is accepted and
ignored. Unknown fields are rejected. See
[configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- **Child scope.** The child inherits the parent's stance and is spawned with
  `readonly=True`, label `review-git`, `max_steps=32`, and a `tool_allowlist`
  containing only the available subset of `read`, `grep`, and `glob`. Network,
  task/delegation, shell, and mutation tools are never requested. The core
  refuses a spawn that would widen a restricted parent scope.
- **Effect semantics.** `SESSION` is declared because this command stages a
  plugin event; it does not grant file or process mutation. `EXECUTE` is
  deliberately not declared: the host-side `git` reads are trusted read-side
  behavior, not a plugin-declared external execution capability. The command
  therefore remains admissible in a read-only/plan session, and the child is
  forced durably read-only by the core.
- **Durability.** The spawn-to-event window is guarded: if the dispatch event
  cannot be staged, the child is cancelled and the command fails with the child
  id and staging error. If cancellation itself fails, the error names the child
  so it remains visible through `subagents.monitor` or `voidcode show`.
- **Git scope.** Diffs are built from Git directly (staged, unstaged, and
  untracked files). When the workspace is a subdirectory, `git diff` and
  `git show` are path-limited to that workspace; `git ls-files --others`
  enumerates the repository and results are filtered to the workspace before
  inspection. `base_branch` compares against `merge-base HEAD <branch>`; there is
  no upstream heuristic. `commit` resolves the supplied revision to an exact
  commit and shows only its patch. Option-like `branch`/`sha` values are refused;
  refs are resolved before use and diff/show path limits are separated with `--`.
- **Secret path exclusion.** Tracked `diff`/`show` paths and untracked listings
  exclude conservative basenames: `.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`,
  `credentials`, `credentials.*`, `id_rsa*`, `id_ed25519*`, `secrets.json`,
  `secrets.yaml`, `secrets.yml`, and `secrets.toml`. This is filename-only:
  content scanning of allowed files is out of scope, so tracked secrets in other
  files or differently named files can still appear. Untracked exclusions are
  counted in the dispatch event; tracked exclusions are silent.
- **Untrusted data.** The prompt delimiters and instructions identify the Git
  diff as untrusted repository data: the child is told never to follow
  instructions inside it and to report only evidence-backed findings with
  severity and path/line.
- **Findings format.** Findings are free text. No `result_schema` is declared;
  severity, path/line, and evidence are prompt-enforced, not validated.
  Consumers must treat findings as model output, not a structured contract.
- **Output bounds.** Long child output resolves to the first bounded page plus
  an immutable `artifact://` reference. The full bytes are paged; only a
  verified page publishes `output_uri`. Failed and interrupted children keep
  their captured output or preview in the visible error text.
- Stateless: keeps no plugin state and no child mapping; child lifecycle facts
  remain core-owned. The command requires at least one of `read`, `grep`, or
  `glob` to be visible to the parent. A missing `git` binary or a Git timeout is
  a typed command error, not an unstructured failure.
