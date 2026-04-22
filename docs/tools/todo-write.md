# `todo_write`

`todo_write` updates runtime-visible work state for the current agent task.

It is not a project documentation tool, memory system, or replacement for user-facing progress updates.

## When to use it

Good fit:

- tracking a multi-step task,
- marking the current step as `in_progress`,
- recording completed or cancelled steps for runtime continuity.

Bad fit:

- writing project plans that belong in docs,
- storing long-term memory,
- logging every small implementation detail,
- replacing a concise user update.

## Risk profile

`todo_write` is `read_only=false`, so it may trigger approval.

## Input

```json
{
  "todos": [
    {"content": "Implement sidecar guidance", "status": "in_progress", "priority": "high"}
  ]
}
```

Allowed statuses:

- `pending`
- `in_progress`
- `completed`
- `cancelled`

Allowed priorities:

- `high`
- `medium`
- `low`

## Return value

On success, expect:

- `data.path`
- `data.summary.total`
- `data.summary.pending`
- `data.summary.in_progress`
- `data.summary.completed`
- `data.summary.cancelled`
