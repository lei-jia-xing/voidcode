# cron

Scheduled prompts as session policy over the core's durable wakeup seam: create them with tools, list them, and settle them from durable wakeup facts.

## Surface

- Tool `cron_create` (`SESSION`, never replayed): `schedule` + `prompt` (both required non-empty strings). Returns the schedule id (`sched-…`), human cadence, actual first-fire instant, and expiry.
- Tool `cron_delete` (`SESSION`, never replayed): `id` stages cancellation of one schedule and its armed wakeup. A run already in flight is not interrupted.
- Tool `cron_list` (`READ`, safe): lists this session's schedules with their stored `next_fire_at` (the actual armed/pending instant, never `now + interval`), `pending_wakeup_id`, `status` (`enabled`/`running`/`disabled`), and `expires_at`.
- Command `/loop` (`SESSION`, `client_syntax="/loop"`): `input` is raw `/loop` text (`[interval] <prompt>`, default interval `10m`); creates an `every <interval>` schedule and fires it immediately. Bare slash `/loop` prepares a multiline interval/prompt editor without scheduling; explicit empty CLI/RPC invocation returns usage. Creation presents Browse/Cancel actions. `/loop list` reads paged session schedules with creation and cancellation controls; `/loop delete <id>` stages cancellation using the same implementation as `cron_delete`.

Schedule grammar: `every <N><s|m|h|d>`, `at <ISO-8601>`, `once`, or `cron <min> <hour> <day> <month> <dow>` (five-field croniter syntax, names like `JAN`/`MON` allowed; six/seven-field refused). Calendar expressions follow host local time.

Policy: recurring schedules expire 7 days after creation. An `every` interval must be at least one minute and shorter than 7 days. A recurring schedule whose first or next fire is at/after `expires_at` is rejected at creation or retired at settlement; use `at`/`once` for a one-shot beyond that horizon. One-shot `at`/`once` schedules never expire.

## Configuration

Takes no plugin config; `create_plugin` accepts and ignores its mapping (the schedule policy — 7-day recurring expiry, 1-minute minimum, 50-job cap — is the ported legacy policy). See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Session scope: the creating session owns each schedule (max 50), and every schedule keeps at most one armed wakeup. Wakeups are staged with the tool/command/observer result, so a cancelled or rejected result commits no orphan wakeup; deletion stages the state removal and wakeup cancellation in one transaction.
- The observer subscribes to `wakeup_fired` and `run_finished`. A `wakeup_fired` fact records a durable `pending` settlement keyed by schedule/wakeup plus the fired sequence, role, and payload; it never scans historical event pages. The next terminal `run_finished` settles it. This is independent of the run id, so an approval/ask park and its resume (which starts a new run id) still settle the original firing. A late replayed firing whose run already finished is paused visibly (`reason: settlement_missed`) rather than left enabled with no successor.
- Settlement follows the run: one-shots delete on success and stay disabled for inspection on failure; recurring schedules re-arm only when the next instant is still before `expires_at`. A typed `RunFinishedFact.budget_exhaustion` or a `limit_reached` stop disables a recurring schedule instead of re-arming into the same exhausted cap. A long run delays the next fire and missed slots are never replayed.
- Each settlement stages a `schedule_fired` event (`id`, `wakeup_id`, `fired_sequence`, `outcome`, `run_status`, optional `reason`/`budget_exhaustion`/`next_fire_at`). Deletion stages `schedule_deleted`. Malformed state is surfaced as a bounded `schedule_error` plugin event rather than an observer exception; tools/commands fail with a clear state error.
- Deleting a pending schedule cancels its armed wakeup atomically. Deleting a schedule whose wakeup was already claimed removes the schedule but does not interrupt that run; its eventual terminal fact settles the core claim and cron has no pending entry to re-arm. This race is deliberate.
- Disabling parks every schedule disabled with no armed wakeup; schedules stay inspectable but never silently re-arm — recreate the schedule to fire again.
