# cron

Scheduled prompts as session policy over the core's durable wakeup seam: create them with tools, list them, and re-arm them when their runs finish.

## Surface

- Tool `cron_create` (`SESSION`, never replayed): `schedule` + `prompt` (both required non-empty strings). Returns the schedule id (`sched-…`) and human cadence.
- Tool `cron_delete` (`SESSION`, never replayed): `id` cancels one schedule and disarms its wakeup.
- Tool `cron_list` (`READ`, safe): lists this session's schedules with recomputed `next_fire_at` (exact for `at`/`once`, within one tick otherwise), `enabled`, and `expires_at`.
- Command `/loop` (`SESSION`, `client_syntax="/loop"`): `input` is raw `/loop` text (`[interval] <prompt>`, default interval `10m`); creates an `every <interval>` schedule and fires it immediately (`next_fire_at: now`). Bare `/loop` returns usage.

Schedule grammar: `every <N><s|m|h|d>`, `at <ISO-8601>`, `once`, or `cron <min> <hour> <day> <month> <dow>` (five-field croniter syntax, names like `JAN`/`MON` allowed; six/seven-field refused). Calendar expressions follow host local time.

## Configuration

Takes no plugin config; `create_plugin` accepts and ignores its mapping (the schedule policy — 7-day recurring expiry, 50-job cap — is the ported legacy policy). See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Session scope: the creating session owns each schedule (max 50), and every schedule keeps exactly one armed wakeup — a claimed wakeup means a run is in flight, which makes overlap impossible without a lock. The plugin never sleeps or polls; the core ticker owns time.
- The observer subscribes only to `run_finished`: when a run ends it scans history for `WakeupFiredFact`s with that run's id, keeping only owned, current ones (matching owner, `wakeup_id`, `schedule_id`, payload kind `cron`); foreign or stale firings only advance the cursor. Settlement follows the finished run's status: one-shots delete on success and stay disabled for inspection on failure; recurring schedules retire past expiry, otherwise recompute `next_fire_after` from now — a long run delays the next fire and missed slots are never replayed. Each settlement stages a `schedule_fired` event (`id`, `outcome`, `run_status`, optional `next_fire_at`).
- `cron_list` recomputes next-fire from the expression at list time; the armed wakeup remains the truth (a plugin cannot read core wakeup rows).
- Disabling parks every schedule disabled with no armed wakeup; schedules stay inspectable but never silently re-arm — recreate the schedule to fire again.
