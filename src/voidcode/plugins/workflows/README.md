# workflows

Scripted dynamic agent workflows: explicitly configured trusted `.py` scripts that fan out over core-owned child sessions. A script defines `async def run(ctx, task)` using ordinary Python plus `ctx.agent(...)` for delegated nodes. There is no inline or model-supplied script execution, and no sandbox claim: a configured script is trusted code, equal to the plugin itself.

Runnable reference: `examples/workflows/` (parallel review: two review children, then one combined summary).

## Surface

- Tool `workflow_run` (`EXECUTE` + `SPAWN` + `SESSION`, never replayed): `workflow` (required configured name) + `task` (one fixed JSON value per run; a plain string passes through verbatim) + `notify_on_completion` (bool, default true). Returns a run receipt immediately; with notify on, the terminal result or error arrives as a durable follow-up turn. Workflow output is data, never instructions.
- Commands: `workflow.run` (`EXECUTE` + `SPAWN` + `SESSION`, `/workflow run`: start; always quiet) · `workflow.status` (`READ`, `/workflow status`: `run_id` plus optional `result_key`/`result_offset` node-result paging; graph, driver, and bounded node results) · `workflow.pause` (`SESSION`, `/workflow pause`: park the driver, children keep running) · `workflow.resume` (`EXECUTE` + `SPAWN` + `SESSION`, `/workflow resume`: re-drive the same script/input/keys) · `workflow.cancel` (`SESSION`, `/workflow cancel`: stop the driver plus every owned child; a non-user caller confirms first; already-terminal runs report their state, never overwritten).
- Views: `workflows` (status/fields: recent runs with status and node counts) and `workflows.run` (form bound to `workflow.run`, prefilled with the first configured workflow).
- Background driver: one core background action per run (`Plugin.background_action`) that keeps only the `BackgroundContext`, commits per short `invoke`, and never holds a lease across script awaits. Node joins release the dispatch slot between slices when an activation is pending, so a reload is not refused by a continuously held join. Running/waiting child sessions still block the activation transaction exactly as any other delegation does; pause or cancel a live workflow before reloading. Claim/status/cancel/wait/liveness follow the generic [background actions](../../../../docs/contracts/background-actions.md) contract.

## Configuration

`plugins.workflows.config` accepts exactly one field (`providers` reserved for product injection):

```json
{"workflows": {"parallel_review": "flows/parallel_review.py"}}
```

Names are non-empty strings; paths are workspace-relative `.py` files without `..`. Unknown fields, absolute paths, and non-`.py` files are refused. An empty mapping admits the plugin with no runnable workflows. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- `workflow.run` snapshots the configured file without executing it, spills the bytes to an immutable source artifact, and records the digest on the run. The driver compares the live file against the recorded digest *before* executing anything, then executes only the recorded bytes — once per driver attempt. A changed or unreadable source refuses resume with a visible error; there is no fallback to the live file.
- The script's return value is the run's durable result: text when small, otherwise a bounded page plus an `artifact://` reference readable through `workflow.status`. JSON `null` stays `null`, never an empty string.
- `ctx.agent(agent_name, prompt, *, key, depends_on=(), ...)` runs one named-agent child, resolved through the shared `workspace.agent_definition` query with the same narrowing (`apply_agent_profile`) as `task` — see the [subagents](../subagents/README.md) README for the agent format. `key` is required; `depends_on` names known keys explicitly (no inference; a `str`/`dict` is refused rather than iterated). `max_steps` caps the node; node concurrency is bounded internally.
- `policy.max_root_children` is a *lifetime* count of child sessions under one chain root, including completed children, not a concurrent fan-out limit. The default is 4 (`1..64`). A workflow with more than four nodes, and repeated runs in a long-lived session, consume that same budget; configure a sufficiently large value (for example `64`) before running wide or repeated workflows, or start a fresh session for a new budget. Exhaustion fails the run visibly and is not resumable after raising the value.
- Each node persists under a fingerprint of its *effective* input (resolved prompt body, stance, tool scope, budget, dependencies, run input, and a declared result schema). A resumed run reuses a completed node's durable outcome only on fingerprint match; any effective change refuses loudly instead of reusing a different outcome. `label` is presentation only and is not part of the fingerprint. Large child outputs resolve to full artifact bytes, never the bounded preview; inline state stays small while reads reassemble whole UTF-8 text across page cuts.
- Run states: `running`, `paused`, `parked`, `completed`, `failed`, `cancelled`. Approval waits and recoverable interruptions park honestly; pause parks the driver. Any real failure marks the run failed *and* fails the action, so `--wait-background` exits nonzero with the recorded error; this includes a script that catches the node error and returns a value. A terminal failed/cancelled run cancels each open node child and its whole descendant subtree. The origin session receives one bounded notice when a run park or cancellation happens, not only on success. Steering commands re-read state after slow RPCs, and a terminal run is never overwritten by a late driver outcome.
- Workflow state is serialized per session across all runs and steering commands. A lost owner-state CAS is retried by re-reading and re-applying the pure mutation (bounded); commands that opted in re-run their idempotent handler. A child created by a keyed spawn whose node-row save was lost is adopted by `find_child` before any respawn, including after a resume that captured a new root action identity; if the row still cannot be recorded, the error names the child id and the run parks so a later resume can join it.
- Resume re-executes the module top level and `run(ctx, task)`; only keyed `ctx.agent` nodes dedupe through their durable fingerprints — arbitrary script I/O has no exactly-once guarantee. Cancel is cooperative: it aborts at `ctx` boundaries, accepts no new nodes afterwards, and cancels owned children, but cannot force-terminate arbitrarily blocked script Python or host I/O.
- Disabling parks live (`running`/`paused`) runs as `parked`; children stay core-owned for resume after re-enable. A running or waiting child still refuses the activation switch through the ordinary session blocker, so pause or cancel live workflow children before `/core reload` (this is the deliberate delegation exception, not a workflow-specific one).

### State versioning

The plugin registers `StateSchema(version=SCHEMA_VERSION)` with a minimal payload schema (`schema_version` const plus a `runs` object), so a future payload-version bump is refused at admission, before any read parses the old shape.

### Interactive run status

Bare status/pause/resume/cancel commands prepare applicable run selectors without
starting work. Status presentations show ten nodes per page, per-node result
navigation and applicable lifecycle actions. Result offsets use the existing
UTF-8 byte paging contract; Next uses the returned offset, and later pages offer
return to the first page. Run summaries in the interactive graph are explicitly
labelled previews. CLI/RPC structured status data and governed cancellation
confirmation retain their existing semantics.
