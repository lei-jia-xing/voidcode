# cron

Scheduled prompts as session policy over the core's durable wakeup seam: create them with tools, list them, and settle them from durable wakeup facts.

Implementation lives in cohesive modules behind the `cron_plugin` facade: `runtime` (clock and policy knobs), `state` (models/parsing/CAS store), `scheduling` (create/delete and staged wakeup arming), `presentation`, `tools`, `commands`, `observer` (pending settlement and facts catch-up), and `lifecycle`. The facade preserves the historical import and `_clock_ms`/`MAX_SCHEDULES_PER_SESSION` monkeypatch paths.

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
- The observer subscribes to `run_requested` (every run start, including resumes), `wakeup_fired`, and `run_finished`. On every trigger it pages the session's facts from a durable `facts_cursor` until exhausted and commits the advanced cursor atomically with the resulting state and successor wakeups. Pages are never capped. The first schedule in a session seeds the cursor at the current session tip, so a long-lived session never replays facts that predate its first schedule. This catches a receipted observer failure or stage-time state CAS: the cursor is not advanced by the failed callback, and the next writable trigger processes the same range. A `wakeup_fired` records a durable `pending` settlement keyed by schedule/wakeup plus the fired sequence, role, and payload; the terminal fact found in the same catch-up (or a later one) settles it. This is independent of the run id, so an approval/ask park and its resume (which starts a new run id) still settle the original firing. A late replayed firing whose run already finished is paused visibly (`reason: settlement_missed`) rather than left enabled with no successor. The plugin API has no writable process-startup hook (`session_start`/`session_end` leases are read-only), so process startup itself begins catch-up on the next writable trigger — a new run start or a claimed wakeup.
- Settlement follows the run: one-shots delete on success and stay disabled for inspection on failure; recurring schedules re-arm only when the next instant is still before `expires_at`. A typed `RunFinishedFact.budget_exhaustion` or a `limit_reached` stop disables a recurring schedule instead of re-arming into the same exhausted cap. A long run delays the next fire and missed slots are never replayed.
- Each settlement stages a `schedule_fired` event (`id`, `wakeup_id`, `fired_sequence`, `outcome`, `run_status`, optional `reason`/`budget_exhaustion`/`next_fire_at`). Deletion stages `schedule_deleted`. Malformed state is surfaced as a bounded `schedule_error` plugin event rather than an observer exception; tools/commands fail with a clear state error. A fully matched owned firing whose immutable payload disagrees with the schedule (or a second firing while one settlement is pending) disables that schedule with `outcome: disabled`, `reason: schedule_error`, and the bounded error instead of advancing the cursor past an enabled schedule that can never re-arm.
- Deleting a pending schedule cancels its armed wakeup atomically. Deleting a schedule whose wakeup was already claimed removes the schedule but does not interrupt that run; its eventual terminal fact settles the core claim and cron has no pending entry to re-arm. This race is deliberate.
- Disabling parks every schedule disabled with no armed wakeup; schedules stay inspectable but never silently re-arm — recreate the schedule to fire again.
- State v2 stores the optional `facts_cursor`; a stored v2 row without it starts catch-up at sequence zero. State v1 still requires a reset (no migration hook).

## Resetting v1 cron state

A stored v1 row makes the core refuse to start (`plugin cron state schema mismatch: stored v1, declared v2`); it is not reinterpreted and no automatic migration runs. Stop every process using the workspace, back up the database, then delete only the cron owner row:

```sh
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/voidcode/$(printf '%s' "$(realpath "$PWD")" | sha256sum | cut -d' ' -f1)"
sqlite3 "$STATE_DIR/voidcode.db" "DELETE FROM plugin_state WHERE owner = 'cron';"
```

If the core was started with `--state-dir`, use that directory instead. This removes every cron schedule in that workspace's database; sessions, events, and other plugins' state are untouched.
