# todo

Session todo list with provider-visible plan truth and durable reminders: the model applies operations, the plugin nudges when work stops with items incomplete.

## Surface

- Tool `todo` (`SESSION`, never replayed): one `op` per call — `init` (create the plan from `list=[{phase, items}]`, or `items` plus optional `phase`), `start`, `done`, `drop`, `block` (optional `reason`), `unblock`, `append`, `rm`, `view` (never mutates). `start` requires a task; every other targeted operation requires an explicit task or phase target. There is no implicit all-task `done`/`drop`/`rm`. Tasks carry stored 32-character hex ids; repeated wording is allowed, and a content target that matches more than one task fails as ambiguous. Only one task may be `in_progress` (the runtime normalizes that invariant); statuses are `pending | in_progress | completed | abandoned | blocked`. Mutations bump a revision and stage a `todo_updated` event; the tool never writes workspace files.
- Command `/todo` (`READ`): bounded paged task details with Add and Edit actions; receipt data carries only the displayed page. `/todo add` prepares a phase and multiline task editor; `/todo edit` selects a task and offers applicable status operations. Submission checks the captured todo revision and refuses stale edits; bookkeeping-only state churn is retried instead of failing the editor. `/todo clear` (`SESSION`, deliberately `busy_gate=False`) clears the list and offers Add tasks. Internal `todo.update` remains callable through forms and hidden from slash discovery.
- Context: contributes the plan truth while active tasks (`pending`/`in_progress`/`blocked`) remain. Task, phase, and blocker text is escaped and bounded inside an explicit `<untrusted_todos>` delimiter: it is data, not higher-priority instructions.
- Observer on `invocation_finished` + `run_finished`: accounts reminders and hands each injection to the core as a user-role continuation with an idempotent `todo:<kind>:<cycle>:<attempt>` key, plus a `reminder_injected` event. `init`, `rm`, `/todo clear`, and a user stop cancel this owner's queued todo nudges; other owners' queues are untouched.

## Configuration

`plugins.todo.config` (strict; unknown keys refused, `providers` accepted and ignored):

```json
{"reminders": {"enabled": true, "todo": {"max_per_cycle": 3}}}
```

- `reminders.enabled` (bool, default `true`): `false` disables the observer entirely.
- `reminders.todo.max_per_cycle` (non-negative int, default `3`): completion reminders allowed per cycle.

See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- A cycle starts only on a fresh user prompt (`RunRequestedFact.trigger == "user"`). Resumes, queued user follow-ups, and automatic continuations retain the cycle's budget, so a todo nudge cannot manufacture a new allowance. The mid-run nudge fires after 12 non-read-tier mutations outside the `todo` tool (max 2 per cycle; any successful `todo` tool call resets the counter); the completion reminder fires on clean completed turns with incomplete items (up to `max_per_cycle`, silenced while awaiting progress until new tool results arrive).
- Ingress is bounded per session row: 64 phases, 512 tasks, 256 tasks/phase, 512-byte phase names, 512-byte blockers, and 2048-byte task content (UTF-8). Provider context and reminder bodies are capped at 16 KiB and truncated visibly; command output is capped at 16 KiB.
- Session state (`schema_version 3`): phases with stable task ids, revision, and the reminder bookkeeping (per-cycle attempts, mutation baseline, invocation count, fact cursor, cycle id); the summary is derived from phases on read, never persisted. `active` includes blocked tasks. Unknown nested phase/task keys and missing required fields are refused visibly. Disabling zeroes the counters but keeps the plan list and cursor — kept counters would describe cancelled injections, while a fresh user prompt starts a new cycle.
