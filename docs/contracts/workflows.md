# Scripted workflows contract

The bundled `workflows` plugin runs explicitly configured trusted `.py`
scripts over core-owned child sessions. A script defines
`async def run(ctx, task)` using ordinary Python plus `ctx.agent(...)` for
delegated nodes. There is no inline or model-supplied script execution, and
no sandbox claim: a configured script is trusted code, equal to the plugin
itself.

## Configuration

`plugins.workflows.config` accepts exactly one field:

```json
{"workflows": {"parallel_review": "flows/parallel_review.py"}}
```

Names are non-empty strings; paths are workspace-relative `.py` files without
`..`. Unknown fields, absolute paths, and non-`.py` files are refused. The
`providers` key is reserved for product injection. An empty mapping admits
the plugin with no runnable workflows. Runnable reference:
`examples/workflows/`.

## Source snapshot and execution

`workflow.run` snapshots the configured file without executing it, spills the
bytes to an immutable source artifact, and records the digest on the run. The
background driver compares the live file against the recorded digest *before*
executing anything, then executes only the recorded bytes — once per driver
attempt. An explicit resume re-executes the module top level and
`run(ctx, task)`; only keyed `ctx.agent` nodes dedupe through their durable
fingerprints, and arbitrary script I/O has no exactly-once guarantee. A
changed or unreadable source refuses resume with a visible error; there is no
fallback to the live file.

The `task` argument is one fixed JSON value per run — a plain string passes
through verbatim — and the script's return value is the run's durable
result: text when small, otherwise a bounded page plus an `artifact://`
reference, readable through `workflow.status`. JSON `null` stays `null`,
never an empty string.

## Nodes

`ctx.agent(agent_name, prompt, *, key, depends_on=(), ...)` runs one
named-agent child, resolved through the shared
[`agent_queries`](./agent-definitions.md) contract with the same narrowing
(`apply_agent_profile`) as `task`. `key` is required; `depends_on` names
known keys explicitly — there is no dependency inference, and a `str`/`dict`
passed as `depends_on` is refused rather than iterated. `max_steps` caps the
node's child run; node concurrency is bounded internally, and the admitted
`policy.max_root_children` quota (1..64, default 4) still governs the total
child budget.

Each node persists under a fingerprint of its *effective* execution spec
(resolved prompt body, stance, tool scope, budget, label, dependencies, and
run input). A resumed run reuses a completed node's durable outcome only
when the fingerprint matches; any effective change refuses loudly instead of
reusing a different outcome. Large child outputs always resolve to the full
artifact bytes, never the bounded preview; inline state stays small while
reads reassemble whole UTF-8 text across page cuts.

## Driver and states

The driver is a core background action (`Plugin.background_action`): it keeps
only the `BackgroundContext`, commits per short `invoke`, and never holds a
lease across awaits. `TaskGroup` rests classify honestly — approval waits
and recoverable interruptions park, pause pauses, cancellation cancels —
while any real failure marks the run failed *and* fails the action, so
`--wait-background` exits nonzero with the recorded error.

Run states are `running`, `paused`, `parked`, `completed`, `failed`,
`cancelled`. Steering commands re-read state after their slow RPCs, and a
run that already reached a terminal state is never overwritten by a late
driver outcome — no cancelled run is re-reported as failed.

`workflow.pause` parks the driver while children keep running;
`workflow.resume` re-drives the same script/input/keys without duplicating
children; `workflow.cancel` stops the driver plus every owned child (a
non-user caller confirms first). Cancel is cooperative: it aborts at `ctx`
boundaries, accepts no new nodes afterwards, and cancels owned children, but
it cannot force-terminate arbitrarily blocked script Python or host I/O.
Completion notices reach the originating
session only when requested: the model-facing `workflow_run` tool defaults
`notify_on_completion` to true and the terminal summary arrives as a
durable follow-up turn; plain commands stay quiet.

## Client surface

Commands (`workflow.run`, `workflow.status`, `workflow.pause`,
`workflow.resume`, `workflow.cancel`) carry shared `client_syntax`
(`/workflow.*`), the cancel confirmation uses the standard confirmation
flow, and views (`workflows`, `workflows.run` form) render through the
generic descriptors — one surface for CLI, TUI, and web. Ephemeral CLIs
stay alive explicitly: `invoke`/`confirm --wait-background` waits after the
command commits and reports the settled action states in the same JSON
result.
