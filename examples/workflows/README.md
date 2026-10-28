# Parallel review: a runnable configured workflow

Two review children in parallel, then one combined summary. The script is
ordinary trusted Python — no manifest, no sandbox claim — admitted only
because `voidcode.json` names it explicitly.

## Layout

- `parallel_review.py` — `async def run(ctx, task)` using `asyncio.TaskGroup`
  plus `ctx.agent(...)` nodes. The return value is the run's durable result.

## Setup

Copy the script into your workspace, keeping whatever model, providers, and
approval settings you already have:

```bash
mkdir -p <workspace>/flows
cp examples/workflows/parallel_review.py <workspace>/flows/parallel_review.py
```

Then merge just the workflows fragment into your existing `voidcode.json`
(this example changes nothing about your model, providers, or approvals):

```jsonc
// merge into <workspace>/voidcode.json under "plugins"
{
  "workflows": {
    "config": {"workflows": {"parallel_review": "flows/parallel_review.py"}}
  }
}
```

Only `workflows: {name: workspace-relative .py path}` is accepted; unknown
fields, absolute paths, `..`, and non-`.py` files are refused. The `providers`
key is reserved for product injection.

## Run

```bash
export WS=/tmp/review-ws STATE=/tmp/review-state
uv run python -m voidcode --workspace "$WS" --state-dir "$STATE" \
  create-session --session-id demo
REV=$(uv run python -m voidcode --workspace "$WS" --state-dir "$STATE" \
  catalog | python -c "import json,sys; print(json.load(sys.stdin)['revision_id'])")
uv run python -m voidcode --workspace "$WS" --state-dir "$STATE" \
  invoke demo workflow.run \
  --arguments '{"workflow": "parallel_review", "task": "diff 42"}' \
  --expected-revision "$REV" --wait-background
```

Expected: `workflow.run` returns a receipt (`run_id`, `action_id`), and
`--wait-background` keeps the same process alive until the driver settles,
appending the terminal `background` entry. Read the graph plus the combined
summary any time with:

```bash
uv run python -m voidcode --workspace "$WS" --state-dir "$STATE" \
  invoke demo workflow.status --arguments '{"run_id": "<run_id>"}' \
  --expected-revision "$REV"
```

`workflow.pause` parks the driver (children keep running),
`workflow.resume` re-drives with the same script, input, and node keys, and
`workflow.cancel` stops the driver plus every owned child. A changed script
refuses resume before executing anything. The model-facing `workflow_run`
tool starts the same runs and, with `notify_on_completion`, hands the bounded
terminal summary back to the originating session as a follow-up turn.

Two honest boundaries: each resume re-executes this file's module top level
and `run(ctx, task)` — only the keyed `ctx.agent` nodes dedupe, script I/O
has no exactly-once guarantee. And cancel is cooperative: it stops the
driver at `ctx` boundaries and cancels owned children, but cannot
force-terminate arbitrarily blocked Python or host I/O.
