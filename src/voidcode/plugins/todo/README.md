# todo

Session todo list with provider-visible plan truth and durable reminders: the model applies operations, the plugin nudges when work stops with items incomplete.

## Surface

- Tool `todo` (`SESSION`, never replayed): one `op` per call — `init` (create the plan from `list=[{phase, items}]`, or `items` plus optional `phase`), `start`, `done`, `drop`, `block` (optional `reason`), `unblock`, `append`, `rm`, `view` (never mutates). Targets use exact task content and phase names; only one task may be `in_progress` (the runtime normalizes that invariant); statuses are `pending | in_progress | completed | abandoned | blocked`. Mutations bump a revision and stage a `todo_updated` event; the tool never writes workspace files.
- Command `/todo` (`READ`): paged task details with Add and Edit actions. `/todo add` prepares a phase and multiline task editor; `/todo edit` selects a task and offers applicable status operations. Submission checks the captured todo revision and refuses stale edits. `/todo clear` (`SESSION`) clears the list and offers Add tasks. Internal `todo.update` remains callable through forms and hidden from slash discovery. CLI receipts retain the readable task table.
- Context: contributes the plan truth while active tasks (`pending`/`in_progress`/`blocked`) remain.
- Observer on `invocation_finished` + `run_finished`: accounts reminders and hands each injection to the core as a user-role continuation with an idempotent `todo:<kind>:<cycle>:<attempt>` key, plus a `reminder_injected` event.

## Configuration

`plugins.todo.config` (strict; unknown keys refused, `providers` accepted and ignored):

```json
{"reminders": {"enabled": true, "todo": {"max_per_cycle": 3}}}
```

- `reminders.enabled` (bool, default `true`): `false` disables the observer entirely.
- `reminders.todo.max_per_cycle` (non-negative int, default `3`): completion reminders allowed per cycle.

See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- A cycle starts only on a fresh user request (resumes retain the cycle's budget). The mid-run nudge fires after 12 non-read-tier mutations outside the `todo` tool (max 2 per cycle; any `todo` touch resets the counter); the completion reminder fires on clean completed turns with incomplete items (up to `max_per_cycle`, silenced while awaiting progress until new tool results arrive).
- Session state (`schema_version 2`): phases, summary, revision, and the reminder bookkeeping (per-cycle attempts, mutation baseline, invocation count, fact cursor, cycle id). Disabling zeroes the counters but keeps the plan list and cursor — kept counters would describe cancelled injections, while a fresh user prompt starts a new cycle.
